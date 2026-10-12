"use client";

/**
 * ToolDiffView — the change ONE tool call made to ONE file, as a compact
 * unified diff inside its tool card.
 *
 * One component for every harness: the backend (`chat_diff.py`) turns an ACP
 * edit's whole-file old/new text and a Claude Edit/MultiEdit/Write into the
 * same `ToolDiff`, so nothing here knows which agent wrote the change.
 *
 * Phone first: lines wrap instead of scrolling sideways (no horizontal page
 * scroll at 393 px), the first `DIFF_PREVIEW_LINES` show, the rest sit behind
 * one text button. Colour carries exactly one meaning — added/removed — as a
 * faint row tint plus the +/− sign, so it still reads without colour.
 */
import { useState } from "react";
import { useTranslations } from "next-intl";
import { C, STATUS_TEXT, alpha } from "@/lib/colors";
import type { ToolDiff } from "@/lib/chatTypes";

/** Lines visible before "show all" — about one phone screen of code. */
export const DIFF_PREVIEW_LINES = 12;

/** `ch`-based hanging indent for one code line (a tab counts as 8 — the browser default tab-size). */
export function hangingIndent(text: string): { paddingLeft: string; textIndent: string } {
  const lead = /^[ \t]*/.exec(text)?.[0] ?? "";
  const cols = [...lead].reduce((n, c) => n + (c === "\t" ? 8 : 1), 0) + 2;
  return { paddingLeft: `${cols}ch`, textIndent: `-${cols}ch` };
}

function splitPath(path: string): { dir: string; base: string } {
  const i = path.lastIndexOf("/");
  return i < 0 ? { dir: "", base: path } : { dir: path.slice(0, i + 1), base: path.slice(i + 1) };
}

/**
 * `multi`: the card holds diffs of SEVERAL files — then each diff names its
 * file and carries its own +/− counts. With one file the card title already
 * names it and the row badge already counts it, so the diff shows neither
 * (K3: each fact once).
 *
 * `initialAll`: every line shown, no "show less" — for the Changes panel,
 * where the diff IS the view the operator tapped into, not a preview in a card.
 */
export function ToolDiffView({
  diff,
  multi,
  initialAll = false,
}: {
  diff: ToolDiff;
  multi: boolean;
  initialAll?: boolean;
}) {
  const t = useTranslations("sessions.toolCard");
  const [all, setAll] = useState(initialAll);
  const lines = all ? diff.lines : diff.lines.slice(0, DIFF_PREVIEW_LINES);
  const hidden = diff.lines.length - lines.length;
  const label = diff.kind === "create" ? t("diffCreate") : diff.kind === "write" ? t("diffWrite") : null;
  const { dir, base } = splitPath(diff.path ?? "");

  return (
    <div
      data-testid="tool-diff"
      className="rounded-sm overflow-hidden"
      style={{ border: `1px solid ${C.border}` }}
    >
      {((multi && diff.path) || label || multi) && (
        <div
          className="flex items-center gap-2 px-2 py-1 text-xs"
          style={{ background: C.bgElevated, borderBottom: `1px solid ${C.borderSubtle}` }}
        >
          {multi && diff.path && (
            <span className="font-mono truncate min-w-0" title={diff.path} style={{ color: C.textSecondary }}>
              <span style={{ color: C.textMuted }}>{dir}</span>
              {base}
            </span>
          )}
          {label && (
            <span className="shrink-0" style={{ color: C.textMuted }}>
              {label}
            </span>
          )}
          {multi && (
            <span className="ml-auto shrink-0 flex items-center gap-2 font-mono tabular-nums">
              {diff.additions > 0 && <span style={{ color: STATUS_TEXT.online }}>+{diff.additions}</span>}
              {diff.deletions > 0 && <span style={{ color: STATUS_TEXT.error }}>−{diff.deletions}</span>}
            </span>
          )}
        </div>
      )}

      <div className="font-mono text-xs py-1" role="group" aria-label={diff.path ?? undefined}>
        {lines.map((line, i) => {
          if (line.t === "hunk") {
            // A gap between two changed regions: a quiet rule, not the raw
            // "@@ -2,1 +2,2 @@" (line numbers of an excerpt mean nothing here).
            return (
              <div
                key={i}
                data-line="hunk"
                aria-hidden="true"
                className={i === 0 ? "hidden" : "my-1 mx-2 h-px"}
                style={{ background: C.borderSubtle }}
              />
            );
          }
          const add = line.t === "add";
          const del = line.t === "del";
          return (
            <div
              key={i}
              data-line={line.t}
              className="flex"
              style={{
                background: add ? alpha(C.online, 0.12) : del ? alpha(C.error, 0.12) : undefined,
                color: add || del ? C.textPrimary : C.textMuted,
              }}
            >
              <span
                className="w-4 shrink-0 text-center select-none"
                aria-hidden="true"
                style={{ color: add ? STATUS_TEXT.online : del ? STATUS_TEXT.error : C.textDim }}
              >
                {add ? "+" : del ? "−" : " "}
              </span>
              {/* Hanging indent: a wrapped code line continues under its OWN
                  code column (its leading whitespace + 2), never under the
                  +/− gutter. Negative text-indent pulls only the first line
                  back; the padding holds every following one. */}
              <span
                className="min-w-0 pr-2 whitespace-pre-wrap break-words"
                style={hangingIndent(line.text)}
              >
                {line.text || " "}
              </span>
            </div>
          );
        })}
      </div>

      {!initialAll && (hidden > 0 || all) && diff.lines.length > DIFF_PREVIEW_LINES && (
        <button
          type="button"
          onClick={() => setAll((v) => !v)}
          className="w-full min-h-[44px] md:min-h-0 px-2 py-1 text-left text-xs font-medium cursor-pointer bg-transparent border-0"
          style={{ color: C.textSecondary, borderTop: `1px solid ${C.borderSubtle}` }}
        >
          {all ? t("diffShowLess") : t("diffShowAll", { count: diff.lines.length })}
        </button>
      )}
      {diff.truncated && (
        <p className="px-2 py-1 text-xs" style={{ color: C.textMuted, borderTop: `1px solid ${C.borderSubtle}` }}>
          {t("diffTruncated")}
        </p>
      )}
    </div>
  );
}
