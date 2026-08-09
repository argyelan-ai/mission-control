"use client";

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Play,
  Power,
  Square,
  RotateCcw,
  RefreshCw,
  Loader2,
  Settings2,
  X,
  Pencil,
  Check,
  Plug,
  type LucideIcon,
} from "lucide-react";
import Link from "next/link";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { Runtime, RuntimeLiveStatus } from "@/lib/types";
import { SlideOverPanel } from "@/components/shared/SlideOverPanel";
import { BindAgentModal } from "@/components/shared/BindAgentModal";
import { SparkRecipeSwitcher } from "@/components/shared/SparkRecipeSwitcher";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { panelCapabilities } from "./grouping";
import { typeLabel } from "./RuntimeListCard";
import { ContextSettingsPanel, loadStoredCtx, fmtCtx } from "./ContextSettings";
import { AutostartToggle } from "./AutostartToggle";

// ── Section divider ────────────────────────────────────────────────────────

function SectionDivider({ label }: { label: string }) {
  return (
    <div className="px-4 pt-4 pb-1.5">
      <span
        className="text-[10px] font-medium uppercase"
        style={{ color: C.textMuted, letterSpacing: "0.08em" }}
      >
        {label}
      </span>
    </div>
  );
}

// ── Action Button (ported verbatim from old page.tsx) ────────────────────────

function ActionButton({
  icon: Icon,
  label,
  disabled,
  onClick,
  loading,
  variant,
}: {
  icon: LucideIcon;
  label: string;
  disabled: boolean;
  onClick: () => void;
  loading: boolean;
  variant: "success" | "danger" | "default";
}) {
  const colors = {
    success: { bg: `${C.online}14`, border: `${C.online}33`, text: C.online },
    danger: { bg: `${C.error}14`, border: `${C.error}33`, text: C.error },
    default: { bg: C.borderSubtle, border: C.borderSubtle, text: C.textMuted },
  };
  const c = colors[variant];

  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={label}
      className="flex items-center justify-center w-7 h-7 rounded-lg transition-all cursor-pointer disabled:cursor-not-allowed"
      style={{
        background: disabled ? "transparent" : c.bg,
        border: `1px solid ${disabled ? "transparent" : c.border}`,
        color: disabled ? C.borderActive : c.text,
      }}
    >
      {loading ? <Loader2 size={12} className="animate-spin" /> : <Icon size={12} />}
    </button>
  );
}

// ── Inline model editor (ported from old RuntimeModelEditor) ─────────────────

function RuntimeModelEditor({
  runtime,
  onMessage,
}: {
  runtime: Runtime;
  onMessage?: (msg: string) => void;
}) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [value, setValue] = useState(runtime.model_identifier ?? "");

  const mutation = useMutation({
    mutationFn: (model: string) =>
      api.runtimes.db.update(runtime.slug ?? runtime.id, { model_identifier: model }),
    onSuccess: (data) => {
      setEditing(false);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
      onMessage?.(
        `Model set to "${data.model_identifier ?? "—"}" — bound agents will be restarted on the next watcher tick.`
      );
    },
    onError: () => onMessage?.("Model update failed."),
  });

  const save = () => {
    const trimmed = value.trim();
    if (!trimmed || trimmed === (runtime.model_identifier ?? "")) {
      setEditing(false);
      return;
    }
    mutation.mutate(trimmed);
  };

  const cancel = () => {
    setValue(runtime.model_identifier ?? "");
    setEditing(false);
  };

  const iconBtn = (color: string) => ({
    padding: "3px",
    borderRadius: "5px",
    background: "transparent" as const,
    border: "1px solid transparent",
    color,
    cursor: "pointer" as const,
    display: "flex" as const,
    alignItems: "center" as const,
  });

  if (editing) {
    return (
      <div className="flex items-center gap-1.5">
        <span className="text-xs shrink-0" style={{ color: C.textMuted }}>
          Model:
        </span>
        <input
          autoFocus
          aria-label="Model identifier"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") save();
            if (e.key === "Escape") cancel();
          }}
          className="font-mono text-xs px-1.5 py-1 rounded min-w-0 flex-1"
          style={{
            background: C.bgDeep,
            border: `1px solid ${C.borderAccent}`,
            color: C.textPrimary,
            outline: "none",
          }}
        />
        <button
          onClick={save}
          disabled={mutation.isPending}
          title="Save"
          aria-label="Save"
          style={iconBtn(C.accent)}
        >
          {mutation.isPending ? (
            <Loader2 size={13} className="animate-spin" />
          ) : (
            <Check size={13} />
          )}
        </button>
        <button
          onClick={cancel}
          disabled={mutation.isPending}
          title="Cancel"
          aria-label="Cancel"
          style={iconBtn(C.textMuted)}
        >
          <X size={13} />
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-1.5">
      <span className="text-xs shrink-0" style={{ color: C.textMuted }}>
        Model:
      </span>
      <span
        className="font-mono text-xs truncate"
        style={{ color: C.textSecondary }}
        title={runtime.model_identifier ?? undefined}
      >
        {runtime.model_identifier ?? "—"}
      </span>
      <button
        onClick={() => {
          setValue(runtime.model_identifier ?? "");
          setEditing(true);
        }}
        title="Edit model"
        aria-label="Edit model"
        style={iconBtn(C.textMuted)}
      >
        <Pencil size={12} />
      </button>
    </div>
  );
}

// ── Bound agents section (ported from old BoundAgentsFooter) ─────────────────

function AgentsSection({ runtime }: { runtime: Runtime }) {
  const [bindOpen, setBindOpen] = useState(false);
  const slug = runtime.slug ?? runtime.id;
  const queryClient = useQueryClient();

  const { data, isLoading } = useQuery({
    queryKey: ["runtimes", slug, "agents"],
    queryFn: () => api.runtimes.db.agents(slug),
    enabled: !!slug,
    staleTime: 15_000,
    retry: false,
  });

  const bound = data?.agents ?? [];
  const anyPending = bound.some((a) => a.pending_runtime_sync);

  const syncNowMutation = useMutation({
    mutationFn: () => api.runtimes.db.syncAgents(slug),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["runtimes", slug, "agents"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
  });

  return (
    <>
      <SectionDivider label="Agents" />
      <div className="px-4 pb-2 flex items-center gap-2 flex-wrap">
        {isLoading && <Loader2 size={11} className="animate-spin" style={{ color: C.textMuted }} />}
        {!isLoading && bound.length === 0 && (
          <span className="text-[11px]" style={{ color: C.textMuted }}>
            none — unbound
          </span>
        )}
        {bound.map((a) => (
          <span key={a.id} className="inline-flex items-center gap-1">
            <Link
              href={`/agents/${a.id}`}
              className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md font-mono text-[10px] hover:bg-[var(--color-bg-hover)] transition-colors cursor-pointer"
              style={{
                backgroundColor: C.accentSubtle,
                color: C.textSecondary,
                border: `1px solid ${C.borderAccent}`,
              }}
              title={`${a.name} · ${a.agent_runtime}`}
            >
              <EntityIcon value="🤖" size={13} className="inline-block align-[-2px] mr-1" />{a.name}
            </Link>
            {a.pending_runtime_sync && (
              <span
                className="rounded px-1 text-[10px]"
                style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                title="Model changed while busy — auto-syncs after its current task"
              >
                pending sync
              </span>
            )}
          </span>
        ))}
        <button
          onClick={() => setBindOpen(true)}
          className="ml-auto inline-flex items-center gap-1 px-1.5 py-0.5 rounded-md text-[10px] cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
          style={{
            color: C.accent,
            border: `1px dashed ${C.borderAccent}`,
          }}
        >
          <Plug size={10} />
          Bind Agent
        </button>
      </div>

      {anyPending && (
        <div
          className="px-4 pb-2 flex items-center gap-2 text-[11px]"
          style={{ color: C.textSecondary }}
        >
          <span>Model sync pending — runs automatically when the agent is idle.</span>
          <button
            onClick={() => syncNowMutation.mutate()}
            className="underline cursor-pointer"
            style={{ color: STATUS_TEXT.warning }}
            title="Force sync NOW — interrupts a running task"
          >
            Sync now (force)
          </button>
        </div>
      )}

      <BindAgentModal open={bindOpen} onClose={() => setBindOpen(false)} runtime={runtime} />
    </>
  );
}

// ── Header state dot color ────────────────────────────────────────────────────

function dotColor(state: string): string {
  if (state === "ready") return STATUS.online;
  if (state === "starting") return C.info;
  if (state === "warming") return C.warning;
  if (state === "failed") return C.error;
  return STATUS.offline;
}

const STATE_LABELS: Record<string, string> = {
  ready: "Ready",
  warming: "Warmup…",
  starting: "Starting…",
  stopped: "Stopped",
  failed: "Error",
  unknown: "Unknown",
};

// ── Panel ──────────────────────────────────────────────────────────────────

export function RuntimeDetailPanel({
  runtime,
  live,
  open,
  onClose,
}: {
  runtime: Runtime | null;
  live?: RuntimeLiveStatus;
  open: boolean;
  onClose: () => void;
}) {
  return (
    <SlideOverPanel open={open && runtime != null} onClose={onClose} title={runtime?.display_name} desktopWidth="440px">
      {runtime && <RuntimeDetailBody key={runtime.id} runtime={runtime} live={live} />}
    </SlideOverPanel>
  );
}

function RuntimeDetailBody({ runtime, live }: { runtime: Runtime; live?: RuntimeLiveStatus }) {
  const queryClient = useQueryClient();
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [settingsOpen, setSettingsOpen] = useState(false);

  const caps = panelCapabilities(runtime);
  const isLmStudio = runtime.runtime_type === "lmstudio";
  const lmsKey = runtime.lms_identifier ?? runtime.id;
  const [storedCtx, setStoredCtx] = useState<number | null>(() =>
    isLmStudio ? loadStoredCtx(lmsKey) : null
  );

  const effectiveState = runtime.state ?? "unknown";
  const canStart = effectiveState === "stopped";
  const canStop = effectiveState !== "stopped";

  // Power-managed runtime (unsloth_porsche): box sleeps when idle. The backend
  // reports container_status "asleep" (:5555 down), "booted_no_model" (box awake,
  // model not serving) or "serving" (ready). WoL only wakes the box; the model is
  // loaded on demand via Start.
  const isPowerManaged = runtime.power_managed === true;
  const isAsleep = isPowerManaged && runtime.container_status === "asleep";
  const isBootedNoModel = isPowerManaged && runtime.container_status === "booted_no_model";

  const invalidate = () => queryClient.invalidateQueries({ queryKey: ["runtimes"] });

  const startMutation = useMutation({
    mutationFn: () => api.runtimes.start(runtime.id, storedCtx ?? undefined),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Start failed."),
  });

  const stopMutation = useMutation({
    mutationFn: () => api.runtimes.stop(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Stop failed."),
  });

  const restartMutation = useMutation({
    mutationFn: () => api.runtimes.restart(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Restart failed."),
  });

  const wakeMutation = useMutation({
    mutationFn: () => api.runtimes.wake(runtime.id),
    onSuccess: (data) => { setActionMsg(data.message); invalidate(); },
    onError: () => setActionMsg("Wake failed."),
  });

  const probeMutation = useMutation({
    mutationFn: () => api.runtimes.probeModel(runtime.id),
    onSuccess: (data) => {
      const msg = data.changed
        ? `Model: ${data.old_model_identifier ?? "—"} → ${data.new_model_identifier}`
        : `Model unchanged: ${data.new_model_identifier ?? "—"}`;
      setActionMsg(msg);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["agents"] });
    },
    onError: () => setActionMsg("Probe failed."),
  });

  const isMutating =
    startMutation.isPending || stopMutation.isPending || restartMutation.isPending ||
    probeMutation.isPending || wakeMutation.isPending;

  return (
    <div className="flex flex-col">
      {/* Header meta line */}
      <div className="px-4 pt-4 pb-1 flex items-center gap-2">
        <span
          className="w-1.5 h-1.5 rounded-full shrink-0"
          style={{ background: dotColor(effectiveState) }}
        />
        <span className="text-xs" style={{ color: C.textSecondary }}>
          {STATE_LABELS[effectiveState] ?? "Unknown"}
        </span>
        <span style={{ color: C.borderSubtle }}>·</span>
        <span className="text-xs truncate" style={{ color: C.textMuted }}>
          {typeLabel(runtime.runtime_type)}
          {runtime.host && ` · ${runtime.host.display_name}`}
        </span>
      </div>

      {isAsleep && (
        <div className="px-4 pb-1 text-xs" style={{ color: C.textDim }}>
          Sleeping
        </div>
      )}
      {isBootedNoModel && (
        <div className="px-4 pb-1 text-xs" style={{ color: STATUS_TEXT.warning }}>
          Awake — model not loaded (Start)
        </div>
      )}

      {/* Control section */}
      {caps.lifecycle && (
        <>
          <SectionDivider label="Control" />
          <div className="px-4 pb-2 flex items-center gap-1.5">
            {caps.wake && (
              <ActionButton
                icon={Power}
                label="Wake"
                disabled={(!isAsleep && effectiveState === "ready") || isMutating}
                onClick={() => wakeMutation.mutate()}
                loading={wakeMutation.isPending}
                variant="success"
              />
            )}
            <ActionButton
              icon={Play}
              label="Start"
              disabled={!canStart || isMutating}
              onClick={() => startMutation.mutate()}
              loading={startMutation.isPending}
              variant="success"
            />
            <ActionButton
              icon={Square}
              label="Stop"
              disabled={!canStop || isMutating}
              onClick={() => stopMutation.mutate()}
              loading={stopMutation.isPending}
              variant="danger"
            />
            {runtime.runtime_type !== "lmstudio" && (
              <ActionButton
                icon={RotateCcw}
                label="Restart"
                disabled={!canStop || isMutating}
                onClick={() => restartMutation.mutate()}
                loading={restartMutation.isPending}
                variant="default"
              />
            )}
          </div>
        </>
      )}

      {/* Model section */}
      <SectionDivider label="Model" />
      <div className="px-4 pb-3 flex flex-col gap-2">
        {caps.modelEditor && (
          <RuntimeModelEditor runtime={runtime} onMessage={setActionMsg} />
        )}
        {!caps.modelEditor && (
          <div className="flex items-center gap-1.5">
            <span className="text-xs shrink-0" style={{ color: C.textMuted }}>
              Model:
            </span>
            <span className="font-mono text-xs truncate" style={{ color: C.textSecondary }}>
              {runtime.model_identifier ?? "—"}
            </span>
          </div>
        )}

        {live && (
          <div className="flex items-center gap-2 text-xs" style={{ color: C.textSecondary }}>
            <span
              className="inline-block h-1.5 w-1.5 rounded-full shrink-0"
              style={{ background: live.reachable ? STATUS.online : STATUS.error }}
            />
            {live.reachable ? (
              <>
                <span className="truncate" title={live.served_model ?? undefined}>
                  Engine serves: {live.served_model ?? "—"}
                </span>
                {live.drift && (
                  <span
                    className="rounded px-1.5 py-0.5 text-[10px] font-medium shrink-0"
                    style={{ color: STATUS_TEXT.warning, border: `1px solid ${STATUS.warning}` }}
                    title={`Registry says ${runtime.model_identifier ?? "—"} — will sync on the next watcher tick`}
                  >
                    Drift
                  </span>
                )}
              </>
            ) : (
              <span style={{ color: STATUS_TEXT.error }}>
                Engine unreachable ({live.consecutive_failures} probes)
              </span>
            )}
          </div>
        )}

        <div className="flex items-center gap-1.5 flex-wrap">
          {caps.probe && (
            <ActionButton
              icon={RefreshCw}
              label="Re-probe model"
              disabled={isMutating}
              onClick={() => probeMutation.mutate()}
              loading={probeMutation.isPending}
              variant="default"
            />
          )}
          {caps.recipeSwitcher && <SparkRecipeSwitcher runtimeId={runtime.id} />}
          {caps.contextSettings && (
            <button
              onClick={() => setSettingsOpen((v) => !v)}
              title="Context settings"
              aria-label="Context settings"
              style={{
                padding: "4px",
                borderRadius: "6px",
                background: settingsOpen ? C.accentSubtle : "transparent",
                border: `1px solid ${settingsOpen ? C.borderAccent : "transparent"}`,
                color: settingsOpen ? C.accent : C.textMuted,
                cursor: "pointer",
                display: "flex",
                alignItems: "center",
                transition: "all 0.15s",
              }}
            >
              <Settings2 size={13} />
              {storedCtx && (
                <span className="ml-1 text-[10px] tabular-nums" style={{ color: C.online }}>
                  {fmtCtx(storedCtx)}
                </span>
              )}
            </button>
          )}
        </div>

        {caps.contextSettings && settingsOpen && (
          <ContextSettingsPanel
            modelId={lmsKey}
            initialCtx={storedCtx}
            onClose={() => {
              setStoredCtx(loadStoredCtx(lmsKey));
              setSettingsOpen(false);
            }}
          />
        )}
      </div>

      {/* Automation section */}
      {caps.autostart && (
        <>
          <SectionDivider label="Automation" />
          <div className="px-4 pb-3">
            <AutostartToggle slug={runtime.slug ?? runtime.id} />
          </div>
        </>
      )}

      {/* Mutation feedback */}
      {actionMsg && (
        <div
          className="text-xs mx-4 mb-3 px-3 py-2 rounded-lg"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.textSecondary,
          }}
        >
          {actionMsg}
        </div>
      )}

      {/* Agents section — always visible */}
      <AgentsSection runtime={runtime} />
    </div>
  );
}
