"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { FileText, Pencil, Terminal, Globe, Bot, Wrench } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ToolEvent } from "@/lib/chatTypes";
import { LocationChips } from "./LocationChips";
import { ToolDiffView } from "./ToolDiffView";
import { ToolImages } from "./ToolImages";
import { useToolOutput } from "./toolOutputStore";

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

export function ToolRow({
  ev,
  detailLevel,
}: {
  ev: ToolEvent;
  detailLevel: "compact" | "normal" | "verbose";
}) {
  const t = useTranslations("sessions.toolCard");
  const [expanded, setExpanded] = useState(detailLevel === "verbose");
  // useState's initial value is read once, so a mounted row never reacted to
  // detailLevel changing under it (e.g. Normal -> Ausführlich never expanded
  // an already-rendered row). Re-sync on every detailLevel change; manual
  // clicks in between are untouched since this effect doesn't depend on
  // `expanded` (review finding I-3).
  useEffect(() => {
    setExpanded(detailLevel === "verbose");
  }, [detailLevel]);
  const Icon = iconFor(ev.name);
  const isError = ev.status === "error";
  // Live output only while the tool has no result: the result is the truth
  // and replaces it in place (the reducer also drops the slot then).
  const live = useToolOutput(ev.result == null ? ev.toolUseId : null);
  const diffs = ev.diffs ?? [];
  const locations = ev.locations ?? [];
  const images = ev.images ?? [];
  // The diff says what the edit did — the raw old/new strings (detail) and
  // the tool's echo of the file (result) would say it a second and third
  // time (K3). An error result still shows: it is the only explanation.
  const showDetail = diffs.length === 0;
  const showResult = ev.result != null && (diffs.length === 0 || isError);

  return (
    <div className="w-full px-4 py-1.5" data-testid="tool-row" data-running={live ? "true" : undefined}>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="w-full flex items-center gap-2 text-left bg-transparent border-0 p-0 cursor-pointer"
      >
        <Icon size={13} className="shrink-0" style={{ color: C.textMuted }} />
        <span
          className="text-xs font-mono truncate"
          style={{ color: isError ? STATUS_TEXT.error : C.textSecondary }}
        >
          {ev.title}
        </span>

        {live && (
          <span className="shrink-0 ml-auto flex items-center gap-1 text-xs" style={{ color: C.textMuted }}>
            <span
              className="inline-block w-1.5 h-1.5 rounded-full animate-pulse motion-reduce:animate-none"
              style={{ background: STATUS_TEXT.info }}
              aria-hidden="true"
            />
            {t("running")}
          </span>
        )}

        {ev.stats && (
          <span className="shrink-0 flex items-center gap-1.5 ml-auto">
            {ev.stats.additions > 0 && (
              <span className="text-[10px] font-mono font-medium" style={{ color: STATUS_TEXT.online }}>
                +{ev.stats.additions}
              </span>
            )}
            {ev.stats.deletions > 0 && (
              <span className="text-[10px] font-mono font-medium" style={{ color: STATUS_TEXT.error }}>
                −{ev.stats.deletions}
              </span>
            )}
          </span>
        )}

        {isError && (
          <span
            data-testid="tool-row-error-dot"
            className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
            style={{ background: STATUS_TEXT.error, marginLeft: ev.stats ? undefined : "auto" }}
            aria-label="Fehler"
          />
        )}
      </button>

      {(expanded || live || images.length > 0) && (
        <div className="mt-1.5 ml-5 space-y-1.5 min-w-0">
          {/* P2: a tool's pictures show without opening the card — seeing
              the screenshot is the point of the call; its JSON stays behind
              the tap. Same left edge as the rest of the card body (K9). */}
          {images.length > 0 && <ToolImages images={images} />}
          {live && <LiveOutput text={live.text} />}
          {expanded && (
            <>
              {locations.length > 0 && <LocationChips paths={locations} />}
              {diffs.map((diff, i) => (
                <ToolDiffView key={`${diff.path ?? ""}:${i}`} diff={diff} multi={diffs.length > 1} />
              ))}
              {showDetail && (
                <pre
                  className="max-h-[320px] overflow-auto scroll-quiet text-xs font-mono p-2 rounded-sm"
                  style={{ background: "var(--color-bg-elevated)", color: C.textMuted, border: `1px solid ${C.border}` }}
                >
                  {JSON.stringify(ev.detail, null, 2)}
                </pre>
              )}
              {showResult && (
                <pre
                  data-testid="tool-result"
                  className="max-h-[320px] max-w-full overflow-auto scroll-quiet text-xs font-mono p-2 rounded-sm whitespace-pre"
                  style={{
                    background: "var(--color-bg-elevated)",
                    color: isError ? STATUS_TEXT.error : C.textSecondary,
                    border: `1px solid ${isError ? C.error : C.border}`,
                  }}
                >
                  {ev.result}
                </pre>
              )}
            </>
          )}
        </div>
      )}
    </div>
  );
}
