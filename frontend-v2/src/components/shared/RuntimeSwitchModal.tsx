"use client";

/**
 * RuntimeSwitchModal — Phase 15 T3.1.
 *
 * Replaces the previous `window.confirm()` with a modal that surfaces:
 *   - source / target runtime pair
 *   - image-switch warning when the container image will change (slow path)
 *   - compatibility warnings from the dry-run preview (e.g. tools mismatch)
 *   - in-progress force toggle when the agent is actively working
 *   - submit progress (spinner + long-switch hint after 10s)
 *
 * Re-used by AgentDetailPage's RuntimeSelectionSection and by /runtimes
 * BindAgentModal (T3.3).
 */

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { AlertTriangle, Check, Loader2, Lock, RotateCcw, X, Zap, Box } from "lucide-react";
import type { Agent, RuntimeSwitchPreview, RuntimeSwitchSummary } from "@/lib/types";
import { api } from "@/lib/api";
import { useQuery } from "@tanstack/react-query";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";

const SWITCH_STEPS: Array<{ key: string; label: string }> = [
  { key: "rendering", label: "Rendering config" },
  { key: "restarting", label: "Restarting container" },
  { key: "waiting_healthy", label: "Waiting for agent health" },
  { key: "done", label: "Done" },
];

interface Props {
  open: boolean;
  onClose: () => void;
  agent: Agent;
  /** Target runtime slug or id. */
  targetRuntimeId: string | null;
  /** Optional callback fired with the SwitchResult on success. */
  onSwitched?: (result: RuntimeSwitchPreview | null) => void;
  /** Mutation function — caller controls retry/error display via this fn.
   *  Must throw on failure so we can surface the message. */
  onConfirm: (params: { force_when_in_progress: boolean }) => Promise<RuntimeSwitchPreview | null>;
}

const RUNTIME_TYPE_COLOR: Record<string, string> = {
  lmstudio: C.info,          // #2E6FD8 — local API, info-blue
  vllm_docker: C.online,     // #2B9A4A — running container, online-green
  unsloth: C.warning,        // #B8870A — fine-tune, warm-amber
  openai_compatible: C.accent, // teal — was lila #A855F7, migrated
  cloud: C.textDim,          // #6E6E6E — external, neutral
  // Phase 24 (Hermes) — mirror of RuntimePill.RUNTIME_TYPE_COLOR. Single-SoT
  // extraction is deferred to v0.9 per 24-CONTEXT.md L-F.
  hermes: C.accentHover, // hermes — helle Teal-Stufe
};

function MiniRuntimeChip({ runtime }: { runtime: RuntimeSwitchSummary }) {
  const color = RUNTIME_TYPE_COLOR[runtime.runtime_type] ?? C.textDim;
  return (
    <span
      className="inline-flex items-center gap-2 font-mono text-[11px] px-2 py-1 rounded-md"
      style={{
        backgroundColor: `${color}14`,
        color: "var(--color-text-secondary)",
        border: `1px solid ${color}33`,
      }}
    >
      <span className="w-1.5 h-1.5 rounded-full" style={{ backgroundColor: color }} />
      {runtime.display_name}
      {runtime.model_identifier && (
        <span className="text-[var(--color-text-muted)]">· {runtime.model_identifier}</span>
      )}
    </span>
  );
}

export function RuntimeSwitchModal({
  open,
  onClose,
  agent,
  targetRuntimeId,
  onConfirm,
  onSwitched,
}: Props) {
  const [forceWhenInProgress, setForceWhenInProgress] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [longSwitch, setLongSwitch] = useState(false);
  const [completed, setCompleted] = useState<RuntimeSwitchPreview | null>(null);
  const longSwitchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Dry-run preview — runs as soon as the modal opens.
  const { data: preview, isLoading: previewLoading, error: previewError } = useQuery({
    queryKey: ["runtime-switch-preview", agent.id, targetRuntimeId],
    queryFn: () =>
      api.agents.previewRuntimeSwitch(agent.id, {
        runtime_id: targetRuntimeId!,
        force_when_in_progress: true, // preview always allowed; we gate at submit
      }),
    enabled: open && !!targetRuntimeId,
    staleTime: 0,  // D-07: jeder Modal-Open triggert frischen Probe
    retry: false,
  });

  // Live switch progress — polled only while a submit is in flight.
  const { data: progress } = useQuery({
    queryKey: ["runtime-switch-progress", agent.id],
    queryFn: () => api.agents.runtimeSwitchProgress(agent.id),
    enabled: open && submitting,
    refetchInterval: 1_500,
  });

  useEffect(() => {
    if (!open) {
      setForceWhenInProgress(false);
      setSubmitting(false);
      setError(null);
      setLongSwitch(false);
      setCompleted(null);
      if (longSwitchTimer.current) {
        clearTimeout(longSwitchTimer.current);
        longSwitchTimer.current = null;
      }
    }
  }, [open]);

  const isBusy = !!agent.current_task_id;
  const previewErrMsg = previewError ? (previewError as Error).message : null;

  // Phase 24 (Hermes) — single_instance runtimes cannot be the source or
  // target of a switch. Backend (plan 03) is authoritative; this gates UX.
  const targetLocked = preview?.new_runtime?.single_instance === true;
  const sourceLocked = preview?.old_runtime?.single_instance === true;
  const singleInstanceBlocked = targetLocked || sourceLocked;

  const handleSubmit = async () => {
    setSubmitting(true);
    setError(null);
    longSwitchTimer.current = setTimeout(() => setLongSwitch(true), 10_000);
    try {
      const result = await onConfirm({ force_when_in_progress: forceWhenInProgress });
      onSwitched?.(result ?? null);
      setCompleted(result ?? preview ?? null);
    } catch (e) {
      setError((e as Error).message ?? "Switch failed");
    } finally {
      setSubmitting(false);
      if (longSwitchTimer.current) {
        clearTimeout(longSwitchTimer.current);
        longSwitchTimer.current = null;
      }
      setLongSwitch(false);
    }
  };

  // iOS-safe scroll lock (M4)
  useBodyScrollLock(open);

  return (
    <AnimatePresence>
      {open && (
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          exit={{ opacity: 0 }}
          transition={{ duration: 0.15 }}
          className="fixed inset-0 z-50 flex items-end sm:items-center justify-center sm:p-4 bg-black/60"
          style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
          onClick={() => !submitting && onClose()}
        >
          {/* Drag indicator — mobile only */}
          <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full pointer-events-none" style={{ backgroundColor: "rgba(255,255,255,0.18)" }} />

          <motion.div
            initial={{ opacity: 0, y: 32 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 32 }}
            transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
            className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:max-w-lg sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
            onClick={(e) => e.stopPropagation()}
            style={{
              backgroundColor: "var(--color-bg-elevated)",
              border: "1px solid rgba(255,255,255,0.08)",
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
              {/* Header */}
              <div className="flex items-center justify-between p-5 border-b shrink-0" style={{ borderColor: "rgba(255,255,255,0.06)" }}>
                <div className="flex items-center gap-2">
                  <RotateCcw size={16} style={{ color: C.accent }} />
                  <h2 className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
                    Switch runtime — {agent.name}
                  </h2>
                </div>
                <button
                  onClick={() => !submitting && onClose()}
                  disabled={submitting}
                  className="p-1 rounded-md hover:bg-[rgba(255,255,255,0.06)] cursor-pointer disabled:cursor-not-allowed"
                >
                  <X size={14} style={{ color: "var(--color-text-muted)" }} />
                </button>
              </div>

              {completed !== null ? (
                <div className="flex flex-col items-center gap-2 py-10 px-5">
                  <span className="text-sm font-medium" style={{ color: STATUS_TEXT.online }}>
                    ✓ Switch complete
                  </span>
                  <span className="text-sm text-center" style={{ color: C.textSecondary }}>
                    {agent.name} now runs{" "}
                    {completed.new_runtime.model_identifier ?? completed.new_runtime.display_name} on{" "}
                    {completed.new_runtime.display_name}
                  </span>
                  <button
                    onClick={() => {
                      setCompleted(null);
                      onClose();
                    }}
                    className="mt-3 text-[12px] px-4 py-1.5 rounded-lg cursor-pointer transition-all"
                    style={{ backgroundColor: C.accent, color: C.textPrimary }}
                  >
                    Close
                  </button>
                </div>
              ) : (
                <>
              {/* Body */}
              <div className="p-5 space-y-4 overflow-y-auto flex-1">
                {/* Side-by-side runtime preview */}
                {previewLoading && (
                  <div className="flex items-center gap-2 text-[12px] text-[var(--color-text-muted)]">
                    <Loader2 size={12} className="animate-spin" />
                    Checking compatibility…
                  </div>
                )}
                {previewErrMsg && (
                  <div
                    className="flex items-start gap-2 p-3 rounded-lg text-[12px]"
                    style={{
                      backgroundColor: "rgba(239,68,68,0.08)",
                      border: "1px solid rgba(239,68,68,0.25)",
                      color: STATUS_TEXT.error,
                    }}
                  >
                    <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                    <div>
                      <div className="font-medium">Switch blocked</div>
                      <div className="opacity-80">{previewErrMsg}</div>
                    </div>
                  </div>
                )}
                {preview && (
                  <div className="space-y-3">
                    <div className="flex items-center gap-3 flex-wrap">
                      {preview.old_runtime ? (
                        <MiniRuntimeChip runtime={preview.old_runtime} />
                      ) : (
                        <span className="font-mono text-[11px] text-[var(--color-text-muted)]">— no runtime set —</span>
                      )}
                      <span
                        className={singleInstanceBlocked ? "opacity-50 cursor-not-allowed" : ""}
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        →
                      </span>
                      <span
                        className={singleInstanceBlocked ? "opacity-50 cursor-not-allowed" : ""}
                        aria-disabled={singleInstanceBlocked || undefined}
                      >
                        <MiniRuntimeChip runtime={preview.new_runtime} />
                      </span>
                    </div>

                    {/* Single-instance lock banner (Phase 24 / D-10) */}
                    {singleInstanceBlocked && (
                      <div
                        className="flex items-start gap-2 p-3 rounded-lg text-[12px]"
                        style={{
                          backgroundColor: "rgba(20,184,166,0.08)",
                          border: "1px solid rgba(20,184,166,0.28)",
                          color: C.accentHover,
                        }}
                        data-testid="single-instance-lock-banner"
                      >
                        <Lock size={13} className="mt-0.5 shrink-0" />
                        <div>
                          <div className="font-medium">
                            Single-instance runtime — switch not possible
                          </div>
                          <div className="opacity-80">
                            {targetLocked
                              ? "Target runtime is single-instance and doesn't accept additional agents."
                              : "Source runtime is single-instance and can't be released."}
                          </div>
                        </div>
                      </div>
                    )}

                    {/* Image-switch banner */}
                    {preview.image_switched && (
                      <div
                        className="flex items-start gap-2 p-3 rounded-lg text-[12px]"
                        style={{
                          backgroundColor: "rgba(249,115,22,0.08)",
                          border: "1px solid rgba(249,115,22,0.25)",
                          color: STATUS_TEXT.warning,
                        }}
                      >
                        <Box size={13} className="mt-0.5 shrink-0" />
                        <div>
                          <div className="font-medium">Container image will change</div>
                          <div className="opacity-80">
                            Switching between claude-code and openclaude — the container is
                            <code className="font-mono mx-1">--force-recreate</code>
                            rebuilt. Expected duration ~30–90s.
                          </div>
                        </div>
                      </div>
                    )}

                    {/* Compatibility warnings */}
                    {preview.warnings.length > 0 && (
                      <div
                        className="p-3 rounded-lg text-[12px] space-y-1"
                        style={{
                          backgroundColor: "rgba(255,178,36,0.08)",
                          border: "1px solid rgba(255,178,36,0.25)",
                          color: STATUS_TEXT.warning,
                        }}
                      >
                        <div className="flex items-center gap-1.5 font-medium">
                          <AlertTriangle size={12} />
                          Notes
                        </div>
                        <ul className="list-disc pl-5 opacity-90">
                          {preview.warnings.map((w, i) => (
                            <li key={i}>{w}</li>
                          ))}
                        </ul>
                      </div>
                    )}
                  </div>
                )}

                {/* In-progress force toggle */}
                {isBusy && (
                  <div
                    className="p-3 rounded-lg text-[12px] space-y-2"
                    style={{
                      backgroundColor: "rgba(239,68,68,0.06)",
                      border: "1px solid rgba(239,68,68,0.22)",
                      color: STATUS_TEXT.error,
                    }}
                  >
                    <div className="flex items-center gap-1.5 font-medium">
                      <Zap size={12} />
                      Agent is working on an active task
                    </div>
                    <div className="opacity-80">
                      <span className="font-mono">{agent.current_task_id?.slice(0, 8)}</span> is running.
                      Switch without force returns 409. The active session will be lost.
                    </div>
                    <label className="flex items-center gap-2 cursor-pointer pt-1">
                      <input
                        type="checkbox"
                        checked={forceWhenInProgress}
                        onChange={(e) => setForceWhenInProgress(e.target.checked)}
                        className="w-3.5 h-3.5 cursor-pointer"
                      />
                      <span>Switch anyway (force)</span>
                    </label>
                  </div>
                )}

                {/* Live progress stepper */}
                {submitting && progress?.step !== "rolled_back" && (
                  <div className="space-y-1.5" data-testid="switch-progress-stepper">
                    {SWITCH_STEPS.map((step, i) => {
                      const activeIdx = SWITCH_STEPS.findIndex((s) => s.key === progress?.step);
                      const isDone = activeIdx >= 0 && i < activeIdx;
                      const isActive = i === activeIdx;
                      return (
                        <div
                          key={step.key}
                          className="flex items-center gap-2 text-[12px]"
                          style={{
                            color: isDone
                              ? STATUS_TEXT.online
                              : isActive
                                ? C.accent
                                : C.textSecondary,
                          }}
                        >
                          {isDone ? (
                            <Check size={12} style={{ color: STATUS.online }} />
                          ) : isActive ? (
                            <Loader2 size={12} className="animate-spin" />
                          ) : (
                            <span className="w-3 h-3 inline-block" />
                          )}
                          {step.label}
                        </div>
                      );
                    })}
                  </div>
                )}

                {/* Rolled-back banner */}
                {progress?.step === "rolled_back" && (
                  <div
                    className="flex items-start gap-2 p-3 rounded-lg text-[12px]"
                    style={{
                      backgroundColor: "rgba(239,68,68,0.08)",
                      border: "1px solid rgba(239,68,68,0.25)",
                      color: STATUS_TEXT.error,
                    }}
                    data-testid="rolled-back-banner"
                  >
                    <AlertTriangle size={13} className="mt-0.5 shrink-0" />
                    <div>
                      <div className="font-medium">Rolled back to previous runtime</div>
                      {progress.error && <div className="opacity-80">{progress.error}</div>}
                    </div>
                  </div>
                )}

                {/* Long-switch hint */}
                {longSwitch && (
                  <div className="flex items-center gap-2 text-[11px] text-[var(--color-text-muted)]">
                    <Loader2 size={12} className="animate-spin" />
                    Rebuilding container, ~60s…
                  </div>
                )}

                {/* Submit error */}
                {error && (
                  <div
                    className="p-3 rounded-lg text-[12px]"
                    style={{
                      backgroundColor: "rgba(239,68,68,0.08)",
                      border: "1px solid rgba(239,68,68,0.25)",
                      color: STATUS_TEXT.error,
                    }}
                  >
                    {error}
                  </div>
                )}
              </div>

              {/* Footer */}
              <div className="flex items-center justify-end gap-2 p-4 border-t shrink-0" style={{ borderColor: "rgba(255,255,255,0.06)", paddingBottom: "calc(env(safe-area-inset-bottom) + 1rem)" }}>
                <button
                  onClick={onClose}
                  disabled={submitting}
                  className="text-[12px] px-3 py-1.5 rounded-lg cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.04)] disabled:cursor-not-allowed disabled:opacity-50"
                  style={{ color: "var(--color-text-secondary)" }}
                >
                  Cancel
                </button>
                <button
                  onClick={handleSubmit}
                  disabled={
                    submitting ||
                    !!previewErrMsg ||
                    singleInstanceBlocked ||
                    (isBusy && !forceWhenInProgress)
                  }
                  title={
                    singleInstanceBlocked
                      ? "Single-instance runtime — switch not possible"
                      : undefined
                  }
                  className="flex items-center gap-1.5 text-[12px] px-3 py-1.5 rounded-lg cursor-pointer transition-all disabled:cursor-not-allowed disabled:opacity-40"
                  style={{ backgroundColor: C.accent, color: C.textPrimary }}
                >
                  {submitting ? <Loader2 size={12} className="animate-spin" /> : <RotateCcw size={12} />}
                  Switch
                </button>
              </div>
                </>
              )}
          </motion.div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}
