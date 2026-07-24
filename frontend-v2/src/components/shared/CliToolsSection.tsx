"use client";

/**
 * CLI-Tools-Sektion auf /runtimes (Feature „CLI-Tool-Updates", Task 8).
 *
 * Zeigt die im Fleet festgebackenen CLI-Werkzeuge (openclaude / claude / omp),
 * die installierte gegen die neueste Version, welche Agents ein Update mitbekommen,
 * und fährt einen Update-Lauf (Manifest → Build → Recreate) mit Live-Log über den
 * globalen /api/v1/cli-tools/update-status-Endpoint.
 *
 * Rollen: kein Client-seitiges Gating — der Update-Button ist immer sichtbar,
 * ein 403 vom Backend landet als Toast (gleiches Muster wie die Runtime-Aktionen).
 */

import { useEffect, useRef, useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import {
  RotateCcw,
  Loader2,
  ArrowUpCircle,
  CheckCircle2,
  AlertCircle,
  X,
} from "lucide-react";
import { api } from "@/lib/api";
import type { CliToolStatus, CliUpdatePhase, CliUpdateProgress } from "@/lib/types";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { useNotificationStore } from "@/lib/store";
import { timeAgo } from "@/lib/utils";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

// ── Phase model ───────────────────────────────────────────────────────────────
// The build pipeline moves manifest → build → recreate → done|failed. "idle"
// means no update is in flight. Only the three middle phases keep the poller hot.

const RUNNING_PHASES: CliUpdatePhase[] = ["manifest", "build", "recreate"];
const PHASE_STEPS: { key: CliUpdatePhase; label: string }[] = [
  { key: "manifest", label: "Manifest" },
  { key: "build", label: "Build" },
  { key: "recreate", label: "Recreate" },
];
// Host-Tools (grok): brew upgrade statt Image-Build, keine Recreate-Phase —
// der Runner meldet die brew-Phase als "build".
const HOST_PHASE_STEPS: { key: CliUpdatePhase; label: string }[] = [
  { key: "manifest", label: "Manifest" },
  { key: "build", label: "Brew" },
];

function isRunning(phase: CliUpdateProgress["phase"]): boolean {
  return RUNNING_PHASES.includes(phase as CliUpdatePhase);
}

// ── Agent pills ───────────────────────────────────────────────────────────────

function AgentPills({ agents }: { agents: CliToolStatus["agents_affected"] }) {
  if (agents.length === 0) {
    return (
      <span className="text-[11px]" style={{ color: C.textMuted }}>
        keine Agents gebunden
      </span>
    );
  }
  return (
    <div className="flex flex-wrap items-center gap-1">
      {agents.map((a) => (
        <span
          key={a.id}
          title={a.busy ? "Agent beschäftigt — Update folgt nach Task-Ende" : a.name}
          className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md font-mono text-[10px]"
          style={{
            backgroundColor: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.textSecondary,
            opacity: a.busy ? 0.45 : 1,
          }}
        >
          🤖 {a.name}
        </span>
      ))}
    </div>
  );
}

// ── Tool card ─────────────────────────────────────────────────────────────────

function CliToolCard({
  tool,
  onUpdate,
}: {
  tool: CliToolStatus;
  onUpdate: (tool: CliToolStatus) => void;
}) {
  const running = isRunning(tool.build_state as CliUpdateProgress["phase"]);

  return (
    <div
      className="flex flex-col gap-3 p-3.5"
      style={{
        background: C.borderSubtle,
        border: `1px solid ${C.borderSubtle}`,
        borderRadius: "10px",
      }}
    >
      {/* Head: tool + image */}
      <div className="min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-sm truncate" style={{ color: C.textPrimary }}>
            {tool.tool}
          </span>
          {tool.update_available && !running && (
            <button
              onClick={() => onUpdate(tool)}
              title={`Auf ${tool.latest} aktualisieren`}
              className="shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium cursor-pointer transition-colors"
              style={{
                color: STATUS_TEXT.warning,
                border: `1px solid ${STATUS.warning}`,
                background: `${STATUS.warning}14`,
              }}
            >
              <ArrowUpCircle size={10} />
              Update {tool.latest}
            </button>
          )}
          {running && (
            <span
              className="shrink-0 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[10px] font-medium"
              style={{ color: C.accent, border: `1px solid ${C.borderAccent}`, background: C.accentSubtle }}
            >
              <Loader2 size={10} className="animate-spin" />
              Update läuft
            </span>
          )}
        </div>
        <div className="text-[11px] font-mono truncate mt-0.5" style={{ color: C.textMuted }} title={tool.image ?? "Host-CLI (brew)"}>
          {tool.image ?? "Host-CLI · brew"}
        </div>
      </div>

      {/* Version row */}
      <div className="flex items-baseline gap-2">
        <span className="text-[10px] uppercase tracking-wider" style={{ color: C.textDim, letterSpacing: "0.06em" }}>
          Ist
        </span>
        <span className="text-xs font-mono tabular-nums" style={{ color: C.textPrimary }}>
          {tool.installed ?? "—"}
        </span>
        {tool.update_available && tool.latest && (
          <>
            <span className="text-xs" style={{ color: C.textDim }}>→</span>
            <span className="text-xs font-mono tabular-nums" style={{ color: STATUS_TEXT.warning }}>
              {tool.latest}
            </span>
          </>
        )}
      </div>

      {/* Affected agents */}
      <div className="flex flex-col gap-1.5">
        <span className="text-[10px] uppercase tracking-wider" style={{ color: C.textDim, letterSpacing: "0.06em" }}>
          Betroffen
        </span>
        <AgentPills agents={tool.agents_affected} />
      </div>
    </div>
  );
}

// ── Phase step indicator ──────────────────────────────────────────────────────

function PhaseTrack({
  phase,
  steps = PHASE_STEPS,
}: {
  phase: CliUpdateProgress["phase"];
  steps?: { key: CliUpdatePhase; label: string }[];
}) {
  const activeIdx = steps.findIndex((s) => s.key === phase);
  // done/failed count all running phases as passed
  const terminal = phase === "done" || phase === "failed";
  return (
    <div className="flex items-center gap-1.5">
      {steps.map((step, i) => {
        const isActive = i === activeIdx;
        const isPast = terminal || (activeIdx >= 0 && i < activeIdx);
        const color = phase === "failed"
          ? (isActive || isPast ? C.error : C.textDim)
          : isActive
            ? C.accent
            : isPast
              ? C.online
              : C.textDim;
        return (
          <div key={step.key} className="flex items-center gap-1.5">
            <span
              className="inline-flex items-center gap-1 text-[11px] font-medium"
              style={{ color }}
            >
              {isActive && phase !== "done" && phase !== "failed" && (
                <Loader2 size={11} className="animate-spin" />
              )}
              {step.label}
            </span>
            {i < steps.length - 1 && (
              <span style={{ color: C.textDim }}>·</span>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Update modal ──────────────────────────────────────────────────────────────

type ModalStep = "confirm" | "running";

function UpdateModal({
  tool,
  progress,
  onClose,
}: {
  tool: CliToolStatus;
  progress: CliUpdateProgress | undefined;
  onClose: () => void;
}) {
  const addNotification = useNotificationStore((s) => s.addNotification);
  const [step, setStep] = useState<ModalStep>("confirm");
  useBodyScrollLock(true);

  const updateMutation = useMutation({
    mutationFn: () => api.cliTools.update(tool.tool),
    onSuccess: () => setStep("running"),
    onError: (err: Error) => {
      // 409 = ein anderes Update läuft bereits → trotzdem in den Fortschritt
      // wechseln, damit der Operator den laufenden Lauf mitverfolgt.
      if (err.message.includes("409")) {
        setStep("running");
        addNotification({ type: "warning", message: "Es läuft bereits ein Update — zeige den aktiven Lauf.", persistent: false });
      } else {
        addNotification({ type: "error", message: `Update fehlgeschlagen: ${err.message}`, persistent: false });
      }
    },
  });

  // Follow the global progress once we're in the running step.
  const phase = progress?.phase ?? "idle";
  const isDone = step === "running" && phase === "done";
  const isFailed = step === "running" && phase === "failed";

  const busyAgents = tool.agents_affected.filter((a) => a.busy);

  return (
    <AnimatePresence>
      <motion.div
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
        exit={{ opacity: 0 }}
        transition={{ duration: 0.15 }}
        className="fixed inset-0 z-40 flex items-end sm:items-center justify-center sm:p-4 bg-black/60"
        onClick={onClose}
        role="dialog"
        aria-modal="true"
        aria-label={`CLI-Tool ${tool.tool} aktualisieren`}
      >
        <motion.div
          initial={{ opacity: 0, y: 32 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: 32 }}
          transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
          className="relative w-full mx-2 rounded-t-2xl sm:mx-0 sm:max-w-lg sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
          onClick={(e) => e.stopPropagation()}
          style={{
            backgroundColor: C.bgElevated,
            border: `1px solid rgba(255,255,255,0.08)`,
            boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
          }}
        >
          {/* Header */}
          <div className="flex items-center justify-between p-5 border-b shrink-0" style={{ borderColor: C.border }}>
            <div className="min-w-0">
              <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
                {tool.tool} aktualisieren
              </h2>
              <div className="text-[11px] font-mono truncate mt-0.5" style={{ color: C.textMuted }}>
                {tool.image ?? "Host-CLI · brew"}
              </div>
            </div>
            <button
              onClick={onClose}
              aria-label="Schliessen"
              className="p-1 rounded-md cursor-pointer hover:bg-[rgba(255,255,255,0.06)]"
              style={{ color: C.textMuted }}
            >
              <X size={16} />
            </button>
          </div>

          <div className="p-5 overflow-y-auto">
            {step === "confirm" && (
              <div className="flex flex-col gap-4">
                {/* Version delta */}
                <div className="flex items-center gap-2 text-sm">
                  <span className="font-mono tabular-nums" style={{ color: C.textPrimary }}>
                    {tool.installed ?? "—"}
                  </span>
                  <span style={{ color: C.textDim }}>→</span>
                  <span className="font-mono tabular-nums" style={{ color: STATUS_TEXT.warning }}>
                    {tool.latest ?? "—"}
                  </span>
                </div>

                {/* Affected agents */}
                <div className="flex flex-col gap-1.5">
                  <span className="text-[10px] uppercase tracking-wider" style={{ color: C.textDim, letterSpacing: "0.06em" }}>
                    Betroffene Agents
                  </span>
                  <AgentPills agents={tool.agents_affected} />
                  {busyAgents.length > 0 && (
                    <span className="text-[11px]" style={{ color: STATUS_TEXT.warning }}>
                      {busyAgents.length} Agent(s) beschäftigt — folgen nach Task-Ende.
                    </span>
                  )}
                </div>

                {/* Manifest-commit hint */}
                <div
                  className="text-xs px-3 py-2 rounded-lg"
                  style={{ background: `${STATUS.warning}14`, border: `1px solid ${STATUS.warning}33`, color: C.textSecondary }}
                >
                  {tool.host ? (
                    <>
                      Host-CLI: Update läuft als <span className="font-mono">brew upgrade</span> auf
                      dem Mac. Laufende Sessions behalten das alte Binary bis zum nächsten
                      Session-Neustart.
                    </>
                  ) : (
                    <>
                      Hinweis: Die Manifest-Änderung (festgebackene Version) muss anschliessend
                      committet werden, sonst fällt der Stand beim nächsten Rebuild zurück.
                    </>
                  )}
                </div>

                <div className="flex items-center justify-end gap-2 pt-1">
                  <button
                    onClick={onClose}
                    className="text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
                    style={{ color: C.textSecondary, border: `1px solid ${C.border}`, background: "transparent" }}
                  >
                    Abbrechen
                  </button>
                  <button
                    onClick={() => updateMutation.mutate()}
                    disabled={updateMutation.isPending}
                    className="text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-colors disabled:opacity-50 disabled:cursor-not-allowed inline-flex items-center gap-1.5"
                    style={{ color: C.textPrimary, background: C.accent }}
                  >
                    {updateMutation.isPending ? <Loader2 size={12} className="animate-spin" /> : <ArrowUpCircle size={12} />}
                    Jetzt aktualisieren
                  </button>
                </div>
              </div>
            )}

            {step === "running" && (
              <div className="flex flex-col gap-4">
                {/* Status line */}
                {isDone ? (
                  <div className="flex items-center gap-2 text-sm" style={{ color: C.online }}>
                    <CheckCircle2 size={16} />
                    <span>
                      Update abgeschlossen
                      {progress?.to_version ? ` — jetzt ${progress.to_version}` : ""}.
                    </span>
                  </div>
                ) : isFailed ? (
                  <div className="flex items-center gap-2 text-sm" style={{ color: STATUS_TEXT.error }}>
                    <AlertCircle size={16} />
                    <span>Update fehlgeschlagen.</span>
                  </div>
                ) : (
                  <PhaseTrack phase={phase} steps={tool.host ? HOST_PHASE_STEPS : PHASE_STEPS} />
                )}

                {/* Failure reason */}
                {isFailed && progress?.error && (
                  <div
                    data-testid="cli-update-error"
                    className="text-xs px-3 py-2 rounded-lg"
                    style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
                  >
                    {progress.error}
                  </div>
                )}

                {/* Log tail */}
                {progress?.log_tail && (
                  <div className="flex flex-col gap-1.5">
                    <span className="text-[10px] uppercase tracking-wider" style={{ color: C.textDim, letterSpacing: "0.06em" }}>
                      Log
                    </span>
                    <pre
                      className="text-[11px] font-mono rounded-lg p-3 overflow-x-auto"
                      style={{
                        background: C.bgDeep,
                        border: `1px solid ${C.border}`,
                        color: C.textSecondary,
                        maxHeight: "220px",
                        overflowY: "auto",
                        whiteSpace: "pre",
                      }}
                    >
                      {progress.log_tail}
                    </pre>
                  </div>
                )}

                <div className="flex items-center justify-end pt-1">
                  <button
                    onClick={onClose}
                    className="text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-colors"
                    style={{
                      color: isDone || isFailed ? C.textPrimary : C.textSecondary,
                      border: `1px solid ${C.border}`,
                      background: isDone || isFailed ? C.borderSubtle : "transparent",
                    }}
                  >
                    {isDone || isFailed ? "Schliessen" : "Im Hintergrund weiterlaufen"}
                  </button>
                </div>
              </div>
            )}
          </div>
        </motion.div>
      </motion.div>
    </AnimatePresence>
  );
}

// ── Section ───────────────────────────────────────────────────────────────────

export function CliToolsSection() {
  const queryClient = useQueryClient();
  const addNotification = useNotificationStore((s) => s.addNotification);
  const [modalTool, setModalTool] = useState<string | null>(null);
  const handledRef = useRef<string | null>(null);

  const { data, isLoading, error } = useQuery({
    queryKey: ["cli-tools"],
    queryFn: () => api.cliTools.list(),
    refetchInterval: 30_000,
  });

  const { data: progress } = useQuery({
    queryKey: ["cli-tools", "update-status"],
    queryFn: () => api.cliTools.updateStatus(),
    // Keep the poller hot only while a build is in flight; TanStack passes the
    // live query so we read the freshest phase each tick.
    refetchInterval: (query) =>
      isRunning((query.state.data as CliUpdateProgress | undefined)?.phase ?? "idle") ? 3_000 : false,
  });

  const checkMutation = useMutation({
    mutationFn: () => api.cliTools.check(),
    onSuccess: (res) => {
      queryClient.setQueryData(["cli-tools"], res);
    },
    onError: (err: Error) =>
      addNotification({ type: "error", message: `Prüfen fehlgeschlagen: ${err.message}`, persistent: false }),
  });

  // When a run reaches a terminal phase, refresh the tool list once (versions
  // + build_state changed). Guard against re-firing on every 3s idle tick.
  useEffect(() => {
    const phase = progress?.phase;
    if (phase !== "done" && phase !== "failed") return;
    const token = `${progress?.tool ?? ""}:${phase}:${progress?.updated_at ?? ""}`;
    if (handledRef.current === token) return;
    handledRef.current = token;
    queryClient.invalidateQueries({ queryKey: ["cli-tools"] });
  }, [progress?.phase, progress?.tool, progress?.updated_at, queryClient]);

  const tools = data?.tools ?? [];
  const checkedAt = tools.find((t) => t.checked_at)?.checked_at ?? null;
  const openTool = modalTool ? tools.find((t) => t.tool === modalTool) ?? null : null;

  return (
    <div className="mt-8">
      {/* Section header — mirrors the vLLM/LM Studio headers on this page */}
      <div className="flex items-center gap-3 mb-4">
        <div
          className="w-px"
          style={{
            alignSelf: "stretch",
            background: `linear-gradient(to bottom, ${C.accent} 0%, transparent 100%)`,
            minHeight: "36px",
          }}
        />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
              CLI-Tools
            </h2>
            <span
              className="text-xs px-1.5 py-px rounded"
              style={{ color: C.textMuted, background: C.border, fontSize: "10px", letterSpacing: "0.06em" }}
            >
              Fleet
            </span>
          </div>
          <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>
            Festgebackene Agent-Werkzeuge · geprüft {timeAgo(checkedAt)}
          </p>
        </div>
        <button
          onClick={() => checkMutation.mutate()}
          disabled={checkMutation.isPending}
          className="shrink-0 flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg transition-all cursor-pointer disabled:opacity-50 disabled:cursor-not-allowed"
          style={{ color: C.textMuted, border: `1px solid ${C.borderSubtle}`, background: C.borderSubtle }}
        >
          {checkMutation.isPending ? <Loader2 size={11} className="animate-spin" /> : <RotateCcw size={11} />}
          Jetzt prüfen
        </button>
      </div>

      {isLoading && (
        <div className="flex items-center gap-2 py-2" style={{ color: C.textMuted }}>
          <Loader2 size={13} className="animate-spin" />
          <span className="text-xs">Lade CLI-Tools...</span>
        </div>
      )}

      {error && (
        <div
          className="flex items-center gap-2 text-xs px-4 py-3 rounded-xl"
          style={{ color: STATUS_TEXT.error, background: `${C.error}0F`, border: `1px solid ${C.error}26` }}
        >
          <AlertCircle size={13} />
          CLI-Tools konnten nicht geladen werden.
        </div>
      )}

      {!isLoading && !error && tools.length === 0 && (
        <div className="text-xs text-center py-10" style={{ color: C.textMuted }}>
          Keine CLI-Tools konfiguriert.
        </div>
      )}

      {tools.length > 0 && (
        <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
          {tools.map((tool) => (
            <CliToolCard key={tool.tool} tool={tool} onUpdate={(t) => setModalTool(t.tool)} />
          ))}
        </div>
      )}

      {openTool && (
        <UpdateModal
          tool={openTool}
          progress={progress}
          onClose={() => setModalTool(null)}
        />
      )}
    </div>
  );
}
