"use client";

/**
 * SlotStage — one GPU slot's "stage" (mockup M1, m1-slot-buehne.html).
 *
 * Renders a single host's occupancy: what's currently serving (or OFF),
 * live GPU/VRAM/temp telemetry and the recipe switcher for the box (one
 * source for every recipe the box can run — see HostRecipeSwitcher).
 *
 * HONESTY RULE (hard, per task brief): only real fields are rendered —
 * no tok/s, no uptime, no ETA. The mockup shows all three; this component
 * deliberately does not reproduce them.
 */

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import type { Runtime, RuntimeLiveStatus } from "@/lib/types";
import { pickServing, pickSlot, type HostGroup } from "./grouping";
import { useGpuSparkline } from "./useGpuSparkline";
import { fmtCtx } from "@/lib/utils";
import { openModelsTab } from "./modelsTab";
import { EntityIcon } from "@/components/shared/EntityIcon";
import { HostRecipeSwitcher } from "@/components/shared/HostRecipeSwitcher";
import { DeviceModeStrip, useDevices } from "./DeviceControl";
import { HostAutostartRow } from "./HostAutostartRow";
import { RoleChip } from "./RoleField";
import { useAppStore } from "@/lib/store";
import type { Device } from "@/lib/types";

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
  // Unified-memory hosts (GPU-Box GB10): nvidia-smi reports no separate
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

// Die Umschalt-Zeile hat genau EINE Quelle: die Rezeptliste der Box
// (GET /hosts/{id}/recipes, Vertrag 02.09.2026). Die früheren zwei Quellen —
// eine per SSH gelesene Rezeptliste eines Werkzeugs plus die
// Geschwister-Runtimes derselben Box — sind weg: sie widersprachen sich, und
// bei 0 Rezepten verschwand das Dropdown ganz. Andere Runtimes der Box
// startet man weiterhin über das Register (Infrastruktur-Tab) → Detail-Panel.
function SwitchRow({
  group,
  serving,
  live,
}: {
  group: HostGroup;
  serving: Runtime | null;
  live?: Record<string, RuntimeLiveStatus>;
}) {
  const t = useTranslations("runtimes.slotStage");
  const servingLive = serving ? liveFor(serving, live) : undefined;

  // Während der Watcher ein Umschalten meldet (evicting → launching → loading)
  // zeigt die Zeile die Phase statt eines bedienbaren Umschalters — zwei
  // Starts gleichzeitig darf es nicht geben.
  if (servingLive?.status === "switching") {
    return (
      <div
        className="flex items-center gap-3 px-4 py-3"
        style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
      >
        <PhaseIndicator phase={servingLive.phase ?? undefined} message={null} />
      </div>
    );
  }

  return (
    <div
      className="flex items-center gap-2 px-4 py-3 flex-wrap"
      style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
    >
      <span
        className="text-[10px] font-medium uppercase shrink-0"
        style={{ color: C.textMuted, letterSpacing: "0.08em" }}
      >
        {t("switchLabel")}
      </span>

      <HostRecipeSwitcher
        hostId={group.host.id}
        hostName={group.host.slug}
        servingName={serving?.display_name ?? null}
      />

      <button
        onClick={() => openModelsTab("download")}
        className="flex items-center gap-1.5 rounded-md px-3 py-2 text-xs cursor-pointer border-dashed"
        style={{ borderWidth: "1px", borderStyle: "dashed", borderColor: C.borderActive, color: C.textMuted }}
      >
        {t("addModel")}
      </button>
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

// ── Slot-Zeile (Agenten-URL) ───────────────────────────────────────────────
/**
 * Die feste Adresse dieser Box (ADR-078, Slot-Runtime).
 *
 * Bewusst RUHIG: eine Zeile, kein Kasten, kein Knopf. Sie sagt nur, unter
 * welchem Namen die Agenten die Box erreichen — der Name kommt FERTIG vom
 * Server („BOX-A :8000 (aktuell: <Modell>)") und wird bei jedem Modellwechsel
 * nachgezogen. Hier wird nichts zusammengebaut, nichts nachgerechnet.
 *
 * Kein Start/Stop/Autostart/Umschalter: diese Zeile hat keinen Startbefehl
 * und kein eigenes Rezept (panelCapabilities sperrt das im Detail-Panel
 * gleich mit). Klick öffnet das Panel — Modell, Endpunkt, gebundene Agenten.
 */
function SlotUrlRow({ slot, onOpen }: { slot: Runtime; onOpen: (rt: Runtime) => void }) {
  const t = useTranslations("runtimes.slot");
  return (
    <button
      type="button"
      data-testid="slot-url-row"
      onClick={() => onOpen(slot)}
      title={t("hint")}
      className="w-full flex items-center gap-2.5 px-4 py-2.5 text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{ borderTop: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
    >
      <span
        className="text-[10px] font-medium uppercase shrink-0"
        style={{ color: C.textMuted, letterSpacing: "0.08em" }}
      >
        {t("rowLabel")}
      </span>
      <span
        className="text-[9px] px-1.5 py-0.5 rounded-sm font-mono uppercase tracking-wide shrink-0"
        style={{ background: C.bgHover, color: C.textSecondary, border: `1px solid ${C.borderSubtle}` }}
      >
        {t("chip")}
      </span>
      <span className="font-mono text-xs truncate" style={{ color: C.textSecondary }}>
        {slot.display_name}
      </span>
    </button>
  );
}

// ── Geräte-Streifen ────────────────────────────────────────────────────────
/**
 * Der GPU-Modus-Schalter, sofern diese Box ihn überhaupt haben darf.
 *
 * Drei Bedingungen, alle drei aus echten Feldern (HONESTY RULE):
 *  - `/nodes/devices` kennt die Box → sie hat einen node-agent. Ohne Agent
 *    kann MC nichts stellen, also gibt es auch keinen Schalter (SSH-/WoL-
 *    Boxen sehen aus wie bisher).
 *  - Ampel nicht grau → MC weiss überhaupt etwas über den Zustand.
 *  - Host erreichbar → bei einer schlafenden Box wäre der Schalter ein
 *    Versprechen, das niemand einlöst. Die Kachel sagt daneben schon, dass
 *    sie nicht erreichbar ist; ein bedienbarer Regler daneben widerspricht dem.
 *
 * Sichtbar heisst noch nicht bedienbar: meldet der Agent keinen Zustand
 * (`reason` no_device_state), ist er verstummt (`stale`) oder fehlen die
 * Steuer-Skripte (`gpu_mode` unknown), SPERRT der Streifen den Schalter selbst
 * und sagt in einem Satz, was fehlt — siehe switchLockFor() in DeviceControl.
 */
function DeviceStrip({
  device,
  hostReachable,
}: {
  device?: Device;
  hostReachable?: boolean;
}) {
  const currentUser = useAppStore((s) => s.currentUser);
  if (!device || device.status === "grey" || hostReachable === false) return null;
  return <DeviceModeStrip device={device} canControl={currentUser?.role === "admin"} />;
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
      <span className="flex items-center gap-2 min-w-0">
        <span
          className="text-[10px] font-medium uppercase truncate"
          style={{ color: C.textSecondary, letterSpacing: "0.08em" }}
        >
          {group.host.display_name}
        </span>
        <RoleChip role={group.host.role} />
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
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-[10px] font-medium uppercase truncate" style={{ color: C.textSecondary, letterSpacing: "0.08em" }}>
            {group.host.display_name}
          </span>
          <RoleChip role={group.host.role} />
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

// ── Worker tile ────────────────────────────────────────────────────────────
// Verbund-UI Phase 1a (30.08.2026): a kind="agent" host with zero bound
// runtimes (e.g. a headless GLM verbund's rank1/worker box) is real fleet
// inventory, not an empty slot — it just doesn't serve its own model through
// MC's runtime registry. StagePlaceholder's "No model set up" + "add model"
// CTA is actively wrong there (nothing to add — the box isn't meant to run
// its own standalone model). This reuses TelemetryColumn as-is instead of
// rebuilding metric rendering: same offline text, same honesty guarantee
// (real fields only, no served_model/throughput/own switch — see the file
// header's HONESTY RULE).
//
// Phase 1b (30.08.2026): once runtime_hosts has a row for this host,
// group.workerOf (grouping.ts) resolves WHICH verbund it belongs to — shown
// as "Part of: <runtime> · head → <host-slug>" instead of the generic hint.
// Falls back to the generic hint when workerOf is absent (a paired agent
// host that isn't (yet) a member of any runtime's declared topology).

function WorkerTile({ group, device, live }: { group: HostGroup; device?: Device; live?: Record<string, RuntimeLiveStatus> }) {
  const t = useTranslations("runtimes.slotStage");
  // Das Modell, das über diese Box läuft, ist das des Kopfes — die Instanz
  // hängt am Kopf-Host, nicht hier. Gleicher Schlüssel wie die Seite, TanStack
  // teilt die Abfrage; scheitert sie, bleibt die Kachel bei der Zeile „Teil von".
  const workerOf = group.workerOf;
  const { data: runtimeList } = useQuery({
    queryKey: ["runtimes"],
    queryFn: () => api.runtimes.list(),
    enabled: Boolean(workerOf),
  });
  const headRuntime = useMemo(() => {
    if (!workerOf) return null;
    // GET /runtimes antwortet mit { runtimes: [...] } (Live-Befund 03.09.2026) —
    // ein nacktes Array bleibt für Tests und ältere Aufrufer möglich.
    const raw = runtimeList as unknown;
    const list: Runtime[] = Array.isArray(raw)
      ? (raw as Runtime[])
      : ((raw as { runtimes?: Runtime[]; items?: Runtime[] } | undefined)?.runtimes
        ?? (raw as { items?: Runtime[] } | undefined)?.items
        ?? []);
    return list.find((rt) => rt.id === workerOf.runtimeId) ?? null;
  }, [runtimeList, workerOf]);
  // Eigenes Erreichbarkeits-Signal — TanStack fasst die Abfrage mit der der
  // TelemetryColumn zusammen, kostet also keinen zweiten Aufruf.
  const { data: hostMetrics } = useQuery({
    queryKey: ["hosts", group.host.id, "metrics"],
    queryFn: () => api.hosts.metrics(group.host.id),
    refetchInterval: 5_000,
  });
  const hint = workerOf
    ? workerOf.headSlug
      ? t("workerPartOfWithHead", { runtime: workerOf.runtimeDisplayName, head: workerOf.headSlug })
      : t("workerPartOf", { runtime: workerOf.runtimeDisplayName })
    : t("workerHint");
  return (
    <div
      className="rounded-xl overflow-hidden"
      style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}
    >
      <div
        className="flex items-center justify-between px-4 py-2.5"
        style={{ borderBottom: `1px solid ${C.borderSubtle}`, background: C.bgBase }}
      >
        <span className="flex items-center gap-2 min-w-0">
          <span className="text-[10px] font-medium uppercase truncate" style={{ color: C.textSecondary, letterSpacing: "0.08em" }}>
            {group.host.display_name}
          </span>
          <RoleChip role={group.host.role} />
        </span>
        <span
          className="text-[9px] px-1.5 py-0.5 rounded-sm font-mono uppercase tracking-wide"
          style={{ background: C.bgHover, color: C.textSecondary, border: `1px solid ${C.borderSubtle}` }}
        >
          {t("workerBadge")}
        </span>
      </div>
      <div className="flex flex-col md:flex-row">
        <div className="flex-1 min-w-0">
          {headRuntime ? (
            <WorkerNowBlock runtime={headRuntime} live={live} hint={hint} />
          ) : (
            <div className="flex items-center px-4 py-6">
              <span className="text-sm" style={{ color: C.textMuted }}>{hint}</span>
            </div>
          )}
        </div>
        <TelemetryColumn hostId={group.host.id} />
      </div>
      <DeviceStrip device={device} hostReachable={hostMetrics?.reachable} />
      {/* Auch die Worker-Kachel zeigt den Autostart: gehört sie zu einem
          Verbund, steht hier der Chip „über Head …"; läuft sie solo, ist es
          ihr eigener Schalter. */}
      <HostAutostartRow hostId={group.host.id} />
    </div>
  );
}

// Der Modellblock der Worker-Kachel (Wunsch des Betreibers 03.09.2026): das
// Modell, das über diese Box läuft, steht da — Zustand, Name, Modell-Kennung,
// Engine — nur eine Stufe kleiner als beim Kopf, denn der Kopf führt. Die
// Zeile „Teil von … · Kopf → …" bleibt darunter als Herkunft. Zustand und
// Kennung kommen aus der Live-Probe des Kopfes (HONESTY RULE: nichts, was
// diese Box nicht wirklich mitträgt).
function WorkerNowBlock({ runtime, live, hint }: { runtime: Runtime; live?: Record<string, RuntimeLiveStatus>; hint: string }) {
  const l = liveFor(runtime, live);
  const state = slotState(runtime, l);
  const modelText = l?.served_model ?? runtime.model_identifier ?? "—";
  return (
    <div className="px-4 pt-5 pb-4" data-testid="worker-now-block">
      <StateChip state={state} phase={l?.phase} />
      <div className="mt-1.5" style={{ fontSize: "20px", fontWeight: 600, letterSpacing: "-0.02em", color: C.textPrimary }}>
        {runtime.display_name}
      </div>
      <div className="font-mono text-xs mt-0.5" style={{ color: C.textMuted }}>
        {modelText} · {typeLabel(runtime.runtime_type)}
      </div>
      <div className="text-[11px] mt-3" style={{ color: C.textMuted }}>{hint}</div>
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
  // Die Slot-Zeile (ADR-078) ist die Adresse der Box, kein Modell auf ihr —
  // sie bekommt ihre eigene ruhige Zeile und zählt darum weder als „läuft"
  // noch als vorhandenes Inventar für die Platzhalter-Entscheidung unten.
  const slot = useMemo(() => pickSlot(group), [group]);
  // Every non-serving host runtime belongs here, not just lifecycle-capable
  // ones — a host-bound omp/openai_compatible/llamacpp_docker runtime has no
  // start/stop path, but it is still real inventory on this box and must not
  // silently disappear. The detail panel opened from a row gates Control
  // correctly on its own (panelCapabilities(rt).lifecycle).
  const readyRuntimes = useMemo(
    () => group.runtimes.filter((rt) => rt.id !== serving?.id && !rt.is_slot),
    [group, serving]
  );

  // Own host-reachability signal for the header — cheap re-query, TanStack
  // dedupes against TelemetryColumn's identical key.
  const { data: hostMetrics } = useQuery({
    queryKey: ["hosts", group.host.id, "metrics"],
    queryFn: () => api.hosts.metrics(group.host.id),
    refetchInterval: 5_000,
  });

  // Eine Abfrage für die ganze Flotte, nicht eine je Kachel — TanStack teilt
  // sie über den Schlüssel. Enthalten sind nur je gekoppelte Boxen.
  const device = useDevices().get(group.host.id);

  if (!serving && readyRuntimes.length === 0) {
    // Phase 1a (Verbund-UI, 30.08.2026): a kind="agent" host (self-registering
    // node-agent, no SSH/lifecycle path) with nothing bound is real fleet
    // inventory — telemetry-only, not an empty slot to fill.
    if (group.host.kind === "agent" || group.workerOf) {
      return <WorkerTile group={group} device={device} live={live} />;
    }
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
      {/* Zwischen Zustand und Rezept-Wechsel: der Modus gehört zur Box, nicht
          zum Modell — darum unter dem Slot-Körper und über der Rezept-Zeile. */}
      <DeviceStrip device={device} hostReachable={hostMetrics?.reachable} />
      {/* Über dem Umschalter: erst steht da, unter welcher Adresse die
          Agenten die Box erreichen — dann, was man dort starten kann. */}
      {slot && <SlotUrlRow slot={slot} onOpen={onOpen} />}
      <SwitchRow group={group} serving={serving} live={live} />
      {/* Unter dem Umschalter: erst wählt man das Rezept, dann sagt man, ob MC
          es nach einem Ausfall selbst wieder hochziehen darf. */}
      <HostAutostartRow hostId={group.host.id} />
    </div>
  );
}
