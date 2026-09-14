"""Model selection, credential handling, and provider routing without live LLM calls."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import chess
import httpx
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from app import db, llm
from app.main import app


class LLMSettingsTests(unittest.TestCase):
    def setUp(self):
        temp = self.enterContext(tempfile.TemporaryDirectory())
        self.enterContext(patch.object(db, "DB_PATH", str(Path(temp) / "test.sqlite")))
        self.enterContext(patch.object(llm, "_fernet", Fernet(Fernet.generate_key())))
        self.client = self.enterContext(TestClient(app))

    def save(self, **settings):
        response = self.client.post("/settings/llm", json=settings)
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_empty_settings_offer_models_without_exposing_credentials(self):
        data = self.client.get("/settings/llm").json()
        self.assertEqual(data["provider"], "")
        self.assertEqual(data["model"], "")
        self.assertFalse(data["has_api_key"])
        self.assertEqual(set(data["models"]), set(llm.DEFAULT_MODELS))

    def test_model_change_preserves_encrypted_key_and_survives_reload(self):
        self.save(provider="openai", api_key="test-secret", model="gpt-4o-mini")
        with db.conn_ctx() as conn:
            encrypted = llm._read_llm_settings(conn)["llm_api_key"]
        data = self.save(provider="openai", model="gpt-4.1-mini", api_key="  ")
        self.assertTrue(data["has_api_key"])
        self.assertNotIn("test-secret", json.dumps(data))
        self.assertEqual(self.client.get("/settings/llm").json()["model"], "gpt-4.1-mini")
        with db.conn_ctx() as conn:
            self.assertEqual(llm._read_llm_settings(conn)["llm_api_key"], encrypted)
        self.assertNotEqual(encrypted, "test-secret")
        self.assertEqual(llm._get_llm_config(), ("openai", "test-secret", "gpt-4.1-mini"))

    def test_provider_switch_requires_its_own_key_and_does_not_change_settings_on_error(self):
        self.save(provider="openai", api_key="openai-key", model="gpt-4o-mini")
        response = self.client.post("/settings/llm", json={
            "provider": "anthropic", "model": "claude-sonnet-5",
        })
        self.assertEqual(response.status_code, 400)
        self.assertEqual(llm._get_llm_config(), ("openai", "openai-key", "gpt-4o-mini"))
        self.save(provider="anthropic", model="claude-sonnet-5", api_key="claude-key")
        self.assertEqual(llm._get_llm_config(), ("anthropic", "claude-key", "claude-sonnet-5"))

    def test_ollama_saves_a_model_without_credentials(self):
        self.save(provider="openai", api_key="old-key")
        data = self.save(provider="ollama", model="local-coach:latest")
        self.assertFalse(data["has_api_key"])
        self.assertEqual(llm._get_llm_config(), ("ollama", "", "local-coach:latest"))
        with db.conn_ctx() as conn:
            self.assertEqual(llm._read_llm_settings(conn)["llm_api_key"], "")

    def test_legacy_ollama_model_is_loaded_and_migrated(self):
        with db.conn_ctx() as conn:
            conn.executemany("INSERT INTO app_settings(key, value) VALUES (?, ?)", [
                ("llm_provider", "ollama"),
                ("llm_api_key", llm.encrypt_value("my-old-model:7b")),
            ])
        self.assertEqual(self.client.get("/settings/llm").json()["model"], "my-old-model:7b")
        self.assertEqual(llm._get_llm_config(), ("ollama", "", "my-old-model:7b"))
        self.save(provider="ollama")
        with db.conn_ctx() as conn:
            data = llm._read_llm_settings(conn)
            self.assertEqual(data["llm_model"], "my-old-model:7b")
            self.assertEqual(data["llm_api_key"], "")

    def test_legacy_cloud_config_and_old_client_payload_get_default_models(self):
        with db.conn_ctx() as conn:
            conn.executemany("INSERT INTO app_settings(key, value) VALUES (?, ?)", [
                ("llm_provider", "openai"), ("llm_api_key", llm.encrypt_value("old-key")),
            ])
        self.assertEqual(self.client.get("/settings/llm").json()["model"], "gpt-4o-mini")
        self.save(provider="openai", api_key="replacement")
        self.assertEqual(llm._get_llm_config(), ("openai", "replacement", "gpt-4o-mini"))
        self.save(provider="ollama", api_key="legacy-client:latest")
        self.assertEqual(llm._get_llm_config(), ("ollama", "", "legacy-client:latest"))

    def test_invalid_models_and_providers_are_rejected(self):
        for model in ["", "   ", "bad model", "bad?key=value", "x" * 201]:
            with self.subTest(model=model):
                response = self.client.post("/settings/llm", json={"provider": "ollama", "model": model})
                self.assertEqual(response.status_code, 400)
        response = self.client.post("/settings/llm", json={"provider": "unknown", "model": "model"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get("/settings/llm").json()["provider"], "")

    def test_model_change_invalidates_cached_summaries_but_key_change_does_not(self):
        self.save(provider="openai", api_key="key", model="gpt-4o-mini")
        with db.conn_ctx() as conn:
            conn.execute("INSERT INTO players(username) VALUES ('learner')")
            game_id = conn.execute(
                "INSERT INTO games(player_username, chesscom_id, pgn) VALUES ('learner', 'game', '*')"
            ).lastrowid
            conn.execute("INSERT INTO narratives VALUES (?, 'old summary', '2026-09-14')", (game_id,))
        self.save(provider="openai", api_key="new-key")
        with db.conn_ctx() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM narratives").fetchone()[0], 1)
        self.save(provider="openai", model="gpt-4.1-mini")
        with db.conn_ctx() as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM narratives").fetchone()[0], 0)

    def test_selected_model_reaches_chat_and_narrative_for_every_provider(self):
        client_type = httpx.AsyncClient
        for provider in llm.MODEL_OPTIONS:
            with self.subTest(provider=provider):
                model = "custom-model-v2"
                self.save(provider=provider, model=model, api_key="test-key" if provider != "ollama" else "")
                requests = []

                def respond(request):
                    requests.append(request)
                    if provider == "anthropic":
                        payload = {"content": [{"type": "thinking", "thinking": "internal"},
                                               {"type": "text", "text": "answer"}]}
                    elif provider == "gemini":
                        payload = {"candidates": [{"content": {"parts": [{"text": "answer"}]}}]}
                    elif provider == "openai":
                        payload = {"choices": [{"message": {"content": "answer"}}]}
                    else:
                        payload = {"message": {"content": "answer"}}
                    return httpx.Response(200, json=payload)

                with patch("app.llm.httpx.AsyncClient", side_effect=lambda **kw:
                           client_type(transport=httpx.MockTransport(respond), **kw)):
                    response = self.client.post("/chat", json={
                        "fen": chess.STARTING_FEN, "candidates": [], "question": "What is the plan?",
                    })
                    self.assertEqual(response.status_code, 200, response.text)
                    self.assertEqual(response.json()["answer"], "answer")
                    answer = asyncio.run(llm.narrative(
                        white="learner", black="opponent", result="1-0", opening_name=None,
                        player_username="learner", acpl=10, blunders=0, mistakes=0,
                        key_positions=[], dominant_motifs=[],
                    ))
                    self.assertEqual(answer, "answer")
                self.assertEqual(len(requests), 2)
                for request in requests:
                    body = json.loads(request.content)
                    if provider == "gemini":
                        self.assertIn(f"/models/{model}:generateContent", request.url.path)
                        self.assertEqual(request.headers["x-goog-api-key"], "test-key")
                        self.assertNotIn("test-key", str(request.url))
                    else:
                        self.assertEqual(body["model"], model)
                    if provider == "openai":
                        self.assertIn("max_completion_tokens", body)
                        self.assertNotIn("max_tokens", body)


if __name__ == "__main__":
    unittest.main()
