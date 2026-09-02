"use client";

/**
 * BoxWizard — eine GPU-Box per Klick anbinden (PR 4).
 *
 * Fünf Schritte, die genau der Reihenfolge folgen, in der eine neue Box
 * tatsächlich nutzbar wird:
 *
 *   1 Verbindung   SSH-Daten eingeben, testen → Inventar der Box sehen
 *   2 Basis        Ampeln (Docker / NVIDIA-Runtime / GPU / Platz), fehlende
 *                  Basis per Bootstrap nachziehen, Live-Log
 *   3 Engine+Modell Registry-Einträge, gefiltert auf die Architektur der Box
 *                  und geprüft gegen ihren ECHTEN Speicher
 *   4 Zusammenfassung inkl. des Kommandos, das gleich auf der Box laufen wird
 *   5 Ergebnis     Health-Status der frisch angelegten Runtime
 *
 * Der Wizard ersetzt nichts — er orchestriert Bestehendes: POST /hosts legt
 * die Host-Zeile an, POST /runtimes die Runtime, /runtimes/{id}/start startet
 * sie. Es gibt keinen zweiten Lifecycle-Pfad; wäre einer hier, würde er beim
 * ersten Backend-Umbau auseinanderlaufen.
 *
 * Die Host-Zeile entsteht bewusst erst beim Übergang 1→2, also NACH einem
 * erfolgreichen Probe. Sonst hinterlässt jeder Tippfehler eine tote Host-Zeile,
 * die jemand später wieder aufräumen muss.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { motion } from "framer-motion";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import {
  AlertTriangle,
  Check,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Cpu,
  Loader2,
  Rocket,
  Server,
  X,
  XCircle,
} from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import type {
  Host,
  HostBootstrapLog,
  HostBootstrapLogLine,
  HostProbeResult,
  HostRole,
  LocalRecipe,
} from "@/lib/types";
import { RoleField, suggestRole } from "./RoleField";
import {
  wizardBackdropClass,
  wizardBtnPrimaryStyle,
  wizardCardStyle,
  wizardInputClass,
  wizardInputStyle,
  wizardLabelClass,
  wizardOverlayClass,
} from "@/app/agents/wizard/shared";

const ease = [0.16, 1, 0.3, 1] as const;

export const BOX_WIZARD_STEPS = [
  "connection",
  "base",
  "model",
  "review",
  "result",
] as const;

/**
 * Engines, für die dieser Wizard eine Runtime bauen kann. sparkrun-Rezepte
 * erscheinen nicht: die laufen über switch-recipe auf einer bestehenden
 * Spark-Runtime (siehe LocalModelBrowser) und brauchen kein launch_command.
 */
const WIZARD_ENGINES = ["llamacpp_docker", "vllm_docker"] as const;

/** Default-Port je Engine — llama.cpp 8080, vLLM 8000 (deren Konventionen). */
const DEFAULT_PORTS: Record<string, number> = {
  llamacpp_docker: 8080,
  vllm_docker: 8000,
};

export interface BoxWizardState {
  step: number;
  slug: string;
  displayName: string;
  /** Geräterolle (P2) — Vorschlag aus dem Bestand, im Schritt 1 änderbar.
   *  null nur, solange die Vorbelegung noch nicht gesetzt wurde. */
  role: HostRole | null;
  /** true, solange der Wert der Vorschlag ist (niemand hat geklickt). */
  roleSuggested: boolean;
  sshHost: string;
  sshUser: string;
  sshKeyPath: string;
  probe: HostProbeResult | null;
  hostId: string | null;
  recipe: LocalRecipe | null;
  runtimeSlug: string;
  port: number;
}

export function initialBoxWizardState(): BoxWizardState {
  return {
    step: 0,
    slug: "",
    displayName: "",
    role: null,
    roleSuggested: true,
    sshHost: "",
    sshUser: "",
    sshKeyPath: "",
    probe: null,
    hostId: null,
    recipe: null,
    runtimeSlug: "",
    port: DEFAULT_PORTS.llamacpp_docker,
  };
}

// ── Reine Logik (separat testbar) ────────────────────────────────────────────

/** `uname -m` → das arch-Vokabular der Registry (arm64 | x86_64). */
export function archFilter(probeArch: string | null): string | undefined {
  if (!probeArch) return undefined;
  const a = probeArch.toLowerCase();
  if (a === "aarch64" || a === "arm64") return "arm64";
  if (a === "x86_64" || a === "amd64") return "x86_64";
  return undefined;
}

/**
 * Nutzbarer Speicher der Box in GB — das, wogegen ein Rezept geprüft wird.
 *
 * Summe des GPU-VRAM, wenn nvidia-smi Grössen liefert. Auf Unified-Memory-
 * Boxen (GB10/DGX Spark) meldet nvidia-smi bereits den geteilten Speicher;
 * meldet es gar nichts Brauchbares, ist der RAM die ehrlichere Schätzung als
 * eine Konstante. Genau das ersetzt die 121-GB-Annahme aus PR 2.
 */
export function usableMemoryGb(probe: HostProbeResult | null): number | null {
  if (!probe) return null;
  const vram = probe.gpus
    .map((g) => g.vram_gb)
    .filter((v): v is number => typeof v === "number");
  if (vram.length > 0) return vram.reduce((a, b) => a + b, 0);
  return probe.ram_gb;
}

/** `false` nur, wenn wir Bedarf UND Kapazität kennen und es nicht passt. */
export function recipeFits(recipe: LocalRecipe, capacityGb: number | null): boolean {
  if (recipe.min_vram_gb == null || capacityGb == null) return true;
  return recipe.min_vram_gb <= capacityGb;
}

/** Braucht die Box überhaupt einen Bootstrap? */
export function needsBootstrap(probe: HostProbeResult | null): boolean {
  if (!probe || !probe.reachable) return false;
  if (!probe.docker.installed) return true;
  // GPU vorhanden, aber Docker kennt die NVIDIA-Runtime nicht → `--gpus all`
  // scheitert später, und zwar erst beim ersten Start des Modells.
  if (probe.gpus.length > 0 && !probe.docker.nvidia_runtime) return true;
  return false;
}

export function canProceed(state: BoxWizardState): boolean {
  switch (state.step) {
    case 0:
      return (
        !!state.probe?.reachable &&
        state.slug.trim().length > 0 &&
        state.displayName.trim().length > 0 &&
        state.sshHost.trim().length > 0
      );
    case 1:
      // Ohne Docker gibt es keine Engine — hier ist Weiterklicken sinnlos,
      // nicht bloss unschön.
      return !!state.probe?.docker.installed;
    case 2:
      return !!state.recipe && state.runtimeSlug.trim().length > 0 && state.port > 0;
    case 3:
      return true;
    default:
      return false;
  }
}

/** "DGX Spark" → "dgx-spark" — Slug-Vorschlag beim Tippen des Namens. */
export function slugify(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-+|-+$/g, "")
    .slice(0, 64);
}

function extractApiError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(jsonStart));
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* raw message */
    }
  }
  return msg;
}

// ── Kleinteile ───────────────────────────────────────────────────────────────

function WizField({
  label,
  value,
  onChange,
  placeholder,
  mono,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
  mono?: boolean;
}) {
  const id = `box-wizard-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div>
      <label htmlFor={id} className={wizardLabelClass}>
        {label}
      </label>
      <input
        id={id}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={`${wizardInputClass} ${mono ? "font-mono text-[12px]" : ""}`}
        style={wizardInputStyle}
      />
    </div>
  );
}

type LightState = "ok" | "warn" | "bad";

function Light({
  state,
  label,
  detail,
  testId,
}: {
  state: LightState;
  label: string;
  detail: string;
  testId: string;
}) {
  const color = state === "ok" ? C.online : state === "warn" ? C.warning : C.error;
  const Icon = state === "ok" ? CheckCircle2 : state === "warn" ? AlertTriangle : XCircle;
  return (
    <div
      data-testid={testId}
      data-state={state}
      className="flex items-start gap-2.5 px-3 py-2.5 rounded-xl"
      style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
    >
      <Icon size={14} style={{ color, marginTop: 1 }} className="shrink-0" />
      <div className="min-w-0">
        <div className="text-xs font-medium" style={{ color: C.textPrimary }}>
          {label}
        </div>
        <div className="text-[11px] mt-0.5" style={{ color: C.textMuted }}>
          {detail}
        </div>
      </div>
    </div>
  );
}

// ── Schritt 1: Verbindung ────────────────────────────────────────────────────

function ConnectionStep({
  state,
  update,
}: {
  state: BoxWizardState;
  update: (patch: Partial<BoxWizardState>) => void;
}) {
  const t = useTranslations("runtimes.boxWizard");
  const [probing, setProbing] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function runProbe() {
    setProbing(true);
    setError(null);
    try {
      const result = await api.hosts.probe({
        ssh_host: state.sshHost.trim(),
        ssh_user: state.sshUser.trim() || null,
        ssh_key_path: state.sshKeyPath.trim() || null,
      });
      update({ probe: result });
    } catch (err) {
      // Eine unerreichbare Box ist ein 200 mit reachable:false — hier landet
      // nur ein echter Request-Fehler (422, Netz, Auth).
      setError(extractApiError(err));
      update({ probe: null });
    } finally {
      setProbing(false);
    }
  }

  const probe = state.probe;

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs" style={{ color: C.textMuted }}>
        {t("connectionIntro")}
      </p>

      <WizField
        label={t("fieldDisplayName")}
        value={state.displayName}
        onChange={(v) =>
          update({
            displayName: v,
            // Slug folgt dem Namen, solange niemand ihn von Hand angefasst hat.
            slug: state.slug === slugify(state.displayName) ? slugify(v) : state.slug,
          })
        }
        placeholder={t("displayNamePlaceholder")}
      />
      <WizField
        label={t("fieldSlug")}
        value={state.slug}
        onChange={(v) => update({ slug: v })}
        placeholder={t("slugPlaceholder")}
        mono
      />
      <WizField
        label={t("fieldSshHost")}
        value={state.sshHost}
        onChange={(v) => update({ sshHost: v, probe: null })}
        placeholder={t("sshHostPlaceholder")}
        mono
      />
      <RoleField
        idPrefix="box-wizard-role"
        value={state.role}
        onChange={(role) => update({ role, roleSuggested: false })}
        labelClassName={wizardLabelClass}
        suggested={state.roleSuggested}
      />
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
        <WizField
          label={t("fieldSshUser")}
          value={state.sshUser}
          onChange={(v) => update({ sshUser: v, probe: null })}
          placeholder={t("sshUserPlaceholder")}
          mono
        />
        <WizField
          label={t("fieldSshKeyPath")}
          value={state.sshKeyPath}
          onChange={(v) => update({ sshKeyPath: v, probe: null })}
          placeholder="/root/.ssh/id_ed25519"
          mono
        />
      </div>

      <button
        type="button"
        data-testid="box-wizard-probe"
        onClick={runProbe}
        disabled={probing || state.sshHost.trim().length === 0}
        className="self-start flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
        style={{
          background: C.accentSubtle,
          border: `1px solid ${C.borderAccent}`,
          color: C.accent,
        }}
      >
        {probing ? <Loader2 size={11} className="animate-spin" /> : <Server size={11} />}
        {t("testConnection")}
      </button>

      {error && (
        <div
          data-testid="box-wizard-probe-error"
          className="text-xs px-3 py-2 rounded-lg"
          style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: C.error }}
        >
          {error}
        </div>
      )}

      {probe && !probe.reachable && (
        <div
          data-testid="box-wizard-unreachable"
          className="text-xs px-3 py-2 rounded-lg flex items-start gap-2"
          style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: C.error }}
        >
          <XCircle size={13} className="shrink-0" style={{ marginTop: 1 }} />
          <div>
            <div className="font-medium">{t("unreachable")}</div>
            <div className="mt-0.5 font-mono text-[11px] break-words">{probe.reason}</div>
          </div>
        </div>
      )}

      {probe?.reachable && (
        <div
          data-testid="box-wizard-inventory"
          className="rounded-xl px-3 py-2.5 flex flex-col gap-1.5"
          style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
        >
          <div className="flex items-center gap-1.5 text-xs" style={{ color: C.online }}>
            <CheckCircle2 size={13} />
            {t("reachable", { user: probe.user ?? "?", arch: probe.arch ?? "?" })}
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-[11px]" style={{ color: C.textMuted }}>
            <span>{probe.os ?? "?"} {probe.kernel ?? ""}</span>
            <span>
              {probe.gpus.length > 0
                ? probe.gpus
                    .map((g) => `${g.name}${g.vram_gb != null ? ` (${g.vram_gb} GB)` : ""}`)
                    .join(", ")
                : t("noGpu")}
            </span>
            {probe.ram_gb != null && <span>{t("ramLabel", { n: probe.ram_gb })}</span>}
            {probe.disk_free_gb != null && (
              <span>{t("diskLabel", { n: probe.disk_free_gb })}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── Schritt 2: Basis-Check + Bootstrap ───────────────────────────────────────

function BaseStep({
  state,
  update,
}: {
  state: BoxWizardState;
  update: (patch: Partial<BoxWizardState>) => void;
}) {
  const t = useTranslations("runtimes.boxWizard");
  const probe = state.probe;
  const [starting, setStarting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [log, setLog] = useState<HostBootstrapLogLine[]>([]);
  const [status, setStatus] = useState<HostBootstrapLog["status"]>("idle");
  const [statusMessage, setStatusMessage] = useState<string | null>(null);
  const cursor = useRef(0);
  const logEnd = useRef<HTMLDivElement | null>(null);

  const polling = status === "running";

  // Poll-Schleife: dieselbe Idee wie die CLI-Tool-Updates, nur cursorbasiert —
  // wir holen ausschliesslich neue Zeilen, statt den ganzen Log neu zu laden.
  useEffect(() => {
    if (!polling || !state.hostId) return;
    let cancelled = false;
    const timer = setInterval(async () => {
      try {
        const res = await api.hosts.bootstrapLog(state.hostId!, cursor.current);
        if (cancelled) return;
        cursor.current = res.cursor;
        if (res.lines.length > 0) setLog((prev) => [...prev, ...res.lines]);
        setStatus(res.status);
        setStatusMessage(res.message);
        if (res.status !== "running") {
          // Nach dem Lauf frisch messen statt das alte Inventar weiterzureichen
          // — die Ampeln sollen zeigen, was JETZT auf der Box ist.
          const fresh = await api.hosts.probe({ host_id: state.hostId! });
          if (!cancelled) update({ probe: fresh });
        }
      } catch (err) {
        if (!cancelled) {
          setError(extractApiError(err));
          setStatus("failed");
        }
      }
    }, 1500);
    return () => {
      cancelled = true;
      clearInterval(timer);
    };
  }, [polling, state.hostId, update]);

  useEffect(() => {
    logEnd.current?.scrollIntoView({ block: "nearest" });
  }, [log.length]);

  async function startBootstrap() {
    if (!state.hostId) return;
    setStarting(true);
    setError(null);
    setLog([]);
    cursor.current = 0;
    try {
      await api.hosts.bootstrap(state.hostId);
      setStatus("running");
      setStatusMessage(null);
    } catch (err) {
      setError(extractApiError(err));
    } finally {
      setStarting(false);
    }
  }

  if (!probe) return null;

  const hasGpu = probe.gpus.length > 0;
  const diskLow = probe.disk_free_gb != null && probe.disk_free_gb < 50;
  const required = needsBootstrap(probe);

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs" style={{ color: C.textMuted }}>
        {required ? t("baseIntroNeeded") : t("baseIntroReady")}
      </p>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-2">
        <Light
          testId="light-docker"
          state={probe.docker.installed ? "ok" : "bad"}
          label={t("lightDocker")}
          detail={probe.docker.installed ? probe.docker.version ?? "" : t("lightDockerMissing")}
        />
        <Light
          testId="light-nvidia"
          state={
            !hasGpu ? "warn" : probe.docker.nvidia_runtime ? "ok" : "bad"
          }
          label={t("lightNvidia")}
          detail={
            !hasGpu
              ? t("lightNvidiaNoGpu")
              : probe.docker.nvidia_runtime
                ? t("lightNvidiaOk")
                : t("lightNvidiaMissing")
          }
        />
        <Light
          testId="light-gpu"
          state={hasGpu ? "ok" : "warn"}
          label={t("lightGpu")}
          detail={
            hasGpu
              ? probe.gpus.map((g) => g.name).join(", ")
              : t("lightGpuNone")
          }
        />
        <Light
          testId="light-disk"
          state={diskLow ? "warn" : "ok"}
          label={t("lightDisk")}
          detail={
            probe.disk_free_gb != null
              ? t("diskLabel", { n: probe.disk_free_gb })
              : t("unknown")
          }
        />
      </div>

      {required && (
        <button
          type="button"
          data-testid="box-wizard-bootstrap"
          onClick={startBootstrap}
          disabled={starting || polling}
          className="self-start flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          style={{
            background: C.accentSubtle,
            border: `1px solid ${C.borderAccent}`,
            color: C.accent,
          }}
        >
          {starting || polling ? (
            <Loader2 size={11} className="animate-spin" />
          ) : (
            <Cpu size={11} />
          )}
          {t("startBootstrap")}
        </button>
      )}

      {status === "needs_sudo" && (
        <div
          data-testid="box-wizard-needs-sudo"
          className="text-[11px] px-3 py-2.5 rounded-lg whitespace-pre-wrap font-mono"
          style={{
            background: `${C.warning}14`,
            border: `1px solid ${C.warning}33`,
            color: C.textSecondary,
          }}
        >
          {statusMessage}
        </div>
      )}

      {error && (
        <div
          className="text-xs px-3 py-2 rounded-lg"
          style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: C.error }}
        >
          {error}
        </div>
      )}

      {log.length > 0 && (
        <div
          data-testid="box-wizard-bootstrap-log"
          className="rounded-xl px-3 py-2 max-h-48 overflow-y-auto font-mono text-[11px] flex flex-col gap-0.5"
          style={{ background: C.bgBase, border: `1px solid ${C.borderSubtle}` }}
        >
          {log.map((line, i) => (
            <div
              key={i}
              style={{
                color:
                  line.level === "error"
                    ? C.error
                    : line.level === "warn"
                      ? C.warning
                      : C.textMuted,
              }}
              className="whitespace-pre-wrap break-words"
            >
              {line.text}
            </div>
          ))}
          <div ref={logEnd} />
        </div>
      )}
    </div>
  );
}

// ── Schritt 3: Engine + Modell ───────────────────────────────────────────────

function ModelStep({
  state,
  update,
}: {
  state: BoxWizardState;
  update: (patch: Partial<BoxWizardState>) => void;
}) {
  const t = useTranslations("runtimes.boxWizard");
  const arch = archFilter(state.probe?.arch ?? null);
  const capacity = usableMemoryGb(state.probe);

  const { data, isLoading } = useQuery({
    queryKey: ["local-registry", "box-wizard", arch],
    queryFn: () => api.localRegistry.list({ enabled: true, ...(arch ? { arch } : {}) }),
  });

  // sparkrun-Einträge fliegen raus: für die gibt es switch-recipe, nicht diesen
  // Weg (siehe WIZARD_ENGINES).
  const recipes = (data?.recipes ?? []).filter((r) =>
    (WIZARD_ENGINES as readonly string[]).includes(r.engine),
  );

  return (
    <div className="flex flex-col gap-3">
      <p className="text-xs" style={{ color: C.textMuted }}>
        {arch
          ? t("modelIntro", { arch, capacity: capacity != null ? `${capacity} GB` : "?" })
          : t("modelIntroNoArch")}
      </p>

      {isLoading && (
        <div className="flex items-center gap-2 text-xs py-2" style={{ color: C.textMuted }}>
          <Loader2 size={13} className="animate-spin" />
          {t("loadingRecipes")}
        </div>
      )}

      {!isLoading && recipes.length === 0 && (
        <div className="text-xs text-center py-8" style={{ color: C.textMuted }}>
          {t("noRecipes")}
        </div>
      )}

      <div className="flex flex-col gap-2">
        {recipes.map((r) => {
          const fits = recipeFits(r, capacity);
          const active = state.recipe?.slug === r.slug;
          return (
            <button
              key={r.slug}
              type="button"
              data-testid="box-wizard-recipe"
              data-slug={r.slug}
              data-fits={fits ? "yes" : "no"}
              onClick={() =>
                update({
                  recipe: r,
                  runtimeSlug: state.runtimeSlug || slugify(r.display_name),
                  port: DEFAULT_PORTS[r.engine] ?? state.port,
                })
              }
              className="text-left rounded-xl px-3 py-2.5 cursor-pointer transition-colors"
              style={{
                background: active ? C.accentSubtle : C.borderSubtle,
                border: `1px solid ${active ? C.borderAccent : C.borderSubtle}`,
              }}
            >
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-medium" style={{ color: C.textPrimary }}>
                  {r.display_name}
                </span>
                <span
                  className="text-[10px] px-1.5 py-px rounded-sm uppercase"
                  style={{ background: C.border, color: C.textMuted, letterSpacing: "0.06em" }}
                >
                  {r.engine}
                </span>
                {!fits && (
                  <span
                    data-testid="box-wizard-fit-warning"
                    className="inline-flex items-center gap-1 text-[10px] px-1.5 py-px rounded-sm"
                    style={{
                      background: `${C.warning}14`,
                      border: `1px solid ${C.warning}33`,
                      color: C.warning,
                    }}
                  >
                    <AlertTriangle size={9} />
                    {t("tooBig", { needed: r.min_vram_gb ?? 0, have: capacity ?? 0 })}
                  </span>
                )}
              </div>
              <div className="mt-0.5 font-mono text-[11px] truncate" style={{ color: C.textMuted }}>
                {r.model_identifier}
              </div>
            </button>
          );
        })}
      </div>

      {state.recipe && (
        <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 pt-1">
          <WizField
            label={t("fieldRuntimeSlug")}
            value={state.runtimeSlug}
            onChange={(v) => update({ runtimeSlug: v })}
            mono
          />
          <div>
            <label htmlFor="box-wizard-port" className={wizardLabelClass}>
              {t("fieldPort")}
            </label>
            <input
              id="box-wizard-port"
              type="number"
              value={state.port}
              onChange={(e) => update({ port: Number(e.target.value) })}
              className={`${wizardInputClass} font-mono text-[12px]`}
              style={wizardInputStyle}
            />
          </div>
        </div>
      )}
    </div>
  );
}

// ── Schritt 4: Zusammenfassung ───────────────────────────────────────────────

function ReviewStep({
  state,
  onCreated,
  onFailed,
}: {
  state: BoxWizardState;
  onCreated: (runtimeId: string) => void;
  onFailed: (message: string) => void;
}) {
  const t = useTranslations("runtimes.boxWizard");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const queryClient = useQueryClient();

  const recipe = state.recipe!;

  // Das Kommando kommt vom Backend, nicht aus dem Frontend: es gibt genau
  // einen Renderer (services/launch_template), und der Operator sieht hier
  // exakt den String, der gleich auf seiner Box landet.
  const { data: preview, error: previewError } = useQuery({
    queryKey: ["launch-command", recipe.slug, state.runtimeSlug, state.port],
    queryFn: () =>
      api.hosts.launchCommand({
        engine: recipe.engine,
        model_identifier: recipe.model_identifier,
        slug: state.runtimeSlug,
        port: state.port,
        launch_template: recipe.launch_template,
      }),
    retry: false,
  });

  async function createAndStart() {
    if (!preview) return;
    setBusy(true);
    setError(null);
    try {
      const runtime = await api.runtimes.create({
        slug: state.runtimeSlug,
        display_name: recipe.display_name,
        runtime_type: recipe.engine,
        endpoint: `http://${state.sshHost}:${state.port}/v1`,
        // null lässt runtime_manager den Engine-Default wählen
        // (/health für llama.cpp, /v1/models sonst).
        healthcheck_path: null,
        model_identifier: recipe.model_identifier,
        container_name: `mc-${state.runtimeSlug}`,
        launch_command: preview.launch_command,
        host_id: state.hostId,
      });
      // Bestehender Start-Endpoint — kein zweiter Startweg.
      await api.runtimes.start(runtime.id);
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
      queryClient.invalidateQueries({ queryKey: ["hosts"] });
      onCreated(runtime.id);
    } catch (err) {
      const message = extractApiError(err);
      setError(message);
      onFailed(message);
    } finally {
      setBusy(false);
    }
  }

  const rows: [string, string][] = [
    [t("summaryHost"), `${state.displayName} (${state.sshHost})`],
    [t("summaryModel"), `${recipe.display_name} — ${recipe.model_identifier}`],
    [t("summaryEngine"), recipe.engine],
    [t("summaryEndpoint"), `http://${state.sshHost}:${state.port}/v1`],
  ];

  return (
    <div className="flex flex-col gap-3">
      <div
        className="rounded-xl overflow-hidden"
        style={{ border: `1px solid ${C.borderSubtle}` }}
      >
        {rows.map(([label, value]) => (
          <div
            key={label}
            className="flex items-start gap-3 px-3 py-2 text-xs"
            style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
          >
            <span className="w-24 shrink-0" style={{ color: C.textMuted }}>
              {label}
            </span>
            <span className="min-w-0 break-words" style={{ color: C.textPrimary }}>
              {value}
            </span>
          </div>
        ))}
      </div>

      <div>
        <div className={wizardLabelClass}>{t("summaryCommand")}</div>
        <pre
          data-testid="box-wizard-launch-command"
          className="rounded-xl px-3 py-2.5 text-[11px] font-mono whitespace-pre-wrap break-words"
          style={{ background: C.bgBase, border: `1px solid ${C.borderSubtle}`, color: C.textMuted }}
        >
          {preview?.launch_command ??
            (previewError ? extractApiError(previewError) : t("renderingCommand"))}
        </pre>
      </div>

      {error && (
        <div
          data-testid="box-wizard-create-error"
          className="text-xs px-3 py-2 rounded-lg"
          style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: C.error }}
        >
          {error}
        </div>
      )}

      <button
        type="button"
        data-testid="box-wizard-create"
        onClick={createAndStart}
        disabled={busy || !preview}
        className="self-end flex items-center gap-1.5 px-5 py-2 text-sm rounded-xl font-medium disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
        style={wizardBtnPrimaryStyle}
      >
        {busy ? <Loader2 size={13} className="animate-spin" /> : <Rocket size={13} />}
        {t("createAndStart")}
      </button>
    </div>
  );
}

// ── Schritt 5: Ergebnis ──────────────────────────────────────────────────────

function ResultStep({ runtimeId, onClose }: { runtimeId: string; onClose: () => void }) {
  const t = useTranslations("runtimes.boxWizard");
  const { data } = useQuery({
    queryKey: ["runtime-health", runtimeId],
    queryFn: () => api.runtimes.health(runtimeId),
    // Ein frisch gestarteter Motor lädt Gewichte — "warming" ist der
    // Normalzustand der ersten Minuten, nicht ein Fehler.
    refetchInterval: 5_000,
  });

  const stateLabel = (data as { state?: string } | undefined)?.state ?? "unknown";

  return (
    <div className="flex flex-col items-center gap-3 py-6 text-center">
      <CheckCircle2 size={32} style={{ color: C.online }} />
      <div className="text-sm font-medium" style={{ color: C.textPrimary }}>
        {t("resultTitle")}
      </div>
      <div data-testid="box-wizard-result-state" className="text-xs" style={{ color: C.textMuted }}>
        {t("resultState", { state: stateLabel })}
      </div>
      <p className="text-[11px] max-w-sm" style={{ color: C.textMuted }}>
        {t("resultHint")}
      </p>
      <button
        type="button"
        onClick={onClose}
        className="flex items-center gap-1.5 px-5 py-2 text-sm rounded-xl font-medium cursor-pointer"
        style={wizardBtnPrimaryStyle}
      >
        <Check size={13} />
        {t("done")}
      </button>
    </div>
  );
}

// ── Wizard ───────────────────────────────────────────────────────────────────

export function BoxWizard({ onClose }: { onClose: () => void }) {
  const t = useTranslations("runtimes.boxWizard");
  const queryClient = useQueryClient();
  useBodyScrollLock(true);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [onClose]);

  const [state, setState] = useState<BoxWizardState>(initialBoxWizardState);
  const [runtimeId, setRuntimeId] = useState<string | null>(null);

  // Rollen-Vorschlag (P2): erste Box im Bestand → Head, sonst Worker. Kommt
  // die Liste nicht (Netz, Rechte), gilt „keine Box" — der Operator sieht den
  // Vorschlag ohnehin und dreht ihn mit einem Klick.
  const { data: existingHosts } = useQuery<Host[]>({ queryKey: ["hosts"], queryFn: api.hosts.list });
  useEffect(() => {
    if (existingHosts === undefined) return;
    setState((s) => (s.roleSuggested ? { ...s, role: suggestRole(existingHosts.length) } : s));
  }, [existingHosts]);
  const [navError, setNavError] = useState<string | null>(null);
  const [navBusy, setNavBusy] = useState(false);

  // Stabile Identität: BaseStep hängt seine Poll-Schleife an `update` — mit
  // einer neuen Funktion je Render würde das Intervall bei jedem Re-Render
  // abgeräumt und neu gesetzt und könnte nie feuern.
  const update = useCallback(
    (patch: Partial<BoxWizardState>) => setState((s) => ({ ...s, ...patch })),
    [],
  );

  /**
   * Der Übergang 1→2 legt die Host-Zeile an — erst hier, nach erfolgreichem
   * Probe. Ein zweiter Klick legt nichts doppelt an (hostId ist gesetzt).
   */
  async function goNext() {
    setNavError(null);
    if (state.step === 0 && !state.hostId) {
      setNavBusy(true);
      try {
        const host = await api.hosts.create({
          slug: state.slug.trim(),
          display_name: state.displayName.trim(),
          kind: "ssh",
          role: state.role ?? suggestRole(existingHosts?.length ?? 0),
          ssh_host: state.sshHost.trim(),
          ssh_user: state.sshUser.trim() || null,
          ssh_key_path: state.sshKeyPath.trim() || null,
        });
        queryClient.invalidateQueries({ queryKey: ["hosts"] });
        setState((s) => ({ ...s, hostId: host.id, step: s.step + 1 }));
      } catch (err) {
        setNavError(extractApiError(err));
      } finally {
        setNavBusy(false);
      }
      return;
    }
    setState((s) => ({
      ...s,
      step: Math.min(s.step + 1, BOX_WIZARD_STEPS.length - 1),
    }));
  }

  const goBack = () => setState((s) => ({ ...s, step: Math.max(s.step - 1, 0) }));

  const isResultStep = state.step === 4;
  const isReviewStep = state.step === 3;

  return (
    <div className={wizardOverlayClass} onClick={onClose}>
      <div className={wizardBackdropClass} />
      <motion.div
        role="dialog"
        aria-modal="true"
        aria-label={t("title")}
        initial={{ opacity: 0, scale: 0.97, y: 8 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        transition={{ duration: 0.2, ease }}
        className="relative w-full max-w-2xl rounded-t-2xl sm:rounded-2xl overflow-hidden max-h-[92dvh] flex flex-col"
        style={wizardCardStyle}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="px-5 py-4 border-b" style={{ borderColor: C.borderSubtle }}>
          <div className="flex items-center justify-between mb-3">
            <h2 className="text-sm font-semibold" style={{ color: C.textPrimary }}>
              {t("title")}
            </h2>
            <button
              onClick={onClose}
              aria-label={t("close")}
              className="cursor-pointer"
              style={{ color: C.textMuted }}
            >
              <X size={16} />
            </button>
          </div>
          <div className="flex items-center gap-1.5">
            {BOX_WIZARD_STEPS.map((key, i) => {
              const active = i === state.step;
              const done = i < state.step;
              return (
                <div key={key} className="flex items-center gap-1.5 flex-1 last:flex-none">
                  <div className="flex items-center gap-1.5">
                    <div
                      className="flex items-center justify-center w-5 h-5 rounded-full text-[10px] font-medium shrink-0"
                      style={{
                        backgroundColor: active || done ? C.accent : "var(--color-bg-elevated)",
                        color: active || done ? C.onAccent : C.textMuted,
                      }}
                    >
                      {done ? <Check size={11} /> : i + 1}
                    </div>
                    <span
                      className="text-[11px] hidden sm:inline"
                      style={{ color: active ? C.textPrimary : C.textMuted }}
                    >
                      {t(`step_${key}`)}
                    </span>
                  </div>
                  {i < BOX_WIZARD_STEPS.length - 1 && (
                    <div
                      className="flex-1 h-px min-w-[8px]"
                      style={{ backgroundColor: done ? C.accent : C.border }}
                    />
                  )}
                </div>
              );
            })}
          </div>
        </div>

        <div className="p-5 overflow-y-auto flex-1">
          {state.step === 0 && <ConnectionStep state={state} update={update} />}
          {state.step === 1 && <BaseStep state={state} update={update} />}
          {state.step === 2 && <ModelStep state={state} update={update} />}
          {state.step === 3 && (
            <ReviewStep
              state={state}
              onCreated={(id) => {
                setRuntimeId(id);
                setState((s) => ({ ...s, step: 4 }));
              }}
              onFailed={() => undefined}
            />
          )}
          {state.step === 4 && runtimeId && (
            <ResultStep runtimeId={runtimeId} onClose={onClose} />
          )}
        </div>

        {!isResultStep && (
          <div
            className="flex items-center justify-between gap-3 px-5 py-4 border-t"
            style={{ borderColor: C.borderSubtle }}
          >
            <button
              onClick={goBack}
              disabled={state.step === 0}
              className="flex items-center gap-1.5 px-4 py-2 text-sm rounded-xl cursor-pointer disabled:opacity-30 disabled:cursor-not-allowed"
              style={{ color: C.textSecondary }}
            >
              <ChevronLeft size={15} /> {t("back")}
            </button>
            {navError && (
              <span
                data-testid="box-wizard-nav-error"
                className="text-[11px] flex-1 text-right"
                style={{ color: C.error }}
              >
                {navError}
              </span>
            )}
            {!isReviewStep && (
              <button
                data-testid="box-wizard-next"
                onClick={goNext}
                disabled={!canProceed(state) || navBusy}
                className="flex items-center gap-1.5 px-5 py-2 text-sm rounded-xl font-medium disabled:opacity-40 disabled:cursor-not-allowed cursor-pointer"
                style={wizardBtnPrimaryStyle}
              >
                {navBusy ? <Loader2 size={13} className="animate-spin" /> : null}
                {t("next")} <ChevronRight size={15} />
              </button>
            )}
          </div>
        )}
      </motion.div>
    </div>
  );
}
