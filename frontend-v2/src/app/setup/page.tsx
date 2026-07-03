"use client";

// First-run wizard — lands here right after initial registration.
// Step 1 (Admin) is already done on arrival; the wizard walks through
// the provider key (skippable) and starter content. No new backend
// endpoint needed — everything runs over existing APIs.

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { motion } from "framer-motion";
import { Check, ExternalLink, Loader2, Rocket } from "lucide-react";
import { AUTH_TOKEN_KEY, api } from "@/lib/api";
import { AmbientBackground } from "@/components/layout/AmbientBackground";
import { C } from "@/lib/colors";

import type { Priority, TaskStatus } from "@/lib/types";

const DEMO_TASKS: Array<[string, TaskStatus, Priority]> = [
  ["Draft launch announcement blog post", "done", "high"],
  ["Set up staging environment", "done", "medium"],
  ["Landing page hero section", "review", "high"],
  ["Load-test the API gateway", "in_progress", "high"],
  ["Write onboarding e-mail sequence", "in_progress", "medium"],
  ["Legal review of the license FAQ", "blocked", "medium"],
  ["Social media launch thread", "inbox", "medium"],
  ["Post-launch retro board", "inbox", "low"],
];

const inputClasses =
  "w-full bg-transparent border rounded-lg px-3 py-2.5 text-sm outline-none transition-all duration-200";
const inputStyle = {
  backgroundColor: "rgba(255, 255, 255, 0.03)",
  borderColor: "var(--color-border)",
  color: "var(--color-text-primary)",
} as const;

export default function SetupWizardPage() {
  const router = useRouter();
  const [step, setStep] = useState<2 | 3>(2);

  // Provider key (step 2)
  const [providers, setProviders] = useState<
    Array<{ provider: string; key: string; label: string; description: string; placeholder: string }>
  >([]);
  const [selected, setSelected] = useState(0);
  const [keyValue, setKeyValue] = useState("");
  const [saving, setSaving] = useState(false);
  const [keySaved, setKeySaved] = useState(false);
  const [error, setError] = useState("");

  // Demo board (step 3)
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
      setError(err instanceof Error ? err.message : "Speichern fehlgeschlagen.");
    } finally {
      setSaving(false);
    }
  }

  async function seedDemo() {
    setSeeding(true);
    setError("");
    try {
      const board = await api.boards.create({
        name: "🚀 Demo: Product Launch",
        slug: "demo-product-launch",
        description: "Demo-Board — gefahrlos loeschbar.",
        objective: "Ship v1.0 publicly: site live, docs done, launch thread out.",
        color: "#0FA3A3",
      });
      for (const [title, status, priority] of DEMO_TASKS) {
        await api.tasks.create(board.id, { title, status, priority });
      }
      setSeeded(true);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Demo-Board fehlgeschlagen.");
    } finally {
      setSeeding(false);
    }
  }

  const steps = [
    { n: 1, label: "Admin", done: true },
    { n: 2, label: "Provider-Key", done: keySaved || step > 2 },
    { n: 3, label: "Loslegen", done: false },
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
                    background: s.done ? C.accent : step === s.n ? "rgba(15,163,163,0.15)" : "transparent",
                    border: `1px solid ${s.done || step === s.n ? C.accent : C.border}`,
                    color: s.done ? "#04110F" : step === s.n ? C.accent : "var(--color-text-muted)",
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
                  LLM-Provider verbinden
                </h2>
                <p className="text-sm mt-1" style={{ color: "var(--color-text-secondary)" }}>
                  Agents brauchen ein Modell. Der Key landet verschluesselt im
                  Secrets-Vault — spaeter aenderbar unter Settings → API Keys.
                </p>
              </div>

              <div className="space-y-1.5">
                <label className="text-nav" htmlFor="provider">Provider</label>
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
                <label className="text-nav" htmlFor="key">Key</label>
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
                    backgroundColor: "rgba(239, 68, 68, 0.08)",
                    border: "1px solid rgba(239, 68, 68, 0.15)",
                  }}
                >
                  {error}
                </p>
              )}

              <div className="flex items-center gap-3">
                <button
                  onClick={saveKey}
                  disabled={saving || !keyValue.trim()}
                  className="flex-1 text-white font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:opacity-40 disabled:cursor-not-allowed"
                  style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
                >
                  {saving && <Loader2 className="animate-spin" size={14} />}
                  Speichern & weiter
                </button>
                <button
                  onClick={() => setStep(3)}
                  className="text-sm px-3 py-2.5 cursor-pointer"
                  style={{ color: "var(--color-text-muted)" }}
                >
                  Ueberspringen
                </button>
              </div>
            </>
          )}

          {step === 3 && (
            <>
              <div>
                <h2 className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  Bereit zum Loslegen
                </h2>
                <p className="text-sm mt-1" style={{ color: "var(--color-text-secondary)" }}>
                  Optional: ein Demo-Board zeigt die Pipeline mit Beispiel-Tasks,
                  bevor der erste Agent provisioniert ist.
                </p>
              </div>

              <button
                onClick={seedDemo}
                disabled={seeding || seeded}
                className="w-full text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200 disabled:cursor-not-allowed"
                style={{
                  border: `1px solid ${seeded ? C.online : C.border}`,
                  color: seeded ? C.online : "var(--color-text-primary)",
                  background: "rgba(255,255,255,0.02)",
                }}
              >
                {seeding && <Loader2 className="animate-spin" size={14} />}
                {seeded ? (
                  <>
                    <Check size={14} /> Demo-Board angelegt
                  </>
                ) : (
                  "Demo-Board anlegen (8 Beispiel-Tasks)"
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
                  background: "rgba(255,255,255,0.02)",
                }}
              >
                Anleitung: ersten Agent provisionieren <ExternalLink size={13} />
              </a>

              {error && (
                <p
                  className="text-xs rounded-lg px-3 py-2"
                  style={{
                    color: "var(--color-error)",
                    backgroundColor: "rgba(239, 68, 68, 0.08)",
                    border: "1px solid rgba(239, 68, 68, 0.15)",
                  }}
                >
                  {error}
                </p>
              )}

              <button
                onClick={() => router.replace("/")}
                className="w-full text-white font-medium text-sm rounded-lg px-4 py-2.5 flex items-center justify-center gap-2 cursor-pointer transition-all duration-200"
                style={{ background: `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` }}
              >
                <Rocket size={14} /> Zum Leitstand
              </button>
            </>
          )}
        </div>

        <p className="text-center text-xs mt-4" style={{ color: "var(--color-text-muted)" }}>
          Alles hier ist spaeter unter Settings aenderbar.
        </p>
      </motion.div>
    </main>
  );
}
