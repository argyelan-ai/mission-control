"use client";

// First-run wizard — lands here right after initial registration.
// Step 1 (Admin) is already done on arrival; the wizard walks through
// the provider key (skippable) and starter content. No new backend
// endpoint needed — everything runs over existing APIs.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { useTranslations } from "next-intl";
import { motion } from "framer-motion";
import { Check, ExternalLink, Loader2, Rocket } from "lucide-react";
import { AUTH_TOKEN_KEY, api } from "@/lib/api";
import { AmbientBackground } from "@/components/layout/AmbientBackground";
import { C } from "@/lib/colors";

import type { GithubConfigUpdate, Priority, TaskStatus } from "@/lib/types";

// Keep in sync with scripts/demo-seed.py (same crew, same board) until the
// seed moves into one backend endpoint.
const DEMO_AGENTS: Array<[string, string, string, boolean]> = [
  ["Atlas", "🧭", "Board lead — plans phases, dispatches subtasks to the crew", true],
  ["Nova", "⚡", "Builder — implements tasks on their own branches", false],
  ["Bolt", "🔧", "Builder — infrastructure and performance work", false],
  ["Vega", "🔍", "Reviewer — gates every merge before it lands", false],
];

const DEMO_TASKS: Array<[string, TaskStatus, Priority, string | null]> = [
  ["Draft launch announcement blog post", "done", "high", "Nova"],
  ["Set up staging environment", "done", "medium", "Bolt"],
  ["Landing page hero section", "review", "high", "Nova"],
  ["Load-test the API gateway", "in_progress", "high", "Bolt"],
  ["Write onboarding e-mail sequence", "in_progress", "medium", "Atlas"],
  ["Legal review of the license FAQ", "blocked", "medium", "Vega"],
  ["Social media launch thread", "inbox", "medium", null],
  ["Post-launch retro board", "inbox", "low", null],
];

const inputClasses =
  "w-full bg-transparent border rounded-sm px-3 py-2.5 text-sm outline-none transition-all duration-200";
const inputStyle = {
  backgroundColor: "var(--color-bg-surface)",
  borderColor: "var(--color-border)",
  color: "var(--color-text-primary)",
} as const;

export default function SetupWizardPage() {
  const t = useTranslations("setup");
  const router = useRouter();
  const [step, setStep] = useState<2 | 3 | 4>(2);

  // Provider key (step 2)
  const [providers, setProviders] = useState<
    Array<{ provider: string; key: string; label: string; description: string; placeholder: string }>
  >([]);
  const [selected, setSelected] = useState(0);
  const [keyValue, setKeyValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [keySaved, setKeySaved] = useState(false);
  const [error, setError] = useState("");

  // Connect GitHub (step 3)
  const [githubOwner, setGithubOwner] = useState("");
  const [githubToken, setGithubToken] = useState("");
  const [githubSaving, setGithubSaving] = useState(false);
  const [githubSaved, setGithubSaved] = useState(false);
  const [githubSkipped, setGithubSkipped] = useState(false);
  const [githubError, setGithubError] = useState("");

  // Demo board (step 4)
  const [seeding, setSeeding] = useState(false);
  const [seeded, setSeeded] = useState(false);

  useEffect(() => {
    if (!localStorage.getItem(AUTH_TOKEN_KEY)) {
      router.replace("/login");
      return;
    }
    api.secrets
      .providers()
      .then(setProviders)
      .catch(() => setProviders([]));
  }, [router]);

  async function saveKey() {
    const p = providers[selected];
    if (!p || !keyValue.trim()) return;
    setSaving(true);
    setError("");
    try {
      await api.secrets.create({
        key: p.key,
        value: keyValue.trim(),
        provider: p.provider,
        label: p.label,
        description: p.description,
      });
      setKeySaved(true);
      setStep(3);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("saveFailed"));
    } finally {
      setSaving(false);
    }
  }

  async function saveGithub() {
    setGithubSaving(true);
    setGithubError("");
    try {
      const payload: GithubConfigUpdate = {};
      if (githubOwner.trim()) payload.owner = githubOwner.trim();
      if (githubToken.trim()) payload.token = githubToken.trim();
      await api.repos.setGithubConfig(payload);
      setGithubSaved(true);
      setStep(4);
    } catch (err) {
      setGithubError(err instanceof Error ? err.message : t("saveFailed"));
    } finally {
      setGithubSaving(false);
    }
  }

  async function seedDemo() {
    setSeeding(true);
    setError("");
    try {
      const board = await api.boards.create({
        name: "🚀 Demo: Product Launch",
        slug: "demo-product-launch",
        description: "Demo board — safe to delete.",
        objective: "Ship v1.0 publicly: site live, docs done, launch thread out.",
        color: C.accent,
      });
      const agentIds: Record<string, string> = {};
      for (const [name, emoji, role, isLead] of DEMO_AGENTS) {
        const agent = await api.agents.create({
          name,
          emoji,
          role,
          board_id: board.id,
          is_board_lead: isLead,
          agent_runtime: "cli-bridge",
        });
        agentIds[name] = agent.id;
      }
      for (const [title, status, priority, assignee] of DEMO_TASKS) {
        await api.tasks.create(board.id, {
          title,
          status,
          priority,
          ...(assignee ? { assigned_agent_id: agentIds[assignee] } : {}),
        });
      }
      setSeeded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : t("demoBoardFailed"));
    } finally {
      setSeeding(false);
    }
  }

  const steps = [
    { n: 1, label: t("stepAdmin"), done: true },
    { n: 2, label: t("stepProviderKey"), done: keySaved || step > 2 },
    { n: 3, label: t("stepGithub"), done: githubSaved || githubSkipped || step > 3 },
    { n: 4, label: t("stepGetStarted"), done: false },
  ];

  return (
    <main
      className="min-h-dvh flex items-center justify-center relative"
      style={{ backgroundColor: "var(--color-bg-deep)" }}
    >
      <AmbientBackground />

      <motion.div
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.5, ease: [0.16, 1, 0.3, 1] }}
        className="w-full max-w-md px-4 relative z-10"
      >
        {/* Step indicator */}
        <div className="flex items-center justify-center gap-3 mb-8">
          {steps.map((s, i) => (
            <div key={s.n} className="flex items-center gap-3">
              <div className="flex items-center gap-2">
                <div
                  className="w-6 h-6 rounded-full flex items-center justify-center text-[11px] font-mono"
                  style={{
                    background: s.done ? C.accent : step === s.n ? C.accentSubtle : "transparent",
                    border: `1px solid ${s.done || step === s.n ? C.accent : C.border}`,
                    color: s.done ? C.onAccent : step === s.n ? C.accent : "var(--color-text-muted)",
                  }}
                >
                  {s.done ? <Check size={13} strokeWidth={3} /> : s.n}
                </div>
                <span
                  className="text-xs"
                  style={{
                    color: s.done || step === s.n ? "var(--color-text-primary)" : "var(--color-text-muted)",
                  }}
                >
                  {s.label}
                </span>
              </div>
              {i < steps.length - 1 && (
                <div className="w-8 h-px" style={{ background: C.border }} />
              )}
            </div>
          ))}
        </div>

        <div
          className="p-6 space-y-5"
          style={{ background: C.bgSurface, border: `1px solid ${C.border}`, borderRadius: 12 }}
        >
          {step === 2 && (
            <>
              <div>
                <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  {t("connectProvider")}
                </h2>
                <p className="text-sm mt-1" style={{ color: "var(--color-text-secondary)" }}>
                  {t("connectProviderHint")}
                </p>
              </div>

              <div className="space-y-1.5">
                <label className="label-sys" htmlFor="provider">{t("provider")}</label>
                <select
                  id="provider"
                  value={selected}
                  onChange={(e) => setSelected(Number(e.target.value))}
                  className={inputClasses}
                  style={inputStyle}
                >
                  {providers.map((p, i) => (
                    <option key={p.key} value={i} style={{ background: C.bgSurface }}>
                      {p.label}
                    </option>
                  ))}
                </select>
                {providers[selected] && (
                  <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                    {providers[selected].description}
                  </p>
                )}
              </div>

              <div className="space-y-1.5">
                <label className="label-sys" htmlFor="key">{t("keyLabel")}</label>
                <input
                  id="key"
                  type="password"
                  value={keyValue}
                  onChange={(e) => setKeyValue(e.target.value)}
                  placeholder={providers[selected]?.placeholder ?? "sk-..."}
                  className={`${inputClasses} font-mono`}
                  style={inputStyle}
                  onFocus={(e) => (e.currentTarget.style.borderColor = "var(--color-accent)")}
                  onBlur={(e) => (e.currentTarget.style.borderColor = "var(--color-border)")}
                />
              </div>

              {error && (
                <p
                  className="text-xs rounded-lg px-3 py-2"
                  style={{
                    color: "var(--color-error)",
                    backgroundColor: `${C.error}14`,
                    border: `1px solid ${C.error}26`,
                  }}
                >
                  {error}
                </p>
              )}

              <div className="flex items-center gap-3">
                <button
                  onClick={saveKey}
                  disabled={saving || !keyValue.trim()}
                  className="flex-1 text-[var(--color-on-accent)] font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
                >
                  {saving && <Loader2 className="animate-spin" size={14} />}
                  {t("saveContinue")}
                </button>
                <button
                  onClick={() => setStep(3)}
                  className="text-sm px-3 py-2.5 cursor-pointer"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {t("skip")}
                </button>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <div>
                <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  {t("stepGithub")}
                </h2>
                <p className="text-sm mt-1" style={{ color: "var(--color-text-secondary)" }}>
                  {t("githubHint")}
                </p>
              </div>

              <div className="space-y-1.5">
                <label className="label-sys" htmlFor="gh-owner">{t("owner")}</label>
                <input
                  id="gh-owner"
                  value={githubOwner}
                  onChange={(e) => setGithubOwner(e.target.value)}
                  placeholder="your-github-user-or-org"
                  className={inputClasses}
                  style={inputStyle}
                  onFocus={(e) => (e.currentTarget.style.borderColor = "var(--color-accent)")}
                  onBlur={(e) => (e.currentTarget.style.borderColor = "var(--color-border)")}
                />
              </div>

              <div className="space-y-1.5">
                <label className="label-sys" htmlFor="gh-token">{t("pat")}</label>
                <input
                  id="gh-token"
                  type="password"
                  value={githubToken}
                  onChange={(e) => setGithubToken(e.target.value)}
                  placeholder="ghp_..."
                  className={`${inputClasses} font-mono`}
                  style={inputStyle}
                  onFocus={(e) => (e.currentTarget.style.borderColor = "var(--color-accent)")}
                  onBlur={(e) => (e.currentTarget.style.borderColor = "var(--color-border)")}
                />
              </div>

              {githubError && (
                <p
                  className="text-xs rounded-lg px-3 py-2"
                  style={{
                    color: "var(--color-error)",
                    backgroundColor: `${C.error}14`,
                    border: `1px solid ${C.error}26`,
                  }}
                >
                  {githubError}
                </p>
              )}

              <div className="flex items-center gap-3">
                <button
                  onClick={saveGithub}
                  disabled={githubSaving || (!githubOwner.trim() && !githubToken.trim())}
                  className="flex-1 text-[var(--color-on-accent)] font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
                >
                  {githubSaving && <Loader2 className="animate-spin" size={14} />}
                  {t("saveContinue")}
                </button>
                <button
                  onClick={() => { setGithubSkipped(true); setStep(4); }}
                  className="text-sm px-3 py-2.5 cursor-pointer"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  {t("skipForNow")}
                </button>
              </div>
              <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                {t("connectLater")}
              </p>
            </>
          )}

          {step === 4 && (
            <>
              <div>
                <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  {t("readyToStart")}
                </h2>
                <p className="text-sm mt-1" style={{ color: "var(--color-text-secondary)" }}>
                  {t("readyHint")}
                </p>
              </div>

              <button
                onClick={seedDemo}
                disabled={seeding || seeded}
                className="w-full text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:cursor-not-allowed"
                style={{
                  border: `1px solid ${seeded ? C.online : C.border}`,
                  color: seeded ? C.online : "var(--color-text-primary)",
                  background: "var(--color-bg-surface)",
                }}
              >
                {seeding && <Loader2 className="animate-spin" size={14} />}
                {seeded ? (
                  <>
                    <Check size={14} /> {t("demoBoardCreated")}
                  </>
                ) : (
                  t("createDemoBoard")
                )}
              </button>

              <a
                href="https://github.com/argyelan-ai/mission-control/blob/main/docs/setup/first-agent.md"
                target="_blank"
                rel="noreferrer"
                className="w-full text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2"
                style={{
                  border: `1px solid ${C.border}`,
                  color: "var(--color-text-secondary)",
                  background: "var(--color-bg-surface)",
                }}
              >
                {t("firstAgentGuide")} <ExternalLink size={13} />
              </a>

              {error && (
                <p
                  className="text-xs rounded-lg px-3 py-2"
                  style={{
                    color: "var(--color-error)",
                    backgroundColor: `${C.error}14`,
                    border: `1px solid ${C.error}26`,
                  }}
                >
                  {error}
                </p>
              )}

              <button
                onClick={() => router.replace("/")}
                className="w-full text-[var(--color-on-accent)] font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200"
                style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
              >
                <Rocket size={14} /> {t("goToCommandCenter")}
              </button>
            </>
          )}
        </div>

        <p className="text-center text-xs mt-4" style={{ color: "var(--color-text-muted)" }}>
          {t("changeableLater")}
        </p>
      </motion.div>
    </main>
  );
}
