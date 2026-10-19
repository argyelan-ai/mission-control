"use client";

/**
 * The update card under Settings → About (ADR-094, update plan PR 5).
 *
 * One card, one state at a time: available → checked (lights) → confirmed
 * with the password (bottom sheet on the phone) → waiting for quiet → the
 * phase bar → the restart gap → success, set back, interrupted or "the way
 * back failed". The backend only relays the machine's journal
 * (routers/system_update.py); which state the card shows is decided in one
 * place, lib/mcUpdate.ts — and a status call that gets no answer while MC
 * restarts means "restarting", never "failed".
 *
 * Shown only to admins and only while the backend's feature switch
 * (MC_SELF_UPDATE_ENABLED) is on: with the switch off the API answers 404
 * and the card renders nothing. A managed install (MC_UPDATE_MANAGED=deploy)
 * gets a one-line note instead of a button.
 */

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Check, ChevronDown, Copy, ExternalLink, Loader2 } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { Unfold } from "@/components/ui/Unfold";
import {
  CMD_BRIDGE,
  CMD_ROLLBACK,
  CMD_UPDATE,
  STEPS,
  backupTakenAt,
  canGoBack,
  checkKey,
  deriveRunState,
  expectArrived,
  isActive,
  isNoAnswer,
  isPreSwitch,
  isTerminal,
  logText,
  parseUpdateError,
  plainVersion,
  sortChecks,
  statusGate,
  stepOf,
  type RunState,
  type UpdateApiError,
  type UpdateCheck,
  type UpdateHistoryRun,
  type UpdatePreflight,
  type UpdateRun,
  type UpdateStatus,
} from "@/lib/mcUpdate";

export const UPDATE_STATUS_KEY = ["mc-update", "status"] as const;
const HISTORY_KEY = ["mc-update", "history"] as const;
const STEP_UP_KEY = ["mc-update", "step-up"] as const;
const RELOADED_KEY = "mc-update-reloaded";

export interface VersionInfo {
  current: string;
  latest: string | null;
  release_url: string | null;
  update_available: boolean;
}

/** Poll intervals (ms). Tests pass short ones. */
export interface UpdateTiming {
  activeMs: number;
  idleMs: number;
  noAnswerMs: number;
  /** Delay before the page reloads after a success it watched. */
  reloadMs: number;
  /** How long a "just started" run may take to show up in the status. */
  expectMs: number;
}

const DEFAULT_TIMING: UpdateTiming = {
  activeMs: 3000,
  idleMs: 30_000,
  noAnswerMs: 5000,
  reloadMs: 4000,
  expectMs: 3 * 60_000,
};

const cardStyle = {
  background: C.bgSurface,
  border: `1px solid ${C.border}`,
  borderRadius: 12,
} as const;

const inputStyle = {
  backgroundColor: C.bgDeep,
  borderWidth: 1,
  borderStyle: "solid",
  borderColor: "var(--color-border)",
  color: "var(--color-text-primary)",
} as const;

// K11: one accent button per screen, the rest are quiet text buttons.
function PrimaryButton(props: React.ButtonHTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      {...props}
      className="min-h-11 px-4 rounded-md text-sm font-medium cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed text-[var(--color-on-accent)]"
      style={{ backgroundColor: C.accent }}
    />
  );
}

function QuietButton({ danger, ...props }: React.ButtonHTMLAttributes<HTMLButtonElement> & { danger?: boolean }) {
  return (
    <button
      {...props}
      className="min-h-11 rounded-md text-sm cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
      style={{ color: danger ? STATUS_TEXT.error : "var(--color-text-secondary)" }}
    />
  );
}

function copyText(text: string): Promise<void> {
  try {
    return navigator.clipboard?.writeText(text) ?? Promise.reject(new Error("no clipboard"));
  } catch (err) {
    return Promise.reject(err);
  }
}

/** A command for the machine: mono, selectable, with a copy button. */
function CommandLine({ command }: { command: string }) {
  const t = useTranslations("settings.update");
  const [copied, setCopied] = useState(false);
  return (
    <div className="flex items-center gap-2 min-w-0" data-testid="update-command">
      <code
        className="flex-1 min-w-0 px-3 py-2 rounded-dense text-sm font-mono break-all select-all"
        style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}`, color: "var(--color-text-primary)" }}
      >
        {command}
      </code>
      <button
        type="button"
        aria-label={t("copyCommand")}
        title={t("copyCommand")}
        onClick={() =>
          copyText(command).then(
            () => {
              setCopied(true);
              notify.success(t("copied"));
              setTimeout(() => setCopied(false), 1500);
            },
            () => undefined,
          )
        }
        className="shrink-0 min-h-11 min-w-11 inline-flex items-center justify-center rounded-md cursor-pointer hover:bg-[var(--color-bg-hover)]"
        style={{ color: "var(--color-text-secondary)" }}
      >
        {copied ? <Check size={16} /> : <Copy size={16} />}
      </button>
    </div>
  );
}

function Sentence({ children, testId, tone = "primary" }: { children: React.ReactNode; testId?: string; tone?: "primary" | "secondary" }) {
  return (
    <p
      className="text-sm"
      data-testid={testId}
      style={{ color: tone === "primary" ? "var(--color-text-primary)" : "var(--color-text-secondary)", maxWidth: "72ch" }}
    >
      {children}
    </p>
  );
}

/** A state sentence led by a status dot on its first line. */
function Lead({ dot, children }: { dot: "online" | "warning" | "error"; children: React.ReactNode }) {
  return (
    <div className="flex items-start gap-3">
      <span className="h-5 flex items-center shrink-0">
        <StatusDot status={dot} />
      </span>
      <Sentence testId="update-sentence">{children}</Sentence>
    </div>
  );
}

function Muted({ children, testId }: { children: React.ReactNode; testId?: string }) {
  return (
    <p className="text-xs" data-testid={testId} style={{ color: "var(--color-text-muted)", maxWidth: "72ch" }}>
      {children}
    </p>
  );
}

/** The machine's own English words, folded away (K10: one language on screen). */
function MachineDetail({ text }: { text: string | null | undefined }) {
  const t = useTranslations("settings.update");
  if (!text) return null;
  return (
    <details className="text-xs">
      <summary className="cursor-pointer min-h-11 flex items-center" style={{ color: "var(--color-text-muted)" }}>
        {t("details")}
      </summary>
      <span className="block font-mono break-words" style={{ color: "var(--color-text-muted)" }}>
        {text}
      </span>
    </details>
  );
}

function formatStamp(iso: string | null | undefined, locale: string): string {
  if (!iso) return "";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const time = new Intl.DateTimeFormat(locale === "de" ? "de-CH" : "en-GB", {
    hour: "2-digit",
    minute: "2-digit",
  }).format(d);
  if (locale === "de") return `${String(d.getDate()).padStart(2, "0")}.${String(d.getMonth() + 1).padStart(2, "0")}. ${time}`;
  const day = new Intl.DateTimeFormat("en-GB", { day: "numeric", month: "short" }).format(d);
  return `${day}, ${time}`;
}

// ── checks (the lights) ─────────────────────────────────────────────────────

const DOT = { ok: "online", warn: "warning", fail: "error" } as const;

function CheckRow({ check }: { check: UpdateCheck }) {
  const t = useTranslations("settings.update.checks");
  const key = checkKey(check);
  const label = key && t.has(key) ? t(key) : t("generic", { id: check.id });
  const attention = check.status !== "ok";
  const head = (
    <>
      {/* h-5 = one line of text-sm: the dot sits on the first line */}
      <span className="h-5 flex items-center shrink-0">
        <StatusDot status={DOT[check.status]} size="sm" />
      </span>
      <span
        className="min-w-0 text-sm"
        style={{ color: attention ? "var(--color-text-primary)" : "var(--color-text-secondary)", maxWidth: "72ch" }}
      >
        {label}
      </span>
    </>
  );
  return (
    <li className="space-y-2" data-testid="update-light" data-check={check.id} data-status={check.status}>
      {attention && check.message ? (
        // The machine's own (English) words fold out under the row — one
        // language on screen (K10), the numbers still one tap away.
        <details className="group">
          <summary className="flex items-start gap-3 cursor-pointer list-none [&::-webkit-details-marker]:hidden">
            {head}
            <ChevronDown
              size={16}
              className="shrink-0 mt-1 transition-transform group-open:rotate-180"
              style={{ color: "var(--color-text-muted)" }}
              aria-hidden
            />
          </summary>
          <span className="block pl-6 pt-1 text-xs font-mono break-words" style={{ color: "var(--color-text-muted)" }}>
            {check.message}
          </span>
        </details>
      ) : (
        <div className="flex items-start gap-3">{head}</div>
      )}
      {attention && check.command && (
        <div className="pl-6">
          <CommandLine command={check.command} />
        </div>
      )}
    </li>
  );
}

/** Red and yellow lights in full; the green ones folded into one line (K4). */
function CheckList({ checks }: { checks: UpdateCheck[] }) {
  const t = useTranslations("settings.update");
  const [showOk, setShowOk] = useState(false);
  const sorted = sortChecks(checks);
  const attention = sorted.filter((c) => c.status !== "ok");
  const ok = sorted.filter((c) => c.status === "ok");
  return (
    <div className="space-y-3" data-testid="update-checks">
      {attention.length > 0 && (
        <ul className="space-y-3">
          {attention.map((c) => (
            <CheckRow key={`${c.id}-${c.status}`} check={c} />
          ))}
        </ul>
      )}
      {ok.length > 0 && (
        <div>
          <button
            type="button"
            onClick={() => setShowOk((v) => !v)}
            aria-expanded={showOk}
            className="min-h-11 flex items-center gap-3 text-sm cursor-pointer"
            style={{ color: "var(--color-text-secondary)" }}
            data-testid="update-checks-ok"
          >
            <span className="h-5 flex items-center shrink-0">
              <StatusDot status="online" size="sm" />
            </span>
            {t("okCount", { count: ok.length })}
            <ChevronDown size={16} style={{ transform: showOk ? "rotate(180deg)" : undefined }} className="transition-transform" />
          </button>
          <Unfold open={showOk}>
            <ul className="space-y-3 pt-1 pb-2">
              {ok.map((c) => (
                <CheckRow key={`${c.id}-${c.status}`} check={c} />
              ))}
            </ul>
          </Unfold>
        </div>
      )}
    </div>
  );
}

// ── phase bar ───────────────────────────────────────────────────────────────

function PhaseBar({ step, failed }: { step: number; failed?: boolean }) {
  const t = useTranslations("settings.update");
  const label = t(`steps.${STEPS[step]}`);
  return (
    <div className="space-y-2" data-testid="update-phase-bar" data-step={step}>
      <div
        className="flex gap-1"
        role="progressbar"
        aria-valuemin={1}
        aria-valuemax={STEPS.length}
        aria-valuenow={step + 1}
        aria-valuetext={t("stepOf", { n: step + 1, step: label })}
      >
        {STEPS.map((s, i) => (
          <span
            key={s}
            className={`h-1 flex-1 rounded-full ${i === step && !failed ? "motion-safe:animate-pulse" : ""}`}
            style={{
              backgroundColor:
                i < step ? C.accent : i === step ? (failed ? C.error : C.accent) : C.bgElevated,
              opacity: i === step && !failed ? 0.7 : 1,
            }}
          />
        ))}
      </div>
      <p className="text-xs tabular-nums" style={{ color: "var(--color-text-secondary)" }} data-testid="update-step-label">
        {t("stepOf", { n: step + 1, step: label })}
      </p>
    </div>
  );
}

// ── the password sheet (step-up) ────────────────────────────────────────────

type SheetMode = "start" | "cancel" | "rollback";

interface SheetProps {
  mode: SheetMode;
  open: boolean;
  target: string | null;
  from: string | null;
  backupWhen: string;
  runId: string | null;
  onClose: () => void;
  onStarted: (runId: string | null, upToDate: boolean) => void;
  onRolledBack: (runId: string | null) => void;
  onCancelled: () => void;
  onRecheck: (checks: UpdateCheck[] | null) => void;
}

interface SheetError {
  key: string;
  values?: Record<string, string | number>;
  command?: string | null;
}

function sheetErrorFor(e: UpdateApiError, mode: SheetMode): SheetError {
  switch (e.code) {
    case "step_up_wrong_password":
      return { key: "wrongPassword" };
    case "step_up_required":
      return { key: "stepUpRequired" };
    case "step_up_unavailable":
      return { key: "unavailable" };
    case "step_up_no_password":
    case "step_up_local_token":
    case "step_up_header_required":
      return { key: "noPassword", command: CMD_UPDATE };
    case "update_busy":
      return mode === "cancel" ? { key: "tooLate" } : { key: "busy", command: e.command };
    case "update_rate_limited":
      return { key: "rateLimited", values: { used: e.used ?? "?", limit: e.limit ?? "?" } };
    case "update_refused":
      return { key: "refused" };
    case "preflight_failed":
      return { key: "preflightFailed" };
    case "bridge_unreachable":
      return { key: "bridgeUnreachable", command: e.command ?? CMD_BRIDGE };
    case "door_token":
      return { key: "doorToken", command: e.command ?? CMD_BRIDGE };
    case "update_engine_error":
      return { key: "engine" };
  }
  if (e.status === 429) return { key: "tooManyAttempts" };
  if (e.status === 401) return { key: "wrongPassword" };
  return { key: "unknown" };
}

function UpdateSheet({
  mode,
  open,
  target,
  from,
  backupWhen,
  runId,
  onClose,
  onStarted,
  onRolledBack,
  onCancelled,
  onRecheck,
}: SheetProps) {
  const t = useTranslations("settings.update.sheet");
  const titleId = useId();
  const pwId = useId();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<SheetError | null>(null);
  const [pending, setPending] = useState(false);
  // A step-up token the backend handed back (the update service provably
  // never got the request): a retry needs no second password.
  const heldToken = useRef<{ token: string; exp: number } | null>(null);

  useEffect(() => {
    if (!open) {
      setPassword("");
      setError(null);
      setPending(false);
    }
  }, [open]);

  const held = heldToken.current && heldToken.current.exp > Date.now() ? heldToken.current : null;

  const submit = async () => {
    setPending(true);
    setError(null);
    let token = held?.token ?? null;
    let tokenExp = held?.exp ?? 0;
    heldToken.current = null;
    if (!token) {
      try {
        const r = await api.auth.stepUp(password);
        token = r.step_up_token;
        tokenExp = Date.now() + Math.max(0, r.expires_in - 5) * 1000;
      } catch (err) {
        setError(sheetErrorFor(parseUpdateError(err), mode));
        setPending(false);
        return;
      }
    }
    try {
      if (mode === "start") {
        const res = await api.system.update.start(target ?? "", token);
        onStarted(res.run_id ?? null, res.started === false);
      } else if (mode === "cancel") {
        await api.system.update.rollback(runId, token);
        onCancelled();
      } else {
        const res = await api.system.update.rollback(runId, token);
        onRolledBack(res.run_id ?? runId);
      }
    } catch (err) {
      const e = parseUpdateError(err);
      if (e.code === "bridge_unreachable" && token) heldToken.current = { token, exp: tokenExp };
      if (e.code === "preflight_failed") onRecheck(e.checks);
      setError(sheetErrorFor(e, mode));
    } finally {
      setPending(false);
      setPassword("");
    }
  };

  const title =
    mode === "start"
      ? t("startTitle", { target: plainVersion(target) })
      : mode === "cancel"
        ? t("cancelTitle")
        : t("rollbackTitle", { from: plainVersion(from) });
  const body =
    mode === "start"
      ? t("startBody")
      : mode === "cancel"
        ? t("cancelBody")
        : t("rollbackBody", { from: plainVersion(from), when: backupWhen });
  const confirm = mode === "start" ? t("startConfirm") : mode === "cancel" ? t("cancelConfirm") : t("rollbackConfirm");
  const needsPassword = !held;

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby={titleId} dismissOnOutside={false}>
      <form
        className="p-6 space-y-4 overflow-y-auto"
        data-testid="update-sheet"
        data-mode={mode}
        onSubmit={(e) => {
          e.preventDefault();
          if (!pending && (!needsPassword || password)) void submit();
        }}
      >
        <h2 id={titleId} className="text-base font-semibold" style={{ color: "var(--color-text-primary)" }}>
          {title}
        </h2>
        <p className="text-sm" style={{ color: "var(--color-text-secondary)", maxWidth: "72ch" }}>
          {body}
        </p>
        {needsPassword && (
          <div className="space-y-2">
            <label htmlFor={pwId} className="block text-sm" style={{ color: "var(--color-text-primary)" }}>
              {t("password")}
            </label>
            <input
              id={pwId}
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              className="w-full min-h-11 rounded-md px-3 text-base sm:text-sm outline-none focus:border-[var(--color-border-accent)]"
              style={inputStyle}
              data-testid="update-sheet-password"
            />
            <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
              {t("passwordHint")}
            </p>
          </div>
        )}
        {error && (
          <div className="space-y-2" role="alert" data-testid="update-sheet-error" data-error={error.key}>
            <p className="text-sm" style={{ color: STATUS_TEXT.error }}>
              {t(`errors.${error.key}`, error.values ?? {})}
            </p>
            {error.command && <CommandLine command={error.command} />}
          </div>
        )}
        <div className="flex flex-col-reverse sm:flex-row sm:justify-end gap-3 pt-2">
          <QuietButton type="button" onClick={onClose}>
            {t("close")}
          </QuietButton>
          <PrimaryButton type="submit" disabled={pending || (needsPassword && !password)} data-testid="update-sheet-confirm">
            {pending ? <Loader2 size={16} className="animate-spin mx-auto" /> : confirm}
          </PrimaryButton>
        </div>
      </form>
    </ResponsiveModal>
  );
}

// ── history ─────────────────────────────────────────────────────────────────

function History({ runs }: { runs: UpdateHistoryRun[] }) {
  const t = useTranslations("settings.update.history");
  const locale = useLocale();
  const [open, setOpen] = useState(false);
  if (runs.length === 0) return null; // K3: nothing empty on screen
  const outcome = (phase: string | null) => {
    const k = phase && isTerminal(phase) ? phase : "running";
    return t(`outcome.${k}`);
  };
  const who = (r: UpdateHistoryRun) =>
    r.started_by ? t("by", { name: r.started_by }) : r.source === "terminal" ? t("terminal") : r.source === "bridge" ? t("bridge") : null;
  return (
    <div data-testid="update-history">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="min-h-11 inline-flex items-center gap-2 text-sm cursor-pointer"
        style={{ color: "var(--color-text-secondary)" }}
      >
        <ChevronDown size={16} style={{ transform: open ? "rotate(180deg)" : undefined }} className="transition-transform" />
        {t("toggle", { count: runs.length })}
      </button>
      <Unfold open={open}>
        <ul className="space-y-3 pt-2 pb-2">
          {runs.map((r) => (
            <li key={r.run_id} className="space-y-1" data-testid="update-history-row" data-phase={r.phase ?? ""}>
              <p className="text-sm font-mono tabular-nums" style={{ color: "var(--color-text-primary)" }}>
                {plainVersion(r.from_version) || "?"} → {plainVersion(r.target_version) || "?"}
              </p>
              <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                {[outcome(r.phase), who(r), formatStamp(r.finished_at ?? r.started_at, locale)].filter(Boolean).join(" · ")}
              </p>
            </li>
          ))}
        </ul>
      </Unfold>
    </div>
  );
}

// ── the status hook ─────────────────────────────────────────────────────────

interface Expect {
  runId: string | null;
  action: "start" | "rollback";
  at: number;
}

/** Is the card on screen for this session? (AboutSection hides its old
 *  "update available" line while it is — K3, everything once.) */
export function useUpdateCardGate(enabled: boolean): "hidden" | "managed" | "shown" | "loading" {
  const q = useQuery<UpdateStatus>({
    queryKey: UPDATE_STATUS_KEY,
    queryFn: api.system.update.status,
    enabled,
    retry: false,
  });
  if (!enabled) return "hidden";
  // A missing answer (restart gap) keeps the card: statusGate says "shown".
  if (q.error) return statusGate(parseUpdateError(q.error));
  return q.data ? "shown" : "loading";
}

// ── the card ────────────────────────────────────────────────────────────────

export function MCUpdateCard({
  version,
  timing: timingOverride,
  reload,
}: {
  version?: VersionInfo;
  timing?: Partial<UpdateTiming>;
  /** Reload the page (tests pass a spy). */
  reload?: () => void;
}) {
  const timing = useMemo(() => ({ ...DEFAULT_TIMING, ...timingOverride }), [timingOverride]);
  const t = useTranslations("settings.update");
  const locale = useLocale();
  const qc = useQueryClient();

  const [noAnswerSince, setNoAnswerSince] = useState<number | null>(null);
  const [staleSince, setStaleSince] = useState<number | null>(null);
  const [expect, setExpect] = useState<Expect | null>(null);
  const [now, setNow] = useState(() => Date.now());
  const seenActive = useRef<Set<string>>(new Set());
  const maxStep = useRef<Map<string, number>>(new Map());
  const pollMs = useRef(timing.idleMs);

  const statusQ = useQuery<UpdateStatus>({
    queryKey: UPDATE_STATUS_KEY,
    queryFn: api.system.update.status,
    retry: false,
    refetchInterval: () => pollMs.current,
    refetchIntervalInBackground: true,
  });
  const status = statusQ.data ?? null;
  const statusErr = statusQ.error ? parseUpdateError(statusQ.error) : null;
  const gate = statusGate(statusErr);

  // React to every answer (and every missing answer).
  useEffect(() => {
    if (!statusQ.dataUpdatedAt) return;
    const at = Date.now();
    setNow(at);
    setNoAnswerSince(null);
    const d = statusQ.data;
    setStaleSince((prev) => (d?.stale ? (prev ?? at) : null));
    const run = d?.run;
    if (run && !isTerminal(run.phase) && !run.interrupted) seenActive.current.add(run.run_id);
    if (run) {
      const s = stepOf(run.phase);
      if (s !== null) maxStep.current.set(run.run_id, Math.max(s, maxStep.current.get(run.run_id) ?? 0));
    }
    setExpect((e) => {
      if (!e) return e;
      if (at - e.at > timing.expectMs) return null;
      if (!run || !expectArrived(e, run)) return e;
      seenActive.current.add(run.run_id);
      return null;
    });
  }, [statusQ.dataUpdatedAt, statusQ.data, timing.expectMs]);

  useEffect(() => {
    if (!statusQ.errorUpdatedAt || !statusQ.error) return;
    const e = parseUpdateError(statusQ.error);
    const at = Date.now();
    setNow(at);
    if (isNoAnswer(e)) setNoAnswerSince((prev) => prev ?? at);
  }, [statusQ.errorUpdatedAt, statusQ.error]);

  const lastRun = status?.run ?? null;
  const activeKnown = !!expect || (!!lastRun && !isTerminal(lastRun.phase));

  const state: RunState = deriveRunState({
    status,
    now,
    seenActive: !!lastRun && seenActive.current.has(lastRun.run_id),
    maxStep: lastRun ? (maxStep.current.get(lastRun.run_id) ?? 0) : 0,
    noAnswerSince,
    activeKnown,
    staleSince,
    expect,
  });

  // Poll fast while something happens; tick the clock for "since N min".
  pollMs.current = noAnswerSince !== null ? timing.noAnswerMs : isActive(state) || expect ? timing.activeMs : timing.idleMs;
  useEffect(() => {
    if (!(isActive(state) || noAnswerSince !== null)) return;
    const id = setInterval(() => setNow(Date.now()), 10_000);
    return () => clearInterval(id);
  }, [state.kind, noAnswerSince]); // eslint-disable-line react-hooks/exhaustive-deps

  // A success this page watched: reload once, so the new version's pages load.
  const doReload = reload ?? (() => window.location.reload());
  const successRun = state.kind === "success" ? state.run.run_id : null;
  const watched = successRun ? seenActive.current.has(successRun) : false;
  useEffect(() => {
    if (!successRun || !watched) return;
    let done = false;
    try {
      done = sessionStorage.getItem(RELOADED_KEY) === successRun;
    } catch {
      /* private mode — reload anyway, once per mount */
    }
    if (done) return;
    const id = setTimeout(() => {
      try {
        sessionStorage.setItem(RELOADED_KEY, successRun);
      } catch {
        /* ignore */
      }
      doReload();
    }, timing.reloadMs);
    return () => clearTimeout(id);
  }, [successRun, watched]); // eslint-disable-line react-hooks/exhaustive-deps

  const historyQ = useQuery<{ runs: UpdateHistoryRun[] }>({
    queryKey: [...HISTORY_KEY, isTerminal(lastRun?.phase) ? lastRun?.phase : "live", lastRun?.run_id ?? ""],
    queryFn: () => api.system.update.history(20),
    enabled: gate === "shown" && !!status,
    retry: false,
  });

  const stepUpQ = useQuery({
    queryKey: STEP_UP_KEY,
    queryFn: api.auth.stepUpAvailability,
    enabled: gate === "shown" && !!status,
    retry: false,
    staleTime: 5 * 60_000,
  });

  const [pf, setPf] = useState<UpdatePreflight | null>(null);
  const preflight = useMutation({
    mutationFn: () => api.system.update.preflight(version?.latest ?? null),
    onSuccess: (r) => setPf(r),
    onError: () => setPf(null),
  });

  const [sheet, setSheet] = useState<SheetMode | null>(null);

  const resume = useMutation({
    mutationFn: () => api.system.update.resume(lastRun?.run_id ?? null),
    onSettled: () => qc.invalidateQueries({ queryKey: UPDATE_STATUS_KEY }),
  });

  const refresh = useCallback(() => {
    setNoAnswerSince(null);
    void qc.invalidateQueries({ queryKey: UPDATE_STATUS_KEY });
  }, [qc]);

  if (gate === "hidden") return null;
  if (gate === "managed") {
    return (
      <section className="mc-card p-6 space-y-2" style={cardStyle} data-region="update-card" data-testid="update-card" data-state="managed">
        <h3 className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
          {t("title")}
        </h3>
        <Sentence testId="update-sentence" tone="secondary">
          {t("managed")}
        </Sentence>
      </section>
    );
  }
  if (!status && !statusErr) return null; // first answer still on its way

  const current = plainVersion(version?.current ?? lastRun?.from?.tag ?? "");
  const latest = plainVersion(version?.latest ?? pf?.target ?? "");
  const targetTag = pf?.target ?? version?.latest ?? null;
  const stepUp = stepUpQ.data;
  const stepUpReasonKey =
    stepUp && !stepUp.available
      ? stepUp.reason === "step_up_no_password"
        ? "noPassword"
        : stepUp.reason === "step_up_local_token"
          ? "localToken"
          : "stepUpUnavailable"
      : null;
  const goBackRun = canGoBack(lastRun, now) ? lastRun : null;
  const backupWhen = formatStamp(goBackRun ? (backupTakenAt(goBackRun) ?? goBackRun.created_at) : null, locale);

  let body: React.ReactNode;
  let dataState: string = state.kind;

  switch (state.kind) {
    case "idle": {
      if (statusErr && !status) {
        dataState = "unknown";
        body = (
          <>
            <Sentence testId="update-sentence" tone="secondary">
              {t("statusUnknown")}
            </Sentence>
            <QuietButton type="button" onClick={refresh}>
              {t("retry")}
            </QuietButton>
          </>
        );
        break;
      }
      const available = !!version?.update_available && !pf?.up_to_date;
      if (!available) {
        dataState = "upToDate";
        body = (
          <Sentence testId="update-sentence" tone="secondary">
            {t("upToDate", { current })}
          </Sentence>
        );
        break;
      }
      const failing = pf ? pf.checks.filter((c) => c.status === "fail").length : 0;
      dataState = preflight.isPending ? "checking" : pf ? (pf.ok ? "ready" : "blocked") : "available";
      body = (
        <>
          <div className="space-y-1">
            <Sentence testId="update-sentence">{t("available", { latest, current })}</Sentence>
            {version?.release_url && (
              <a
                href={version.release_url}
                target="_blank"
                rel="noopener noreferrer"
                className="inline-flex items-center gap-1 min-h-11 text-sm"
                style={{ color: "var(--color-text-secondary)" }}
              >
                {t("whatsNew")}
                <ExternalLink size={14} />
              </a>
            )}
          </div>
          {preflight.isPending && (
            <div className="flex items-center gap-2 text-sm" style={{ color: "var(--color-text-secondary)" }} data-testid="update-checking">
              <Loader2 size={16} className="animate-spin" />
              <span>{t("checking")}</span>
            </div>
          )}
          {preflight.isError && !preflight.isPending && <Muted testId="update-check-error">{t("checkError")}</Muted>}
          {pf && !preflight.isPending && (
            <>
              <Sentence testId="update-verdict" tone="secondary">
                {pf.ok ? t("ready") : t("blocked", { count: failing })}
              </Sentence>
              <CheckList checks={pf.checks} />
            </>
          )}
          {!preflight.isPending && (
            <div className="flex flex-wrap items-center gap-x-6 gap-y-2">
              {pf?.ok && stepUp?.available ? (
                <PrimaryButton type="button" onClick={() => setSheet("start")} data-testid="update-now">
                  {t("updateNow")}
                </PrimaryButton>
              ) : pf ? (
                <QuietButton type="button" onClick={() => preflight.mutate()} data-testid="update-check">
                  {t("checkAgain")}
                </QuietButton>
              ) : (
                <PrimaryButton type="button" onClick={() => preflight.mutate()} data-testid="update-check">
                  {t("check")}
                </PrimaryButton>
              )}
            </div>
          )}
          {pf?.ok && stepUpReasonKey && (
            <div className="space-y-2" data-testid="update-terminal-only">
              <Muted>{t(stepUpReasonKey)}</Muted>
              <CommandLine command={CMD_UPDATE} />
            </div>
          )}
        </>
      );
      break;
    }
    case "pending": {
      const target = plainVersion(targetTag);
      const from = plainVersion(lastRun?.from?.tag ?? "");
      body = (
        <div className="flex items-center gap-2">
          <Loader2 size={16} className="animate-spin shrink-0" style={{ color: "var(--color-text-secondary)" }} />
          <Sentence testId="update-sentence">
            {state.action === "rollback"
              ? t("goingBack", { from })
              : target
                ? t("preparing", { target })
                : t("preparingNoTarget")}
          </Sentence>
        </div>
      );
      break;
    }
    case "waiting": {
      const parts: string[] = [];
      if (state.work.tasks) parts.push(t("waitingTasks", { count: state.work.tasks }));
      if (state.work.group_rounds) parts.push(t("waitingRounds", { count: state.work.group_rounds }));
      const target = plainVersion(state.run.target?.tag ?? targetTag);
      const preparing = state.run.phase === "preflight";
      body = (
        <>
          <Sentence testId="update-sentence">
            {preparing
              ? target
                ? t("preparing", { target })
                : t("preparingNoTarget")
              : parts.length
                ? t("waiting", { what: parts.join(" · ") })
                : t("waitingQuiet")}
          </Sentence>
          {!preparing && <Muted>{t("waitingHint")}</Muted>}
          {state.stale && <Muted testId="update-stale">{t("staleNote")}</Muted>}
          {isPreSwitch(state.run.phase) && (
            <QuietButton type="button" onClick={() => setSheet("cancel")} data-testid="update-cancel">
              {t("cancel")}
            </QuietButton>
          )}
        </>
      );
      break;
    }
    case "running": {
      const target = plainVersion(state.run.target?.tag ?? targetTag);
      body = (
        <>
          <Sentence testId="update-sentence">{t("running", { target })}</Sentence>
          <PhaseBar step={state.step} />
          {state.step >= 2 && <Muted>{t("restartSoon")}</Muted>}
          {state.stale && <Muted testId="update-stale">{t("staleNote")}</Muted>}
          {isPreSwitch(state.run.phase) && (
            <QuietButton type="button" onClick={() => setSheet("cancel")} data-testid="update-cancel">
              {t("cancel")}
            </QuietButton>
          )}
        </>
      );
      break;
    }
    case "rollingBack": {
      const from = plainVersion(state.run.from?.tag ?? "");
      const stepName = state.step !== null ? t(`steps.${STEPS[state.step]}`) : null;
      body = (
        <>
          <Sentence testId="update-sentence">
            {stepName ? t("rollingBack", { step: stepName, from }) : t("rollingBackNoStep", { from })}
          </Sentence>
          {state.step !== null && <PhaseBar step={state.step} failed />}
          <div className="flex items-center gap-2 text-sm" style={{ color: "var(--color-text-secondary)" }}>
            <Loader2 size={16} className="animate-spin" />
          </div>
        </>
      );
      break;
    }
    case "restarting": {
      const minutes = Math.floor((now - state.since) / 60_000);
      const step = lastRun ? Math.max(stepOf(lastRun.phase) ?? 0, maxStep.current.get(lastRun.run_id) ?? 0) : null;
      body = (
        <>
          <div className="flex items-center gap-2">
            <Loader2 size={16} className="animate-spin shrink-0" style={{ color: "var(--color-text-secondary)" }} />
            <Sentence testId="update-sentence">{t("restarting")}</Sentence>
          </div>
          {step !== null && <PhaseBar step={Math.max(step, 3)} />}
          {minutes >= 1 && <Muted testId="update-asking-since">{t("restartingSince", { minutes })}</Muted>}
        </>
      );
      break;
    }
    case "restartTimeout": {
      body = (
        <>
          <Sentence testId="update-sentence">{t("restartTimeout")}</Sentence>
          <CommandLine command={status?.command ?? "./install.sh --resume"} />
          <QuietButton type="button" onClick={refresh}>
            {t("askAgain")}
          </QuietButton>
        </>
      );
      break;
    }
    case "success": {
      const v = plainVersion(state.run.target?.tag ?? version?.current ?? "");
      const when = formatStamp(backupTakenAt(state.run) ?? state.run.finished_at, locale);
      body = (
        <>
          <Lead dot="online">{t("success", { version: v })}</Lead>
          {when && <Muted testId="update-backup">{t("successBackup", { when })}</Muted>}
          {watched && <Muted>{t("reloading")}</Muted>}
        </>
      );
      break;
    }
    case "rolledBack": {
      const from = plainVersion(state.run.from?.tag ?? "");
      const stepName = state.step !== null ? t(`steps.${STEPS[state.step]}`) : null;
      body = (
        <>
          <Lead dot={state.requested ? "online" : "error"}>
            {state.requested
              ? t("rolledBackRequested", { from })
              : stepName
                ? t("rolledBack", { step: stepName, from })
                : t("rolledBackNoStep", { from })}
          </Lead>
          {!state.requested && <LogBlock run={state.run} />}
        </>
      );
      break;
    }
    case "aborted": {
      const kind = state.run.failure_kind;
      const key = kind === "network" || kind === "cancelled" || kind === "interrupted" ? kind : "error";
      const runCurrent = plainVersion(state.run.from?.tag ?? current);
      body = (
        <>
          <Sentence testId="update-sentence">{t(`aborted.${key}`, { current: runCurrent })}</Sentence>
          {key === "network" && <Muted>{t("retryNote")}</Muted>}
          {key === "error" && <MachineDetail text={state.run.error} />}
          {version?.update_available && (
            <QuietButton
              type="button"
              onClick={() => {
                // A finished run's result gives way to a fresh check.
                seenActive.current.delete(state.run.run_id);
                preflight.mutate();
                qc.setQueryData<UpdateStatus>(UPDATE_STATUS_KEY, (s) => (s ? { ...s, run: null } : s));
              }}
              data-testid="update-retry"
            >
              {t("retry")}
            </QuietButton>
          )}
        </>
      );
      break;
    }
    case "interrupted": {
      body = state.bridgeReachable ? (
        <>
          <Lead dot="warning">{t("interrupted")}</Lead>
          <QuietButton type="button" onClick={() => resume.mutate()} disabled={resume.isPending} data-testid="update-resume">
            {t("resumeNow")}
          </QuietButton>
        </>
      ) : (
        <>
          <Lead dot="warning">{t("interruptedNoBridge")}</Lead>
          <CommandLine command={state.command} />
        </>
      );
      break;
    }
    case "rollbackFailed": {
      body = (
        <>
          <Lead dot="error">{t("rollbackFailed")}</Lead>
          <CommandLine command={status?.command ?? CMD_ROLLBACK} />
          <LogBlock run={state.run} />
        </>
      );
      break;
    }
  }

  const showGoBack =
    !!goBackRun && (state.kind === "success" || state.kind === "idle") && stepUp?.available === true;

  return (
    <section
      className="mc-card p-6 space-y-4"
      style={cardStyle}
      data-region="update-card"
      data-testid="update-card"
      data-state={dataState}
    >
      <h3 className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
        {t("title")}
      </h3>
      {body}
      {showGoBack && goBackRun && (
        <QuietButton danger type="button" onClick={() => setSheet("rollback")} data-testid="update-go-back">
          {t("goBack", { version: plainVersion(goBackRun.from?.tag) })}
        </QuietButton>
      )}
      <History runs={historyQ.data?.runs ?? []} />
      <UpdateSheet
        mode={sheet ?? "start"}
        open={sheet !== null}
        target={targetTag}
        from={goBackRun?.from?.tag ?? lastRun?.from?.tag ?? null}
        backupWhen={backupWhen}
        runId={lastRun?.run_id ?? null}
        onClose={() => setSheet(null)}
        onStarted={(runId, upToDate) => {
          setSheet(null);
          if (upToDate) {
            notify.info(t("sheet.upToDate"));
            setPf(null);
            return;
          }
          setPf(null);
          setExpect({ runId, action: "start", at: Date.now() });
          void qc.invalidateQueries({ queryKey: UPDATE_STATUS_KEY });
        }}
        onRolledBack={(runId) => {
          setSheet(null);
          setExpect({ runId, action: "rollback", at: Date.now() });
          void qc.invalidateQueries({ queryKey: UPDATE_STATUS_KEY });
        }}
        onCancelled={() => {
          setSheet(null);
          void qc.invalidateQueries({ queryKey: UPDATE_STATUS_KEY });
        }}
        onRecheck={(checks) => {
          if (checks && pf) setPf({ ...pf, ok: false, checks: [...pf.checks.filter((c) => !checks.some((x) => x.id === c.id)), ...checks] });
        }}
      />
    </section>
  );
}

/** The journal's last lines, with "copy log" (never the raw engine log). */
function LogBlock({ run }: { run: UpdateRun }) {
  const t = useTranslations("settings.update");
  const lines = (run.events ?? []).slice(-6);
  if (lines.length === 0 && !run.error) return null;
  return (
    <div className="space-y-2" data-testid="update-log">
      <pre
        className="px-3 py-2 rounded-dense text-xs font-mono whitespace-pre-wrap break-words overflow-x-hidden"
        style={{ backgroundColor: C.term, color: C.termFg }}
      >
        {lines.map((e) => [e.phase, e.event, e.detail].filter(Boolean).join("  ")).join("\n")}
      </pre>
      <QuietButton
        type="button"
        onClick={() => copyText(logText(run)).then(() => notify.success(t("logCopied")), () => undefined)}
        data-testid="update-copy-log"
      >
        {t("copyLog")}
      </QuietButton>
    </div>
  );
}
