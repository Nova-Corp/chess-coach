"""Offline drill contract tests: run with python -m unittest discover -s tests."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chess
from fastapi.testclient import TestClient

from app import db, services
from app.main import app


class DrillTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        db_patch = patch.object(db, "DB_PATH", str(Path(temp.name) / "test.sqlite"))
        db_patch.start()
        self.addCleanup(db_patch.stop)
        remote_patch = patch("app.main.ensure_lichess_puzzles")
        remote_patch.start()
        self.addCleanup(remote_patch.stop)
        self.client = self.enterContext(TestClient(app))
        with db.conn_ctx() as conn:
            conn.execute("INSERT INTO players(username) VALUES ('learner')")
            self.game_id = conn.execute(
                "INSERT INTO games(player_username, chesscom_id, white, black, pgn)"
                " VALUES ('learner', 'game-1', 'learner', 'opponent', '*')"
            ).lastrowid
            # Isolated analysis fixtures cover ordinary moves, castling, and
            # underpromotion, where SAN cannot be consumed as coordinate moves.
            self.positions = [
                (0, chess.STARTING_FEN, "g1f3", "Nf3"),
                (2, "r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1", "e1g1", "O-O"),
                (4, "8/P7/7k/8/8/8/8/7K w - - 0 1", "a7a8n", "a8=N"),
            ]
            for ply, fen, best, _ in self.positions:
                conn.execute(
                    "INSERT INTO analyses(game_id, ply, fen, best_move, played_move,"
                    " classification, motif_tags, phase) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (self.game_id, ply, fen, best, best, "mistake",
                     json.dumps(["fork_missed"]), "middlegame"),
                )
            conn.execute(
                "INSERT INTO puzzles(id, source, fen, solution_moves, themes)"
                " VALUES ('lichess-1', 'lichess', ?, 'e2e4 e7e5', ?)",
                (chess.STARTING_FEN, json.dumps(["fork"])),
            )

    def queue(self):
        response = self.client.get("/players/learner/drill?motif=fork_missed&limit=30")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["items"]

    def test_own_game_solutions_are_legal_uci_and_keep_san_context(self):
        items = [item for item in self.queue() if item["type"] == "own_game"]
        self.assertEqual(len(items), len(self.positions))
        expected = {ply: (uci, san) for ply, _, uci, san in self.positions}
        for item in items:
            with self.subTest(ply=item["ply"]):
                uci, san = expected[item["ply"]]
                self.assertEqual(item["solution_moves"], [uci])
                self.assertEqual(item["best_move"], san)
                board = chess.Board(item["fen"])
                move = chess.Move.from_uci(item["solution_moves"][0])
                self.assertIn(move, board.legal_moves)
                self.assertEqual(board.san(move), san)

    def test_own_game_attempts_persist_under_distinct_stable_position_ids(self):
        own = [item for item in self.queue() if item["type"] == "own_game"]
        ids = {item["puzzle_id"] for item in own}
        self.assertEqual(len(ids), 3)
        self.assertNotIn(str(self.game_id), ids)
        for index, item in enumerate(own):
            response = self.client.post("/puzzle_attempts", json={
                "puzzle_id": item["puzzle_id"], "username": "learner",
                "solved": index % 2 == 0,
            })
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()["solved"], index % 2 == 0)
        again = [item for item in self.queue() if item["type"] == "own_game"]
        self.assertEqual({item["puzzle_id"] for item in again}, ids)
        with db.conn_ctx() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM puzzle_attempts").fetchone()[0], 3)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM puzzles WHERE source = 'own_game'"
            ).fetchone()[0], 3)

    def test_reanalysis_updates_solution_without_losing_attempts(self):
        item = next(item for item in self.queue() if item.get("ply") == 0)
        self.client.post("/puzzle_attempts", json={
            "puzzle_id": item["puzzle_id"], "username": "learner", "solved": True,
        })
        with db.conn_ctx() as conn:
            conn.execute("UPDATE analyses SET best_move = 'b1c3' WHERE ply = 0")
        updated = next(item for item in self.queue() if item.get("ply") == 0)
        self.assertEqual(updated["puzzle_id"], item["puzzle_id"])
        self.assertEqual(updated["solution_moves"], ["b1c3"])
        with db.conn_ctx() as conn:
            row = conn.execute("SELECT solution_moves FROM puzzles WHERE id = ?",
                               (item["puzzle_id"],)).fetchone()
            self.assertEqual(row["solution_moves"], "b1c3")
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM puzzle_attempts").fetchone()[0], 1)

    def test_own_game_puzzles_do_not_enter_lichess_selection_or_supply_count(self):
        self.queue()  # Persist own-game exercises with matching theme text.
        with db.conn_ctx() as conn:
            rows = services.lichess_puzzles_for_motif(conn, "fork_missed", 30)
            self.assertEqual([row["id"] for row in rows], ["lichess-1"])
            conn.execute("DELETE FROM puzzles WHERE source = 'lichess'")
            with patch("app.services._fetch_lichess_batch", return_value=[]) as fetch:
                services.ensure_lichess_puzzles(conn, "learner", "fork_missed", min_unused=1)
                fetch.assert_called()

    def test_lichess_attempts_still_work_and_attempted_puzzles_are_excluded(self):
        response = self.client.post("/puzzle_attempts", json={
            "puzzle_id": "lichess-1", "username": "learner", "solved": False,
        })
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()["solved"])
        self.assertNotIn("lichess-1", {item["puzzle_id"] for item in self.queue()})

    def test_missing_best_move_does_not_create_unsolvable_exercise(self):
        with db.conn_ctx() as conn:
            conn.execute("UPDATE analyses SET best_move = NULL WHERE ply = 0")
        self.assertFalse(any(item.get("ply") == 0 for item in self.queue()))

    def test_unknown_attempt_targets_are_rejected(self):
        for username, puzzle_id in [("missing", "lichess-1"), ("learner", "missing")]:
            with self.subTest(username=username, puzzle_id=puzzle_id):
                response = self.client.post("/puzzle_attempts", json={
                    "puzzle_id": puzzle_id, "username": username, "solved": True,
                })
                self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
