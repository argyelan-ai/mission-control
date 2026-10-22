"use client";

import { memo, useEffect, useLayoutEffect, useRef } from "react";
import { useLocale, useTranslations } from "next-intl";
import { FileText, Pencil, Terminal, Globe, Bot, Wrench } from "lucide-react";
import { C, STATUS_TEXT, stepFailedStyle } from "@/lib/colors";
import { formatSpan } from "@/lib/taskDetail/stateLine";
import { useNow } from "@/hooks/useNow";
import type { ToolEvent } from "@/lib/chatTypes";
import { LocationChips } from "./LocationChips";
import { ToolDiffView } from "./ToolDiffView";
import { ToolImages } from "./ToolImages";
import { useToolOutput } from "./toolOutputStore";
import { useAnchoredToggle } from "./useScrollAnchor";

/* Claude Code names its tools in PascalCase, omp/ACP in lower case (the
   bridge maps ACP kinds onto omp's names: bash, read, edit, write, grep). */
const TOOL_ICON: Record<string, typeof Wrench> = {
  Read: FileText,
  Edit: Pencil,
  MultiEdit: Pencil,
  Write: Pencil,
  Bash: Terminal,
  WebSearch: Globe,
  WebFetch: Globe,
  Task: Bot,
  read: FileText,
  edit: Pencil,
  write: Pencil,
  bash: Terminal,
};

function iconFor(name: string) {
  return TOOL_ICON[name] ?? Wrench;
}

/** Within this many px of the bottom counts as "following the tail". */
const TAIL_SLACK_PX = 8;

/**
 * The running command's output so far (ACP live tool output). Terminal
 * colours — it IS terminal output — and terminal layout: lines never wrap
 * (a pytest dot row wrapped into unreadable fragments), the box scrolls
 * sideways INSIDE itself (the page never does), about 11 lines tall.
 * Tail-anchored like a terminal: each snapshot pins the newest line — unless
 * the operator scrolled up to read, then it stays where they are until they
 * scroll back down. `aria-live` stays off on purpose: a screen reader would
 * otherwise read every snapshot twice a second.
 */
function LiveOutput({ text }: { text: string }) {
  const t = useTranslations("sessions.toolCard");
  const ref = useRef<HTMLPreElement>(null);
  const following = useRef(true);
  useLayoutEffect(() => {
    const el = ref.current;
    if (el && following.current) el.scrollTop = el.scrollHeight;
  }, [text]);
  return (
    <pre
      ref={ref}
      data-testid="tool-live-output"
      aria-label={t("liveOutput")}
      onScroll={(e) => {
        const el = e.currentTarget;
        following.current = el.scrollHeight - el.scrollTop - el.clientHeight <= TAIL_SLACK_PX;
      }}
      className="max-h-48 max-w-full overflow-auto scroll-quiet text-xs font-mono p-2 rounded-sm whitespace-pre"
      style={{ background: C.term, color: C.termFg }}
    >
      {text}
    </pre>
  );
}

type DetailLevelProp = "compact" | "normal" | "verbose";

/** A row whose body is fetched on open (task sections, spec A §4.3). */
export interface LazyToolDetail {
  /** The full event once loaded, else null. */
  event: ToolEvent | null;
  loading: boolean;
  load(): void;
}

/** What a task section knows about a tool without a result (spec A §2.2). */
export type ToolRunState = { kind: "running"; since: string | null } | { kind: "noResult" };

/** "running" / "running · for 3 min" — ticks on its own, the row does not. */
function RunningLabel({ since }: { since: string | null }) {
  const tTool = useTranslations("sessions.toolCard");
  const tSteps = useTranslations("sessions.taskSteps");
  const locale = useLocale();
  const now = useNow(30_000);
  const sinceMs = since ? Date.parse(since) : NaN;
  const seconds = Number.isNaN(sinceMs) ? null : (now - sinceMs) / 1000;
  if (seconds != null && seconds >= 60) {
    return <>{tSteps("runningFor", { span: formatSpan(seconds, locale) ?? "" })}</>;
  }
  return <>{tTool("running")}</>;
}

export const ToolRow = memo(function ToolRow({
  ev,
  detailLevel,
  lazyDetail,
  liveOutput = true,
  runningTone = "info",
  statsTone = "status",
  runState = null,
  anchored = false,
  wideIcon = false,
}: {
  ev: ToolEvent;
  detailLevel: DetailLevelProp;
  /** Body fetched on open; until then a one-line loading text, never `{}`. */
  lazyDetail?: LazyToolDetail;
  /** false (task sections): no live output and no opening by itself — not
   *  on Ausführlich, not while running (S9). */
  liveOutput?: boolean;
  /** The "running" pulse: info blue in a chat turn, neutral in a section (S10). */
  runningTone?: "info" | "neutral";
  /** Diff numbers in status colours, or muted (a section's steps region, S10). */
  statsTone?: "status" | "muted";
  runState?: ToolRunState | null;
  /** Keep the tapped row in place when it opens (spec A M3). */
  anchored?: boolean;
  /** Task sections: a 16 px icon slot, so the title sits on the section's
   *  40 px text edge like the chapter lines (K9, spec A §2.6). */
  wideIcon?: boolean;
}) {
  const t = useTranslations("sessions.toolCard");
  const tSteps = useTranslations("sessions.taskSteps");
  const { open: expanded, setOpen: setExpanded, toggle, ref } = useAnchoredToggle(
    liveOutput && detailLevel === "verbose",
    anchored,
  );
  // useState's initial value is read once, so a mounted row never reacted to
  // detailLevel changing under it (e.g. Normal -> Ausführlich never expanded
  // an already-rendered row). Re-sync on every detailLevel change; manual
  // clicks in between are untouched since this effect doesn't depend on
  // `expanded` (review finding I-3). A section row never opens by itself.
  useEffect(() => {
    if (liveOutput) setExpanded(detailLevel === "verbose");
  }, [detailLevel, liveOutput, setExpanded]);
  const needsLoad = !!lazyDetail && expanded && !lazyDetail.event && !lazyDetail.loading;
  const load = lazyDetail?.load;
  useEffect(() => {
    if (needsLoad) load?.();
  }, [needsLoad, load]);
  const Icon = iconFor(ev.name);
  const isError = ev.status === "error";
  const failed = stepFailedStyle();
  // Live output only while the tool has no result: the result is the truth
  // and replaces it in place (the reducer also drops the slot then).
  const live = useToolOutput(liveOutput && ev.result == null ? ev.toolUseId : null);
  const body: ToolEvent | null = lazyDetail ? lazyDetail.event : ev;
  const diffs = body?.diffs ?? [];
  const locations = body?.locations ?? [];
  const images = ev.images ?? [];
  // The diff says what the edit did — the raw old/new strings (detail) and
  // the tool's echo of the file (result) would say it a second and third
  // time (K3). An error result still shows: it is the only explanation.
  const showDetail = !!body && diffs.length === 0;
  const showResult = !!body && body.result != null && (diffs.length === 0 || isError);
  const running = !!live || runState?.kind === "running";
  const pulse = runningTone === "neutral" ? C.textMuted : STATUS_TEXT.info;

  return (
    <div className="w-full px-4" data-testid="tool-row" data-running={running ? "true" : undefined}>
      <button
        ref={ref as React.RefObject<HTMLButtonElement>}
        type="button"
        onClick={toggle}
        aria-expanded={expanded}
        className="w-full min-h-11 flex items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer"
      >
        {wideIcon ? (
          <span className="w-4 shrink-0 flex justify-center" aria-hidden="true">
            <Icon size={13} style={{ color: C.textMuted }} />
          </span>
        ) : (
          <Icon size={13} className="shrink-0" style={{ color: C.textMuted }} />
        )}
        <span
          className="text-xs font-mono truncate"
          style={{ color: isError ? failed.title : C.textSecondary }}
        >
          {ev.title}
        </span>

        {running && (
          <span className="shrink-0 ml-auto flex items-center gap-1 text-xs" style={{ color: C.textMuted }}>
            <span
              className="inline-block w-1.5 h-1.5 rounded-full animate-pulse motion-reduce:animate-none"
              style={{ background: pulse }}
              aria-hidden="true"
            />
            {runState?.kind === "running" ? <RunningLabel since={runState.since} /> : t("running")}
          </span>
        )}

        {!running && runState?.kind === "noResult" && (
          <span className="shrink-0 ml-auto text-xs" style={{ color: C.textMuted }}>
            {tSteps("noResult")}
          </span>
        )}

        {ev.stats && (
          <span className="shrink-0 flex items-center gap-1.5 ml-auto">
            {ev.stats.additions > 0 && (
              <span
                className="text-[10px] font-mono font-medium"
                style={{ color: statsTone === "muted" ? C.textMuted : STATUS_TEXT.online }}
              >
                +{ev.stats.additions}
              </span>
            )}
            {ev.stats.deletions > 0 && (
              <span
                className="text-[10px] font-mono font-medium"
                style={{ color: statsTone === "muted" ? C.textMuted : STATUS_TEXT.error }}
              >
                −{ev.stats.deletions}
              </span>
            )}
          </span>
        )}

        {isError && (
          <span
            data-testid="tool-row-error-dot"
            className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: failed.dot, marginLeft: ev.stats ? undefined : "auto" }}
            role="img"
            aria-label={t("failed")}
          />
        )}
      </button>

      {(expanded || live || images.length > 0) && (
        <div className={`pb-2 ${wideIcon ? "ml-6" : "ml-5"} space-y-1.5 min-w-0`}>
          {/* P2: a tool's pictures show without opening the card — seeing
              the screenshot is the point of the call; its JSON stays behind
              the tap. Same left edge as the rest of the card body (K9). */}
          {images.length > 0 && <ToolImages images={images} />}
          {live && <LiveOutput text={live.text} />}
          {expanded && lazyDetail && !body && (
            <p data-testid="tool-row-loading" className="m-0 text-xs" style={{ color: C.textMuted }}>
              {lazyDetail.loading ? tSteps("loading") : null}
            </p>
          )}
          {expanded && body && (
            <>
              {locations.length > 0 && <LocationChips paths={locations} />}
              {diffs.map((diff, i) => (
                <ToolDiffView key={`${diff.path ?? ""}:${i}`} diff={diff} multi={diffs.length > 1} />
              ))}
              {showDetail && body.detail != null && (
                <pre
                  className="max-h-[320px] overflow-auto scroll-quiet text-xs font-mono p-2 rounded-sm"
                  style={{ background: "var(--color-bg-elevated)", color: C.textMuted, border: `1px solid ${C.border}` }}
                >
                  {JSON.stringify(body.detail, null, 2)}
                </pre>
              )}
              {showResult && (
                <pre
                  data-testid="tool-result"
                  className="max-h-[320px] max-w-full overflow-auto scroll-quiet text-xs font-mono p-2 rounded-sm whitespace-pre"
                  style={{
                    background: "var(--color-bg-elevated)",
                    color: isError ? failed.resultText : C.textSecondary,
                    border: `1px solid ${isError ? failed.resultBorder : C.border}`,
                  }}
                >
                  {body.result}
                </pre>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
});
