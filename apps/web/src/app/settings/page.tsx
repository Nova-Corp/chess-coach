"use client";

import { useCallback, useEffect, useState } from "react";
import { BOARD_THEMES, useBoardTheme } from "@/lib/boardTheme";
import { API_URL } from "@/lib/api";

const PROVIDERS = [
  { value: "anthropic", label: "Anthropic (Claude)" },
  { value: "openai",    label: "OpenAI (GPT)" },
  { value: "gemini",    label: "Google Gemini" },
  { value: "ollama",    label: "Ollama (local)" },
] as const;

type Provider = (typeof PROVIDERS)[number]["value"];

interface LLMSettings {
  provider: Provider | "";
  model: string;
  has_api_key: boolean;
  models: Record<Provider, string[]>;
}

function SectionHeader({ title, description }: { title: string; description: string }) {
  return (
    <div className="mb-4">
      <h2 className="text-base font-semibold text-neutral-200">{title}</h2>
      <p className="mt-0.5 text-sm text-neutral-400">{description}</p>
    </div>
  );
}

function Divider() {
  return <hr className="border-neutral-800" />;
}

export default function SettingsPage() {
  const [boardTheme, setBoardTheme] = useBoardTheme();

  // LLM settings — stored in backend
  const [provider, setProvider] = useState<Provider>("anthropic");
  const [model, setModel] = useState("");
  const [customModel, setCustomModel] = useState(false);
  const [apiKey, setApiKey] = useState("");
  const [saved, setSaved] = useState<LLMSettings | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [status, setStatus] = useState<{ ok: boolean; msg: string } | null>(null);

  function applySettings(data: LLMSettings) {
    const selectedProvider = data.provider || "anthropic";
    const selectedModel = data.model || data.models[selectedProvider][0];
    setSaved(data);
    setProvider(selectedProvider);
    setModel(selectedModel);
    setCustomModel(!data.models[selectedProvider].includes(selectedModel));
  }

  const loadSettings = useCallback(async () => {
    setLoading(true);
    setStatus(null);
    try {
      const res = await fetch(`${API_URL}/settings/llm`);
      if (!res.ok) throw new Error("Could not load AI Coach settings. Please try again.");
      applySettings(await res.json());
    } catch {
      setStatus({ ok: false, msg: "Could not load AI Coach settings. Check that the API is running." });
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { void loadSettings(); }, [loadSettings]);

  const isOllama = provider === "ollama";
  const hasStoredKey = saved?.provider === provider && saved.has_api_key;
  const models = saved?.models[provider] ?? [];
  const canSave = !!saved && !!model.trim() && (isOllama || !!apiKey.trim() || hasStoredKey);

  function changeProvider(nextProvider: Provider) {
    const nextModel = saved?.provider === nextProvider
      ? saved.model
      : saved?.models[nextProvider][0] ?? "";
    setProvider(nextProvider);
    setModel(nextModel);
    setCustomModel(!saved?.models[nextProvider].includes(nextModel));
    setApiKey("");
    setStatus(null);
  }

  async function handleSave(e: React.FormEvent) {
    e.preventDefault();
    if (!canSave || saving) return;
    setSaving(true);
    setStatus(null);
    try {
      const res = await fetch(`${API_URL}/settings/llm`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ provider, model: model.trim(), api_key: apiKey }),
      });
      if (!res.ok) {
        const err = await res.json().catch(() => ({}));
        throw new Error(typeof err.detail === "string" ? err.detail : `HTTP ${res.status}`);
      }
      applySettings(await res.json());
      setStatus({ ok: true, msg: "Saved. Chat and game summaries will use this model." });
      setApiKey("");
    } catch (e) {
      setStatus({ ok: false, msg: e instanceof Error ? e.message : String(e) });
    } finally {
      setSaving(false);
    }
  }

  return (
    <main className="mx-auto max-w-lg px-6 py-12">
      <a href="/" className="mb-6 inline-block text-sm text-neutral-500 hover:text-neutral-300">
        ← Home
      </a>
      <h1 className="mb-8 text-2xl font-bold tracking-tight">Settings</h1>

      <div className="flex flex-col gap-8">

        {/* ── Appearance ─────────────────────────────────────────────────── */}
        <section>
          <SectionHeader title="Appearance" description="Visual preferences applied across the app." />
          <div className="flex flex-wrap gap-3">
            {BOARD_THEMES.map((theme) => {
              const selected = boardTheme.id === theme.id;
              const cells = Array.from({ length: 16 }, (_, i) => {
                const row = Math.floor(i / 4);
                const col = i % 4;
                return (row + col) % 2 === 0 ? theme.light : theme.dark;
              });
              return (
                <button
                  key={theme.id}
                  type="button"
                  onClick={() => setBoardTheme(theme.id)}
                  className={`flex flex-col items-center gap-2 rounded-lg border-2 p-2 transition-colors ${
                    selected ? "border-emerald-500" : "border-neutral-700 hover:border-neutral-500"
                  }`}
                >
                  <div className="grid grid-cols-4 overflow-hidden rounded" style={{ width: 56, height: 56 }}>
                    {cells.map((color, i) => (
                      <div key={i} style={{ backgroundColor: color }} />
                    ))}
                  </div>
                  <span className={`text-xs font-medium ${selected ? "text-emerald-400" : "text-neutral-400"}`}>
                    {theme.label}
                  </span>
                </button>
              );
            })}
          </div>
        </section>


        <Divider />

        {/* ── AI Coach ───────────────────────────────────────────────────── */}
        <section>
          <SectionHeader
            title="AI Coach"
            description="Choose the provider and model for coaching chat and game summaries. API keys are encrypted in local storage and sent only to the selected provider."
          />
          {loading && <p role="status" className="mb-3 text-sm text-neutral-500">Loading settings…</p>}
          <form onSubmit={handleSave}>
            <fieldset disabled={loading || saving || !saved} className="flex flex-col gap-4 disabled:opacity-60">
            <div>
              <label htmlFor="llm-provider" className="mb-1.5 block text-sm font-medium text-neutral-300">Provider</label>
              <select
                id="llm-provider"
                value={provider}
                onChange={(e) => changeProvider(e.target.value as Provider)}
                className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-200 focus:outline-none focus:ring-1 focus:ring-neutral-500"
              >
                {PROVIDERS.map((p) => (
                  <option key={p.value} value={p.value}>{p.label}</option>
                ))}
              </select>
            </div>

            <div>
              <label htmlFor="llm-model" className="mb-1.5 block text-sm font-medium text-neutral-300">Model</label>
              <select
                id="llm-model"
                value={customModel ? "__custom__" : model}
                onChange={(e) => {
                  const custom = e.target.value === "__custom__";
                  setCustomModel(custom);
                  setModel(custom ? "" : e.target.value);
                  setStatus(null);
                }}
                aria-describedby="llm-model-help"
                className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-200 focus:outline-none focus:ring-1 focus:ring-neutral-500"
              >
                {models.map((id) => <option key={id} value={id}>{id}</option>)}
                <option value="__custom__">Custom model…</option>
              </select>
              {customModel && (
                <div className="mt-3">
                  <label htmlFor="llm-custom-model" className="mb-1.5 block text-sm font-medium text-neutral-300">Model ID</label>
                  <input
                    id="llm-custom-model"
                    value={model}
                    onChange={(e) => { setModel(e.target.value); setStatus(null); }}
                    placeholder={isOllama ? "e.g. llama3.2:latest" : "Enter the provider’s exact model ID"}
                    required
                    maxLength={200}
                    autoComplete="off"
                    spellCheck={false}
                    className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-200 placeholder-neutral-600 focus:outline-none focus:ring-1 focus:ring-neutral-500"
                  />
                </div>
              )}
              <p id="llm-model-help" className="mt-1.5 text-xs text-neutral-500">
                {isOllama
                  ? "Choose a model installed in Ollama. Ollama must be running locally on port 11434; no API key is needed."
                  : "Choose a text model available to your API account, or enter a custom model ID. Availability and pricing depend on your provider."}
              </p>
            </div>

            {!isOllama && (
              <div>
                <label htmlFor="llm-api-key" className="mb-1.5 block text-sm font-medium text-neutral-300">API key</label>
                <input
                  id="llm-api-key"
                  type="password"
                  value={apiKey}
                  onChange={(e) => { setApiKey(e.target.value); setStatus(null); }}
                  placeholder={hasStoredKey ? "Leave blank to keep your saved key…" : "Enter your API key"}
                  required={!hasStoredKey}
                  autoComplete="new-password"
                  className="w-full rounded-lg border border-neutral-700 bg-neutral-900 px-3 py-2 text-sm text-neutral-200 placeholder-neutral-600 focus:outline-none focus:ring-1 focus:ring-neutral-500"
                />
                {hasStoredKey && (
                  <p className="mt-1.5 text-xs text-emerald-500">✓ A key is saved for this provider. You can change models without replacing it.</p>
                )}
              </div>
            )}

            <div className="flex items-center gap-4">
              <button
                type="submit"
                disabled={saving || !canSave}
                className="rounded-lg bg-emerald-700 px-5 py-2 text-sm font-semibold text-white hover:bg-emerald-600 disabled:opacity-40"
              >
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
            </fieldset>
            {status && (
              <p role={status.ok ? "status" : "alert"} className={`mt-3 text-sm ${status.ok ? "text-emerald-400" : "text-red-400"}`}>
                {status.msg}
              </p>
            )}
            {!loading && !saved && (
              <button type="button" onClick={() => void loadSettings()} className="mt-3 text-sm text-emerald-400 underline hover:text-emerald-300">
                Retry loading settings
              </button>
            )}
          </form>
        </section>

      </div>
    </main>
  );
}
