"use client";

/**
 * SlotStage — one GPU slot's "stage" (mockup M1, m1-slot-buehne.html).
 *
 * Renders a single host's occupancy: what's currently serving (or OFF),
 * live GPU/VRAM/temp telemetry, a way to switch to another sparkrun recipe
 * or start another lifecycle-capable runtime on the box, and a quiet ready
 * list of everything else that could take the slot.
 *
 * HONESTY RULE (hard, per task brief): only real fields are rendered —
 * no tok/s, no uptime, no ETA. The mockup shows all three; this component
 * deliberately does not reproduce them.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { Runtime, RuntimeLiveStatus, SparkrunRecipe } from "@/lib/types";
import { panelCapabilities, pickServing, type HostGroup } from "./grouping";
import { useGpuSparkline } from "./useGpuSparkline";
import { fmtCtx } from "@/lib/utils";
import { openModelsTab } from "./modelsTab";
import { EntityIcon } from "@/components/shared/EntityIcon";

// typeLabel copied from RuntimeDetailPanel.tsx (same list, same reasoning —
// no shared export exists yet, and this label text must not drift between
// the two runtime surfaces).
const TYPE_LABELS: Record<string, string> = {
  vllm_docker: "vLLM Docker", lmstudio: "LM Studio", unsloth: "Unsloth",
  unsloth_porsche: "Unsloth · PORSCHE", openai_compatible: "OpenAI-compatible",
  cloud: "Cloud API", hermes: "Hermes", grok: "Grok", kimi: "Kimi",
  omp: "OMP", llamacpp_docker: "llama.cpp", ssh_process: "SSH process",
};
const typeLabel = (t: string) => TYPE_LABELS[t] ?? t;
export { typeLabel };

const liveFor = (rt: Runtime, live?: Record<string, RuntimeLiveStatus>) => live?.[rt.slug ?? rt.id];

type SlotState = "serving" | "warmup" | "switching" | "off" | "failed";

const ACTIVE_DB_STATES = new Set(["ready", "warming", "starting"]);

// consecutive_failures below this threshold during an active DB state (ready/
// warming/starting) is just the watcher catching an engine that hasn't come
// up yet — not a failure. A normal warmup is unreachable by definition until
// the model finishes loading; flagging it FAILED on probe #1 was the bug.
const FAILURE_THRESHOLD = 3;

function slotState(rt: Runtime | null, l?: RuntimeLiveStatus): SlotState {
  if (!rt) return "off";
  // The backend only ever sets live.status="switching" (+ .phase) inside an
  // active recipe-switch/eviction grace window (runtime_watcher.py
  // _probe_one, the `switching is not None` branch) — a normal warmup never
  // reaches this. Kept wired up so it activates automatically if/when the
  // backend starts emitting it more broadly; currently a dead-but-harmless
  // path outside that window.
  if (l?.status === "switching") return "switching";
  // A reachable engine IS serving — the live probe outranks a stale registry
  // state (e.g. state "unknown"/"stopped" while the engine answers in 14 ms).
  if (l?.reachable === true) return "serving";
  const state = rt.state ?? "unknown";
  if (state === "failed") return "failed";
  if (ACTIVE_DB_STATES.has(state) && l?.reachable === false && (l?.consecutive_failures ?? 0) >= FAILURE_THRESHOLD) {
    return "failed";
  }
  if (state === "ready") return "serving";
  if (state === "warming" || state === "starting") return "warmup";
  return "off";
}

function StateChip({ state, phase }: { state: SlotState; phase?: string | null }) {
  const t = useTranslations("runtimes.slotStage");
  const config: Record<SlotState, { color: string; label: string }> = {
    serving: { color: STATUS.online, label: t("stateServing") },
    warmup: { color: STATUS_TEXT.warning, label: t("stateWarmup") },
    switching: { color: STATUS_TEXT.warning, label: t("stateSwitching", { phase: phase ?? "—" }) },
    off: { color: C.textDim, label: t("stateOff") },
    failed: { color: STATUS_TEXT.error, label: t("stateFailed") },
  };
  const c = config[state];
  return (
    <div className="flex items-center gap-2 text-[11px]" style={{ color: c.color }}>
      <span className="w-1.5 h-1.5 rounded-full shrink-0" style={{ background: c.color }} />
      {c.label}
    </div>
  );
}

// ── Telemetry meters ──────────────────────────────────────────────────────

function Meter({ label, value, pct }: { label: string; value: string; pct: number }) {
  return (
    <div>
      <div className="flex items-baseline justify-between mb-1" style={{ fontSize: "11px", color: C.textMuted }}>
        <span>{label}</span>
        <span className="font-semibold tabular-nums" style={{ color: C.textPrimary }}>{value}</span>
      </div>
      <div className="rounded-full overflow-hidden" style={{ height: "2px", background: C.bgElevated }}>
        <div
          style={{
            height: "100%",
            background: C.accent,
            width: `${Math.min(Math.max(pct, 0), 100)}%`,
            transition: "width 0.6s cubic-bezier(0.16,1,0.3,1)",
          }}
        />
      </div>
    </div>
  );
}

function TelemetryColumn({ hostId }: { hostId: string }) {
  const t = useTranslations("runtimes.slotStage");
  const { data, dataUpdatedAt } = useQuery({
    queryKey: ["hosts", hostId, "metrics"],
    queryFn: () => api.hosts.metrics(hostId),
    refetchInterval: 5_000,
  });

  const sparkline = useGpuSparkline(hostId, data?.gpu_util_pct ?? null, dataUpdatedAt);

  if (!data || !data.reachable) {
    return (
      <div
        className="flex items-center px-4 py-4 text-xs shrink-0 w-full md:w-[300px] border-t md:border-t-0 md:border-l border-subtle"
        style={{ color: C.textMuted, background: C.bgBase }}
      >
        {t("hostUnreachable")}
      </div>
    );
  }

  const gpuPct = data.gpu_util_pct ?? 0;
  // Unified-memory hosts (DGX Spark GB10): nvidia-smi reports no separate
  // VRAM — the GPU shares system RAM, so the RAM reading IS the GPU memory.
  // Fall back to the ram_* fields (and label the meter accordingly).
  const hasVram = data.vram_total_mb != null;
  const memLabel = hasVram ? "VRAM" : "RAM";
  const memUsedMb = hasVram ? data.vram_used_mb : data.ram_used_mb;
  const memTotalMb = hasVram ? data.vram_total_mb : data.ram_total_mb;
  const vramPct = memTotalMb && memUsedMb != null ? (memUsedMb / memTotalMb) * 100 : 0;
  const vramUsedGb = memUsedMb != null ? (memUsedMb / 1024).toFixed(0) : "—";
  const vramTotalGb = memTotalMb != null ? (memTotalMb / 1024).toFixed(0) : "—";
  // Temp has no natural 0-100 scale — 100C is a conservative thermal ceiling
  // used purely to size the bar; the printed value is always the real reading.
  const tempPct = data.gpu_temp_c != null ? Math.min((data.gpu_temp_c / 100) * 100, 100) : 0;

  const maxSample = Math.max(1, ...sparkline);

  return (
    <div
      className="flex flex-col gap-3.5 px-4 py-4 shrink-0 w-full md:w-[300px] border-t md:border-t-0 md:border-l border-subtle"
      style={{ background: C.bgBase }}
    >
      <Meter label="GPU" value={data.gpu_util_pct != null ? `${data.gpu_util_pct} %` : "—"} pct={gpuPct} />
      <Meter label={memLabel} value={`${vramUsedGb} / ${vramTotalGb} GB`} pct={vramPct} />
      <Meter label="Temp" value={data.gpu_temp_c != null ? `${data.gpu_temp_c} °C` : "—"} pct={tempPct} />
      {sparkline.length >= 2 && (
        <div className="flex items-end gap-0.5" style={{ height: "26px" }}>
          {sparkline.map((v, i) => (
            <div
              key={i}
              style={{
                width: "5px",
                borderRadius: "1px",
                height: `${Math.max(4, Math.min((v / maxSample) * 100, 100))}%`,
                background: i === sparkline.length - 1 ? C.accent : C.bgHover,
              }}
            />
          ))}
        </div>
      )}
    </div>
  );
}

// ── Agent chips ────────────────────────────────────────────────────────────

function AgentChipsRow({ runtime }: { runtime: Runtime }) {
  const t = useTranslations("runtimes.slotStage");
  const slug = runtime.slug ?? runtime.id;
  const { data } = useQuery({
    queryKey: ["runtime-agents", slug],
    queryFn: () => api.runtimes.db.agents(slug),
    enabled: !!slug,
    staleTime: 15_000,
    retry: false,
  });
  const bound = data?.agents ?? [];
  if (bound.length === 0) return null;

  return (
    <div className="flex items-center gap-1.5 flex-wrap mt-4">
      <span className="text-[11px]" style={{ color: C.textDim }}>{t("runsLabel")}</span>
      {bound.map((a) => (
        <Link
          key={a.id}
          href={`/agents/${a.id}`}
          className="inline-flex items-center gap-1 rounded-sm px-2 py-1 font-mono text-[11px] leading-none hover:bg-[var(--color-bg-hover)] transition-colors"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textSecondary }}
        >
          <EntityIcon value="🤖" size={12} className="inline-block align-[-2px] mr-1" />{a.name}
        </Link>
      ))}
    </div>
  );
}

// ── Switch row ─────────────────────────────────────────────────────────────

const PHASES: Array<NonNullable<RuntimeLiveStatus["phase"]>> = ["evicting", "launching", "loading"];

function PhaseIndicator({ phase, message }: { phase?: string | null; message?: string | null }) {
  return (
    <div className="flex-1 flex items-center gap-2" data-testid="phase-indicator">
      <div className="flex items-center gap-1.5 text-xs font-mono">
        {PHASES.map((p, i) => (
          <span key={p} className="flex items-center gap-1.5">
            <span style={{ color: p === phase ? C.accent : C.textDim, fontWeight: p === phase ? 600 : 400 }}>
              {p}
            </span>
            {i < PHASES.length - 1 && <span style={{ color: C.textDim }}>→</span>}
          </span>
        ))}
      </div>
      {message && <span className="text-xs truncate" style={{ color: C.textMuted }}>{message}</span>}
    </div>
  );
}

// Switch row carries sparkrun recipes only (per the approved M1 mockup — the
// switch targets are recipes, not sibling runtimes). Non-serving lifecycle
// runtimes live exclusively in the ready list below; slot takeover for them
// happens via Start inside the detail panel opened from that row.
/** One thing the operator can hand the GPU slot to — either a sparkrun
 *  recipe (same engine, different model/config) or a sibling runtime on the
 *  host (a different engine entirely). The UI deliberately unifies both:
 *  to the operator they are the same question ("which model gets the GPU?"),
 *  even though the backend paths differ (switch-recipe vs. start+eviction). */
type SwitchChoice =
  | { kind: "recipe"; name: string }
  | { kind: "runtime"; rt: Runtime };

function SwitchRow({
  group,
  serving,
  live,
  siblings,
  sizeGb,
}: {
  group: HostGroup;
  serving: Runtime | null;
  live?: Record<string, RuntimeLiveStatus>;
  siblings: Runtime[];
  sizeGb: (rt: Runtime) => number | undefined;
}) {
  const t = useTranslations("runtimes.slotStage");
  const tRecipe = useTranslations("runtimes.recipe");
  const queryClient = useQueryClient();
  // Two-step confirm before an eviction — restores the old SparkRecipeSwitcher's
  // arm/confirm behavior (a switch evicts whatever the GPU is currently
  // serving; that must never be one click away).
  const [confirm, setConfirm] = useState<SwitchChoice | null>(null);
  const rowRef = useRef<HTMLDivElement>(null);

  // The vllm_docker runtime actually holding the slot wins over "the first
  // vllm_docker row in the group" — a host can carry more than one
  // vllm_docker runtime (e.g. mid box-migration), and the switch must target
  // whichever one is really serving right now.
  const vllmRuntime =
    serving?.runtime_type === "vllm_docker"
      ? serving
      : group.runtimes.find((rt) => rt.runtime_type === "vllm_docker") ?? null;
  const recipeCapable = vllmRuntime != null;

  const currentRecipeQuery = useQuery({
    queryKey: ["runtime-current-recipe", vllmRuntime?.id],
    queryFn: () => api.runtimes.sparkrun.currentRecipe(vllmRuntime!.id),
    enabled: recipeCapable,
    staleTime: 300_000,
    refetchOnWindowFocus: false,
  });
  const recipesQuery = useQuery({
    queryKey: ["sparkrun-recipes"],
    queryFn: () => api.runtimes.sparkrun.listRecipes(),
    enabled: recipeCapable,
    staleTime: 300_000,
    refetchOnWindowFocus: false,
  });

  // sparkrun_managed: false means this is a plain vllm_docker container the
  // operator runs by hand — there is nothing honest to switch (mirrors the
  // old SparkRecipeSwitcher's `isSparkrun` gate). Undefined (still loading)
  // keeps the row rendered so it doesn't flash in and out while the probe is
  // in flight.
  const isSparkrunManaged = currentRecipeQuery.data?.sparkrun_managed !== false;

  const switchMutation = useMutation({
    mutationFn: (recipe: string) => api.runtimes.sparkrun.switchRecipe(vllmRuntime!.id, recipe),
    onSuccess: () => {
      setConfirm(null);
      queryClient.invalidateQueries({ queryKey: ["runtime-current-recipe", vllmRuntime?.id] });
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
    onError: () => setConfirm(null),
  });

  // Slot takeover by a sibling engine: plain start — the backend evicts the
  // current occupant itself (exclusive-memory handling in runtime_manager).
  const startMutation = useMutation({
    mutationFn: (rt: Runtime) => api.runtimes.start(rt.id),
    onSuccess: () => {
      setConfirm(null);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
    onError: () => setConfirm(null),
  });

  // Clicking outside the row disarms an armed confirm — same "click elsewhere
  // closes it" behavior the old dropdown had via its document mousedown listener.
  useEffect(() => {
    if (confirm === null) return;
    const onPointerDown = (e: MouseEvent) => {
      if (rowRef.current?.contains(e.target as Node)) return;
      setConfirm(null);
    };
    document.addEventListener("mousedown", onPointerDown);
    return () => document.removeEventListener("mousedown", onPointerDown);
  }, [confirm]);

  const servingLive = serving ? liveFor(serving, live) : undefined;
  const isSwitching = servingLive?.status === "switching";
  const isMutating = switchMutation.isPending || startMutation.isPending || isSwitching;

  if (isMutating) {
    const message = switchMutation.data?.message ?? startMutation.data?.message ?? null;
    return (
      <div
        className="flex items-center gap-3 px-4 py-3"
        style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
      >
        <PhaseIndicator phase={servingLive?.phase ?? undefined} message={message} />
      </div>
    );
  }

  const errorMessage = switchMutation.isError
    ? t("switchFailed", { message: switchMutation.error.message })
    : startMutation.isError
      ? t("switchFailed", { message: startMutation.error.message })
      : null;

  const showRecipes = recipeCapable && isSparkrunManaged;
  const allRecipes = recipesQuery.data?.recipes ?? [];
  const currentName = currentRecipeQuery.data?.current_recipe ?? null;
  // Dropdown order: current first, then runnable (solo-capable) recipes, then
  // the rest (disabled — they need more GPUs/nodes than this host has).
  const sortedRecipes = [
    ...allRecipes.filter((r) => r.name === currentName),
    ...allRecipes.filter((r) => r.name !== currentName && r.solo_capable),
    ...allRecipes.filter((r) => r.name !== currentName && !r.solo_capable),
  ];

  return (
    <div
      ref={rowRef}
      className="flex items-center gap-2 px-4 py-3 flex-wrap"
      style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
    >
      <span
        className="text-[10px] font-medium uppercase shrink-0"
        style={{ color: C.textMuted, letterSpacing: "0.08em" }}
      >
        {t("switchLabel")}
      </span>

      {confirm == null && (showRecipes || siblings.length > 0) && (
        <UnifiedSwitchDropdown
          recipes={showRecipes ? sortedRecipes : []}
          currentName={currentName}
          servingName={serving?.display_name ?? null}
          siblings={siblings}
          sizeGb={sizeGb}
          onSelect={setConfirm}
        />
      )}

      {confirm != null && (
        <div
          className="flex items-center gap-2 rounded-md px-3 py-2 text-xs flex-wrap"
          style={{ background: C.bgSurface, border: `1px solid ${C.borderAccent}` }}
        >
          <span className="font-mono" style={{ color: C.textPrimary }}>
            {confirm.kind === "recipe" ? confirm.name : confirm.rt.display_name}
          </span>
          <button
            onClick={() =>
              confirm.kind === "recipe"
                ? switchMutation.mutate(confirm.name)
                : startMutation.mutate(confirm.rt)
            }
            className="rounded-sm px-2 py-1 text-[10px] font-semibold cursor-pointer"
            style={{ background: C.accent, color: C.bgDeep }}
          >
            {tRecipe("confirmSwitch")}
          </button>
          <button
            onClick={() => setConfirm(null)}
            className="rounded-sm px-2 py-1 text-[10px] cursor-pointer"
            style={{ border: `1px solid ${C.borderSubtle}`, color: C.textMuted }}
          >
            {tRecipe("cancel")}
          </button>
          <span className="text-[10px]" style={{ color: C.textMuted }}>{tRecipe("warmupNotice")}</span>
        </div>
      )}

      {showRecipes && recipesQuery.isError && (
        <span className="text-xs" style={{ color: STATUS_TEXT.error }}>{tRecipe("unreachable")}</span>
      )}

      <button
        onClick={() => openModelsTab("download")}
        className="flex items-center gap-1.5 rounded-md px-3 py-2 text-xs cursor-pointer border-dashed"
        style={{ borderWidth: "1px", borderStyle: "dashed", borderColor: C.borderActive, color: C.textMuted }}
      >
        {t("addModel")}
      </button>

      {errorMessage && (
        <span className="text-xs w-full" style={{ color: STATUS_TEXT.error }}>{errorMessage}</span>
      )}
    </div>
  );
}

// ── Now block ──────────────────────────────────────────────────────────────

function NowBlock({ serving, live, sizeGb }: { serving: Runtime | null; live?: Record<string, RuntimeLiveStatus>; sizeGb: (rt: Runtime) => number | undefined }) {
  const t = useTranslations("runtimes.slotStage");
  const tr = useTranslations("runtimes");

  if (!serving) {
    return (
      <div className="px-4 pt-5 pb-4">
        <StateChip state="off" />
      </div>
    );
  }

  const l = liveFor(serving, live);
  const state = slotState(serving, l);
  const ctxNum = l?.served_context_len ?? serving.max_context_len;
  const gb = sizeGb(serving);
  const modelText = l?.served_model ?? serving.model_identifier ?? "—";
  const isReachable = l?.reachable === true;
  const hasDrift = Boolean(l?.drift || l?.context_drift);

  return (
    <div className="px-4 pt-5 pb-4">
      <StateChip state={state} phase={l?.phase} />
      <div className="mt-1.5" style={{ fontSize: "26px", fontWeight: 600, letterSpacing: "-0.02em", color: C.textPrimary }}>
        {serving.display_name}
      </div>
      <div className="font-mono text-xs mt-0.5" style={{ color: C.textMuted }}>
        {modelText} · {typeLabel(serving.runtime_type)}{gb != null ? ` · ${Math.round(gb)} GB` : ""}
      </div>
      <div className="flex items-center gap-6 mt-4">
        <div>
          <div className="text-[17px] font-semibold tabular-nums" style={{ color: C.textPrimary }}>{fmtCtx(ctxNum)}</div>
          <div className="text-[10px] uppercase mt-0.5" style={{ color: C.textMuted, letterSpacing: "0.1em" }}>{t("ctxLabel")}</div>
        </div>
        {isReachable && l?.latency_ms != null && (
          <div>
            <div className="text-[17px] font-semibold tabular-nums" style={{ color: C.textPrimary }}>{l.latency_ms} ms</div>
            <div className="text-[10px] uppercase mt-0.5" style={{ color: C.textMuted, letterSpacing: "0.1em" }}>{t("latencyLabel")}</div>
          </div>
        )}
        {hasDrift && (
          <div className="flex items-end pb-0.5">
            <span
              className="rounded-sm px-1.5 py-0.5 text-[10px] font-medium"
              style={{ color: STATUS_TEXT.warning, border: `1px solid ${C.warning}` }}
              title={tr("driftTitle", { model: serving.model_identifier ?? "—" })}
            >
              {tr("drift")}
            </span>
          </div>
        )}
      </div>
      <AgentChipsRow runtime={serving} />
    </div>
  );
}

// ── Header ─────────────────────────────────────────────────────────────────

function StageHeader({ group, serving, live, hostReachable }: { group: HostGroup; serving: Runtime | null; live?: Record<string, RuntimeLiveStatus>; hostReachable?: boolean }) {
  const t = useTranslations("runtimes.slotStage");
  const servingLive = serving ? liveFor(serving, live) : undefined;

  let status: React.ReactNode = null;
  if (servingLive?.status === "switching") {
    status = servingLive.phase ?? "—";
  } else if (serving && servingLive?.reachable === true) {
    status = t("engineReachable", { ms: servingLive.latency_ms ?? "—" });
  } else if (hostReachable === false) {
    status = t("hostUnreachable");
  }

  return (
    <div
      className="flex items-center justify-between px-4 py-2.5"
      style={{ borderBottom: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
    >
      <span
        className="text-[10px] font-medium uppercase"
        style={{ color: C.textSecondary, letterSpacing: "0.08em" }}
      >
        {group.host.display_name}
      </span>
      {status && (
        <span className="font-mono text-xs" style={{ color: C.textMuted }}>{status}</span>
      )}
    </div>
  );
}

// ── Placeholder ────────────────────────────────────────────────────────────

function StagePlaceholder({ group }: { group: HostGroup }) {
  const t = useTranslations("runtimes.slotStage");
  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{ borderBottom: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
      >
        <span className="text-[10px] font-medium uppercase" style={{ color: C.textSecondary, letterSpacing: "0.08em" }}>
          {group.host.display_name}
        </span>
      </div>
      <div className="flex items-center justify-between px-4 py-6">
        <span className="text-sm" style={{ color: C.textMuted }}>{t("placeholderTitle")}</span>
        <button
          onClick={() => openModelsTab("download")}
          className="flex items-center gap-1.5 rounded-md px-3 py-2 text-xs cursor-pointer"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
        >
          {t("addModel")}
        </button>
      </div>
    </div>
  );
}

// ── Root ───────────────────────────────────────────────────────────────────

export function SlotStage({
  group,
  live,
  sizeGb,
  onOpen,
}: {
  group: HostGroup;
  live?: Record<string, RuntimeLiveStatus>;
  sizeGb: (rt: Runtime) => number | undefined;
  onOpen: (rt: Runtime) => void;
}) {
  const serving = useMemo(() => pickServing(group, live), [group, live]);
  // Every non-serving host runtime belongs here, not just lifecycle-capable
  // ones — a host-bound omp/openai_compatible/llamacpp_docker runtime has no
  // start/stop path, but it is still real inventory on this box and must not
  // silently disappear. The detail panel opened from a row gates Control
  // correctly on its own (panelCapabilities(rt).lifecycle).
  const readyRuntimes = useMemo(
    () => group.runtimes.filter((rt) => rt.id !== serving?.id),
    [group, serving]
  );

  // Own host-reachability signal for the header — cheap re-query, TanStack
  // dedupes against TelemetryColumn's identical key.
  const { data: hostMetrics } = useQuery({
    queryKey: ["hosts", group.host.id, "metrics"],
    queryFn: () => api.hosts.metrics(group.host.id),
    refetchInterval: 5_000,
  });

  if (!serving && readyRuntimes.length === 0) {
    return <StagePlaceholder group={group} />;
  }

  return (
    <div className="rounded-xl overflow-hidden" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
      <StageHeader group={group} serving={serving} live={live} hostReachable={hostMetrics?.reachable} />
      <div className="flex flex-col md:flex-row">
        <div className="flex-1 min-w-0">
          <NowBlock serving={serving} live={live} sizeGb={sizeGb} />
        </div>
        <TelemetryColumn hostId={group.host.id} />
      </div>
      <SwitchRow group={group} serving={serving} live={live} siblings={readyRuntimes} sizeGb={sizeGb} />
    </div>
  );
}

// ── Recipe dropdown ─────────────────────────────────────────────────────────
// The sparkrun catalog is 65 recipes — pills flooded the stage. One trigger
// showing the current recipe; the list opens in a portal (the stage card
// clips overflow) with runnable recipes first and non-solo ones disabled.

function UnifiedSwitchDropdown({
  recipes,
  currentName,
  servingName,
  siblings,
  sizeGb,
  onSelect,
}: {
  recipes: SparkrunRecipe[];
  currentName: string | null;
  servingName: string | null;
  siblings: Runtime[];
  sizeGb: (rt: Runtime) => number | undefined;
  onSelect: (choice: SwitchChoice) => void;
}) {
  const t = useTranslations("runtimes.slotStage");
  const tRecipe = useTranslations("runtimes.recipe");
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (triggerRef.current?.contains(target) || menuRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const toggle = () => {
    if (!open && triggerRef.current) {
      const r = triggerRef.current.getBoundingClientRect();
      setPos({ top: r.bottom + 4, left: r.left });
    }
    setOpen((v) => !v);
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        onClick={toggle}
        aria-haspopup="listbox"
        aria-expanded={open}
        data-testid="recipe-dropdown-trigger"
        className="flex items-center gap-2 rounded-md px-3 py-2 text-xs cursor-pointer"
        style={{ background: C.bgSurface, border: `1px solid ${open ? C.borderAccent : C.border}`, color: C.textPrimary }}
      >
        <span className="font-mono truncate max-w-[340px]">
          {currentName ?? servingName ?? t("selectRecipe")}
        </span>
        <span aria-hidden style={{ color: C.textDim, fontSize: "9px" }}>▾</span>
      </button>

      {open && pos != null &&
        createPortal(
          <div
            ref={menuRef}
            role="listbox"
            data-testid="recipe-dropdown-list"
            className="fixed z-50 rounded-md overflow-y-auto"
            style={{
              top: pos.top,
              left: pos.left,
              width: "380px",
              maxHeight: "280px",
              background: C.bgElevated,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
            }}
          >
            {recipes.length > 0 && (
              <div
                className="px-3 pt-2 pb-1 text-[9px] font-medium uppercase"
                style={{ color: C.textDim, letterSpacing: "0.1em" }}
              >
                {t("groupRecipes")}
              </div>
            )}
            {recipes.map((r) => {
              const isActive = r.name === currentName;
              const isDisabled = !r.solo_capable;
              const gpuHint =
                r.tp != null || r.nodes != null
                  ? `tp=${r.tp ?? 1}${r.nodes != null ? `, nodes=${r.nodes}` : ""}`
                  : null;
              return (
                <button
                  key={r.name}
                  role="option"
                  aria-selected={isActive}
                  disabled={isActive || isDisabled}
                  title={isDisabled ? tRecipe("needsMoreTitle", { gpuHint: gpuHint ?? tRecipe("moreGpusNodes") }) : undefined}
                  onClick={() => {
                    setOpen(false);
                    onSelect({ kind: "recipe", name: r.name });
                  }}
                  className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs font-mono cursor-pointer disabled:cursor-not-allowed transition-colors hover:bg-[var(--color-bg-hover)]"
                  style={{
                    color: isActive ? C.accent : isDisabled ? C.textDim : C.textPrimary,
                    borderBottom: `1px solid ${C.borderSubtle}`,
                  }}
                >
                  <span className="truncate">{r.name}</span>
                  {isActive && <span className="ml-auto shrink-0 text-[9px] uppercase" style={{ color: C.accent }}>{t("recipeCurrent")}</span>}
                </button>
              );
            })}

            {siblings.length > 0 && (
              <div
                className="px-3 pt-2 pb-1 text-[9px] font-medium uppercase"
                style={{ color: C.textDim, letterSpacing: "0.1em" }}
              >
                {t("groupEngines")}
              </div>
            )}
            {siblings.map((rt) => {
              // Slot takeover = start this runtime; only backend-startable
              // engines are selectable, the rest stay visible but disabled.
              const startable = panelCapabilities(rt).lifecycle;
              const gb = sizeGb(rt);
              return (
                <button
                  key={rt.id}
                  role="option"
                  aria-selected={false}
                  disabled={!startable}
                  data-testid={`switch-engine-${rt.slug ?? rt.id}`}
                  title={!startable ? t("engineNotStartable") : undefined}
                  onClick={() => {
                    setOpen(false);
                    onSelect({ kind: "runtime", rt });
                  }}
                  className="flex items-center gap-2 w-full px-3 py-2 text-left text-xs cursor-pointer disabled:cursor-not-allowed transition-colors hover:bg-[var(--color-bg-hover)]"
                  style={{
                    color: startable ? C.textPrimary : C.textDim,
                    borderBottom: `1px solid ${C.borderSubtle}`,
                  }}
                >
                  <span className="truncate">{rt.display_name}</span>
                  <span className="ml-auto shrink-0" style={{ color: C.textDim }}>
                    {typeLabel(rt.runtime_type)}{gb != null ? ` · ${Math.round(gb)} GB` : ""}
                  </span>
                </button>
              );
            })}
          </div>,
          document.body
        )}
    </>
  );
}
