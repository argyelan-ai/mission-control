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
import { markWizardDone } from "@/lib/firstRun";
import { AmbientBackground } from "@/components/layout/AmbientBackground";
import { C, alpha } from "@/lib/colors";
import { CheckReason } from "@/components/shared/CheckStatus";
import { isValueRejected } from "@/lib/checkReason";

import type { ConfigCheckItem, GithubConfigUpdate, Priority, TaskStatus } from "@/lib/types";

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
  const tProviderDesc = useTranslations("providerDescriptions");
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
  // Test on save (ADR-093 §2, S5): the key is checked before the wizard moves
  // on. A rejected value cannot be sent on again unchanged.
  const [testingKey, setTestingKey] = useState(false);
  const [rejectedValue, setRejectedValue] = useState<string | null>(null);
  // The failed check: its reason is worded in the UI language (CheckReason).
  const [keyCheck, setKeyCheck] = useState<ConfigCheckItem | null>(null);
  const [storedKeys, setStoredKeys] = useState<Set<string>>(() => new Set());

  // Connect GitHub (step 3)
  const [githubOwner, setGithubOwner] = useState("");
  const [githubToken, setGithubToken] = useState("");
  const [githubSaving, setGithubSaving] = useState(false);
  const [githubSaved, setGithubSaved] = useState(false);
  const [githubSkipped, setGithubSkipped] = useState(false);
  const [githubError, setGithubError] = useState("");
  const [githubRejected, setGithubRejected] = useState<string | null>(null);
  const [githubCheck, setGithubCheck] = useState<ConfigCheckItem | null>(null);

  // Demo board (step 4)
  const [seeding, setSeeding] = useState(false);
  const [seeded, setSeeded] = useState(false);
  // Demo data only on a fresh install. The wizard creates the first board
  // only in step 4 (seedDemo) and registration creates none, so an existing
  // board means this is not a fresh install — /setup stays reachable there,
  // and one click used to write demo data into live data. Agents are not
  // checked: the migrations seed a built-in agent on every fresh install.
  // null = still checking; a failed check leaves the button available
  // (a fresh install must never be blocked from finishing the wizard).
  const [hasExistingData, setHasExistingData] = useState<boolean | null>(null);

  useEffect(() => {
    if (!localStorage.getItem(AUTH_TOKEN_KEY)) {
      router.replace("/login");
      return;
    }
    api.secrets
      .providers()
      .then(setProviders)
      .catch(() => setProviders([]));
    api.boards
      .list()
      .then((boards) => setHasExistingData(boards.length > 0))
      .catch(() => setHasExistingData(false));
  }, [router]);

  // Step 4 reached = the provider key and GitHub steps are behind the
  // operator (saved or skipped). From here on the wizard is done: the app
  // stops sending them back to /setup, even if they leave via a link instead
  // of the last button. /setup itself stays reachable.
  useEffect(() => {
    if (step === 4) void markWizardDone();
  }, [step]);

  /** Runs the registry check. null = the wizard may go on (it works, or no
   *  test procedure exists for it). Otherwise the reason, and whether the
   *  value itself was rejected (then the same value cannot be sent again;
   *  `item` carries the reason code) or only the test could not run (a
   *  retry may help). */
  async function checkSaved(
    kind: string,
    subjectId: string,
  ): Promise<{ rejected: true; item: ConfigCheckItem } | { rejected: false; error: string } | null> {
    try {
      const { item, notRunnable } = await api.configChecks.run(kind, subjectId);
      if (notRunnable || item.state === "ok") return null;
      return { rejected: true, item };
    } catch (err) {
      return { rejected: false, error: err instanceof Error ? err.message : String(err) };
    }
  }

  async function saveKey() {
    const p = providers[selected];
    const value = keyValue.trim();
    if (!p || !value) return;
    setSaving(true);
    setError("");
    setKeyCheck(null);
    try {
      // A retry after a rejection replaces the stored value instead of
      // creating the same key twice.
      if (storedKeys.has(p.key)) {
        await api.secrets.update(p.key, { value });
      } else {
        await api.secrets.create({
          key: p.key,
          value,
          provider: p.provider,
          label: p.label,
          description: p.description,
        });
        setStoredKeys((prev) => new Set(prev).add(p.key));
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : t("saveFailed"));
      setSaving(false);
      return;
    }
    setTestingKey(true);
    const problem = await checkSaved("secret", p.key);
    setTestingKey(false);
    setSaving(false);
    if (problem) {
      if (problem.rejected) {
        setKeyCheck(problem.item);
        // Only a refused value is locked; a test that did not get through
        // (timeout, network, rate limit) may be retried with the same value.
        setRejectedValue(isValueRejected(problem.item) ? value : null);
      } else {
        setError(t("checkFailed", { error: problem.error }));
      }
      return;
    }
    setRejectedValue(null);
    setKeySaved(true);
    setStep(3);
  }

  async function saveGithub() {
    setGithubSaving(true);
    setGithubError("");
    setGithubCheck(null);
    const payload: GithubConfigUpdate = {};
    if (githubOwner.trim()) payload.owner = githubOwner.trim();
    if (githubToken.trim()) payload.token = githubToken.trim();
    try {
      await api.repos.setGithubConfig(payload);
    } catch (err) {
      setGithubError(err instanceof Error ? err.message : t("saveFailed"));
      setGithubSaving(false);
      return;
    }
    const problem = await checkSaved("github", "connection");
    setGithubSaving(false);
    if (problem) {
      if (problem.rejected) {
        setGithubCheck(problem.item);
        setGithubRejected(
          isValueRejected(problem.item) ? `${payload.owner ?? ""}\u0000${payload.token ?? ""}` : null,
        );
      } else {
        setGithubError(t("checkFailed", { error: problem.error }));
      }
      return;
    }
    setGithubRejected(null);
    setGithubSaved(true);
    setStep(4);
  }

  async function seedDemo() {
    if (hasExistingData !== false) return;
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
        // "manual" = registry entry only — "cli-bridge" would schedule
        // auto-provisioning and greet the fresh install with four
        // provision_failed warnings (review finding 2026-08-06).
        const agent = await api.agents.create({
          name,
          emoji,
          role,
          board_id: board.id,
          is_board_lead: isLead,
          agent_runtime: "manual",
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
        {/* Step indicator. On a phone the four labelled steps do not fit in
            390 px: one line "Step 2 of 4 · Provider Key" plus a quiet bar. */}
        <div className="md:hidden mb-6" data-testid="setup-steps-compact">
          <p className="text-xs" style={{ color: "var(--color-text-secondary)" }}>
            {t("stepOf", {
              n: step,
              total: steps.length,
              label: steps.find((s) => s.n === step)?.label ?? "",
            })}
          </p>
          <div className="mt-2 flex gap-2" aria-hidden="true">
            {steps.map((s) => (
              <span
                key={s.n}
                className="h-1 flex-1 rounded-full"
                style={{ background: s.done || step === s.n ? C.accent : C.border }}
              />
            ))}
          </div>
        </div>
        <div className="hidden md:flex items-center justify-center gap-3 mb-8" data-testid="setup-steps">
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
                  onChange={(e) => {
                    setSelected(Number(e.target.value));
                    setRejectedValue(null);
                    setKeyCheck(null);
                    setError("");
                  }}
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
                    {tProviderDesc.has(providers[selected].key)
                      ? tProviderDesc(providers[selected].key)
                      : providers[selected].description}
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

              {(error || keyCheck) && (
                <div
                  role="alert"
                  data-testid="setup-key-error"
                  className="text-xs rounded-lg px-3 py-2"
                  style={{
                    color: "var(--color-error)",
                    backgroundColor: alpha(C.error, 0.08),
                    border: `1px solid ${alpha(C.error, 0.15)}`,
                  }}
                >
                  {keyCheck ? (
                    <>
                      <span className="block font-medium">
                        {isValueRejected(keyCheck) ? t("keyRejected") : t("keyNotConfirmed")}
                      </span>
                      <CheckReason item={keyCheck} className="mt-1" />
                    </>
                  ) : (
                    error
                  )}
                  {rejectedValue !== null && (
                    <span className="block mt-1" style={{ color: "var(--color-text-secondary)" }}>
                      {t("keyRejectedHint")}
                    </span>
                  )}
                </div>
              )}

              <div className="flex items-center gap-3">
                <button
                  onClick={saveKey}
                  disabled={saving || !keyValue.trim() || rejectedValue === keyValue.trim()}
                  className="flex-1 text-[var(--color-on-accent)] font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
                >
                  {saving && <Loader2 className="animate-spin" size={14} />}
                  {testingKey ? t("testingKey") : t("saveContinue")}
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

              {(githubError || githubCheck) && (
                <div
                  role="alert"
                  data-testid="setup-github-error"
                  className="text-xs rounded-lg px-3 py-2"
                  style={{
                    color: "var(--color-error)",
                    backgroundColor: alpha(C.error, 0.08),
                    border: `1px solid ${alpha(C.error, 0.15)}`,
                  }}
                >
                  {githubCheck ? (
                    <>
                      <span className="block font-medium">
                        {isValueRejected(githubCheck) ? t("githubRejected") : t("githubNotConfirmed")}
                      </span>
                      <CheckReason item={githubCheck} className="mt-1" />
                    </>
                  ) : (
                    githubError
                  )}
                </div>
              )}

              <div className="flex items-center gap-3">
                <button
                  onClick={saveGithub}
                  disabled={
                    githubSaving ||
                    (!githubOwner.trim() && !githubToken.trim()) ||
                    githubRejected === `${githubOwner.trim()}\u0000${githubToken.trim()}`
                  }
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
                disabled={seeding || seeded || hasExistingData !== false}
                className="w-full text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:cursor-not-allowed"
                style={{
                  border: `1px solid ${seeded ? C.online : C.border}`,
                  color: seeded ? C.online : "var(--color-text-primary)",
                  background: "var(--color-bg-surface)",
                  // Locked (existing data, or still checking) must look locked.
                  opacity: !seeded && hasExistingData !== false ? 0.5 : undefined,
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
              {hasExistingData && !seeded && (
                <p className="text-xs -mt-2" style={{ color: "var(--color-text-muted)" }}>
                  {t("demoLockedExisting")}
                </p>
              )}

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
                    backgroundColor: alpha(C.error, 0.08),
                    border: `1px solid ${alpha(C.error, 0.15)}`,
                  }}
                >
                  {error}
                </p>
              )}

              <button
                onClick={async () => {
                  await markWizardDone();
                  router.replace("/");
                }}
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
