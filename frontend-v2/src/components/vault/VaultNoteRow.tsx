"use client";

/**
 * VaultNoteRow — Editorial Codex row (M.3 T8 redesign, 2026-05-15)
 *
 * Layout:
 *   ┌─────────┬──┬───────────────────────────────────────┐
 *   │  Day    │ │   [type-chip]  agent                  │
 *   │  MON    │ │                                       │
 *   │  Year   │ │   Title (sans bold)                   │
 *   │         │ │   Excerpt (sans, 2-line clamp)        │
 *   │         │ │   #tag  #tag  #tag                    │
 *   └─────────┴──┴───────────────────────────────────────┘
 *      ↑ Marginalia (88px) │ vertical hairline │ Content
 *
 * The marginalia day number is the signature element — magazine-index
 * weight that anchors each entry. Hover/selected colour-shifts it to
 * the owning agent's hue (CSS var) so identity reads at a glance.
 *
 * No horizontal divider per row — the vertical hairline + breathing
 * room between rows is enough separation without the activity-feed
 * "stacked cards" pattern.
 */

import { useMemo } from "react";
import { useLocale, useTranslations } from "next-intl";
import { motion } from "framer-motion";
import type { VaultNote } from "@/lib/types";
import { colorForAgent } from "./agentColors";
import { TASK_WIKILINK_RE } from "./wikilinks";

import { alpha, C } from "@/lib/colors";
// ── Helpers ────────────────────────────────────────────────────────────────────

const UUID_LIKE_RE = /^[0-9a-f]{6,}-[0-9a-f]{4,}/i;

/**
 * Lightweight YAML frontmatter parser — handles the simple key/value subset
 * vault notes use (no nested objects, no arrays-on-one-line). Returns an
 * empty object when no frontmatter block is found.
 *
 * Why not pull in `js-yaml`: ~30 KB gzipped is far too heavy for parsing
 * a handful of string fields per row. We control the writer (backend
 * Jinja template), so the format is predictable.
 */
function parseFrontmatter(content: string): Record<string, string> {
  const match = content.match(/^---\s*\n([\s\S]*?)\n---/);
  if (!match) return {};
  const fm: Record<string, string> = {};
  for (const line of match[1].split("\n")) {
    const kv = line.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
    if (!kv) continue;
    let value = kv[2].trim();
    // Strip surrounding quotes (single or double).
    if (
      (value.startsWith('"') && value.endsWith('"')) ||
      (value.startsWith("'") && value.endsWith("'"))
    ) {
      value = value.slice(1, -1);
    }
    if (value !== "") fm[kv[1]] = value;
  }
  return fm;
}

/** First `# Heading` from the markdown body (frontmatter already stripped). */
function firstH1FromContent(content: string): string | null {
  const body = content.replace(/^---[\s\S]*?---\n?/, "");
  const match = body.match(/^#\s+(.+?)\s*$/m);
  return match ? match[1].trim() : null;
}

function isUuidLikeStem(stem: string): boolean {
  return UUID_LIKE_RE.test(stem);
}

function humanizeStem(stem: string): string {
  return stem
    .replace(/[-_]+/g, " ")
    .replace(/^\d{4}-\d{2}-\d{2}[-_]?/, "")
    .trim();
}

/**
 * Derive a human-readable title from a vault path stem. Last-resort fallback
 * when neither frontmatter nor an H1 heading is available — leaves UUID-like
 * stems alone so the caller can show "Untitled" instead of letter-noise.
 */
export function titleFromPath(path: string): string {
  const stem = path.split("/").pop()?.replace(/\.md$/i, "") ?? path;
  const cleaned = humanizeStem(stem);
  return cleaned || path;
}

/**
 * Resolve the best-available title for a note, in priority order:
 *   1. frontmatter.title           (canonical, set by the author)
 *   2. first H1 in body            (markdown convention)
 *   3. path stem (humanised)       (legacy / no-frontmatter notes)
 *   4. "Untitled"                  (UUID-only stems — pure noise)
 *
 * Works with the list/search responses where `content` is the full body
 * (list) or a snippet (search). Snippets without frontmatter fall through
 * gracefully to the path-stem path.
 */
export function titleFromNote(note: {
  path: string;
  content: string;
  title?: string;
}): string {
  // Primary source: backend's denormalised `title` column (from frontmatter
  // at index time). Falls through to body-side parsing only for old indexes
  // that don't expose the field, or snippets that include raw frontmatter.
  if (note.title && note.title.trim()) return note.title.trim();
  const fm = parseFrontmatter(note.content);
  if (fm.title) return fm.title;
  const h1 = firstH1FromContent(note.content);
  if (h1) return h1;
  const stem = note.path.split("/").pop()?.replace(/\.md$/i, "") ?? "";
  const cleaned = humanizeStem(stem);
  if (!cleaned) return "Untitled";
  if (isUuidLikeStem(stem.replace(/^\d{4}-\d{2}-\d{2}[-_]?/, ""))) return "Untitled";
  return cleaned;
}

/** Note type in the UI language; an unknown type is shown as stored. */
export function noteTypeLabel(
  t: { (key: string): string; has: (key: string) => boolean },
  type: string,
): string {
  const key = `noteType.${type}`;
  return t.has(key) ? t(key) : type;
}

/** Split space-joined tags string into array, filtering empties. */
export function parseTags(tagsStr: string): string[] {
  return tagsStr.split(" ").filter(Boolean);
}

/**
 * Plain-text preview of a note for the list (2-line clamp). Drops the
 * frontmatter, headings, attachment embeds and image syntax, and unwraps
 * Markdown (quotes, emphasis, code, links, wikilinks, list bullets) so the
 * row reads as text — not as "> **Für …**" or "!../attachments/x.html".
 */
export function excerptFromContent(content: string, maxChars = 200): string {
  const stripped = content
    .replace(/^---[\s\S]*?---\n?/, "")
    .replace(/^#+\s+.*$/gm, "")
    .replace(/!\[\[[^\]]*\]\]/g, "")
    .replace(/!\[[^\]]*\]\([^)]*\)/g, "")
    .replace(/\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g, (_m, name: string, alias?: string) =>
      alias ?? (TASK_WIKILINK_RE.test(name) ? name.slice(5, 13) : name),
    )
    .replace(/\[([^\]]+)\]\([^)]*\)/g, "$1")
    .replace(/<((?:https?|mailto):[^>]+)>/gi, "$1")
    .replace(/^\s{0,3}>\s?/gm, "")
    .replace(/^\s*(?:[-*+]|\d+\.)\s+/gm, "")
    .replace(/(\*\*|__)(.+?)\1/g, "$2")
    .replace(/\*(\S(?:.*?\S)?)\*/g, "$1")
    .replace(/(^|[^\w])_(\S(?:.*?\S)?)_(?=[^\w]|$)/g, "$1$2")
    .replace(/`([^`]*)`/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
  return stripped.length > maxChars ? stripped.slice(0, maxChars) + "…" : stripped;
}

export interface ParsedDate {
  day: string;
  /** 0-11 — format it with formatMonthShort / formatMonthLabel (UI language). */
  monthIndex: number;
  year: string;
  /** Locale-neutral group key, e.g. "2026-10". */
  monthKey: string;
}

function makeParsedDate(year: number, monthIndex: number, day: number): ParsedDate {
  return {
    day: String(day),
    monthIndex,
    year: String(year),
    monthKey: `${year}-${String(monthIndex + 1).padStart(2, "0")}`,
  };
}

function parsedDateFromDate(d: Date): ParsedDate {
  return makeParsedDate(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate());
}

function utcMonthDate(d: ParsedDate): Date {
  return new Date(Date.UTC(parseInt(d.year, 10), d.monthIndex, 1, 12));
}

/** Short month in the UI language ("Oct" / "Okt."), for the row margin. */
export function formatMonthShort(d: ParsedDate, locale: string): string {
  return new Intl.DateTimeFormat(locale, { month: "short", timeZone: "UTC" }).format(utcMonthDate(d));
}

/** Month group label in the UI language ("Oct 2026" / "Okt. 2026"). */
export function formatMonthLabel(d: ParsedDate, locale: string): string {
  return new Intl.DateTimeFormat(locale, { month: "short", year: "numeric", timeZone: "UTC" }).format(
    utcMonthDate(d),
  );
}

/** Long date of a parsed (day-precision) date in the UI language. */
export function formatParsedLongDate(d: ParsedDate, locale: string): string {
  const date = new Date(Date.UTC(parseInt(d.year, 10), d.monthIndex, parseInt(d.day, 10), 12));
  return new Intl.DateTimeFormat(locale, {
    day: "numeric",
    month: "long",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}

/**
 * Parse the YYYY-MM-DD prefix from a vault path stem.
 * Returns null when no date prefix is present.
 */
export function parseDateFromPath(path: string): ParsedDate | null {
  const stem = path.split("/").pop() ?? "";
  const match = stem.match(/^(\d{4})-(\d{2})-(\d{2})/);
  if (!match) return null;
  const [, year, monthRaw, dayRaw] = match;
  const monthIdx = parseInt(monthRaw, 10) - 1;
  if (monthIdx < 0 || monthIdx > 11) return null;
  return makeParsedDate(parseInt(year, 10), monthIdx, parseInt(dayRaw, 10));
}

/**
 * Resolve a renderable date for a note, in priority order:
 *   1. note.date          (backend's denormalised frontmatter.date column)
 *   2. frontmatter.date   (in case the backend hasn't been redeployed yet)
 *   3. path stem date     (legacy / no-frontmatter notes)
 *   4. null               (truly undated)
 */
export function parseDateFromNote(note: {
  path: string;
  content: string;
  date?: string;
}): ParsedDate | null {
  if (note.date && note.date.trim()) {
    const d = new Date(note.date);
    if (!isNaN(d.getTime())) return parsedDateFromDate(d);
  }
  const fm = parseFrontmatter(note.content);
  if (fm.date) {
    const d = new Date(fm.date);
    if (!isNaN(d.getTime())) return parsedDateFromDate(d);
  }
  if (fm.created_at) {
    const d = new Date(fm.created_at);
    if (!isNaN(d.getTime())) return parsedDateFromDate(d);
  }
  return parseDateFromPath(note.path);
}

/** Long-form date for the reading panel in the UI language
 *  ("October 23, 2026" / "23. Oktober 2026"). Unparseable input is shown as is. */
export function formatLongDate(input: string, locale: string): string {
  const d = new Date(input);
  if (isNaN(d.getTime())) return input;
  try {
    return d.toLocaleDateString(locale, { day: "numeric", month: "long", year: "numeric" });
  } catch {
    return input;
  }
}

// ── Month marker ───────────────────────────────────────────────────────────────
//
// Replaces the legacy DateDivider. Slim, no full-row bar — a sigil + caps label
// followed by a fading hairline. Sticky so it stays as you scroll within a month.

export function MonthMarker({ label }: { label: string }) {
  return (
    <div
      className="sticky top-0 z-10 flex items-baseline gap-3 px-4 py-3"
      style={{
        // Translucent + blur so the list bg shines through. Solid
        // var(--color-bg-base) created a hard dark stripe that sat
        // visually outside the surrounding layout — the operator called it out.
        background: "var(--surface-sticky)",
        backdropFilter: "blur(10px)",
        WebkitBackdropFilter: "blur(10px)",
      }}
    >
      <span
        className="font-mono"
        style={{ fontSize: "10px", color: "var(--color-text-dim)" }}
      >
        ◆
      </span>
      <span
        className="font-mono uppercase font-semibold"
        style={{
          fontSize: "10px",
          letterSpacing: "0.18em",
          color: "var(--color-text-secondary)",
        }}
      >
        {label}
      </span>
      <div
        className="flex-1 h-px"
        style={{
          background:
            `linear-gradient(to right, var(--color-bg-hover), ${alpha(C.overlay, 0.02)} 50%, transparent)`,
        }}
      />
    </div>
  );
}

/** Back-compat shim — old import name still works. */
export const DateDivider = MonthMarker;

// ── Note row ───────────────────────────────────────────────────────────────────

interface VaultNoteRowProps {
  note: VaultNote;
  selected: boolean;
  onSelect: (note: VaultNote) => void;
}

export function VaultNoteRow({ note, selected, onSelect }: VaultNoteRowProps) {
  const t = useTranslations("vault");
  const locale = useLocale();
  const rawTitle = useMemo(() => titleFromNote(note), [note]);
  // titleFromNote returns the sentinel "Untitled" for UUID-only stems —
  // translate it here (the helper stays a pure module function).
  const title = rawTitle === "Untitled" ? t("untitled") : rawTitle;
  const excerpt = useMemo(() => excerptFromContent(note.content), [note.content]);
  const tags = useMemo(() => parseTags(note.tags), [note.tags]);
  const agentColor = colorForAgent(note.agent);
  const date = useMemo(() => parseDateFromNote(note), [note]);

  // Marginalia day colour: agent-tinted when selected, neutral otherwise.
  // We expose --agent-color so the hover Tailwind utility picks it up.
  const rowStyle = {
    "--agent-color": agentColor,
    background: selected ? alpha(agentColor, 0.05) : "transparent",
    transition: "background 0.2s ease",
  } as React.CSSProperties;

  return (
    <motion.button
      whileHover={{ x: 3 }}
      transition={{ type: "spring", stiffness: 380, damping: 30 }}
      onClick={() => onSelect(note)}
      className="w-full text-left cursor-pointer group flex relative"
      style={rowStyle}
    >
      {/* ── Marginalia (date) ───────────────────────────────────────────────── */}
      <div className="shrink-0 w-[92px] py-5 pl-3 pr-4 text-right select-none">
        {date ? (
          <>
            <div
              className="text-[30px] font-bold leading-none tracking-tighter tabular-nums transition-colors duration-200 group-hover:text-[var(--agent-color)]"
              style={{
                color: selected
                  ? agentColor
                  : "var(--color-text-primary)",
                fontFeatureSettings: "'ss01' on, 'tnum' on",
              }}
            >
              {date.day}
            </div>
            <div
              className="font-mono uppercase font-semibold mt-1.5 transition-colors duration-200 group-hover:text-[var(--agent-color)]"
              style={{
                fontSize: "10px",
                letterSpacing: "0.18em",
                color: selected ? agentColor : "var(--color-text-muted)",
                opacity: selected ? 1 : 0.9,
              }}
            >
              {formatMonthShort(date, locale)}
            </div>
            <div
              className="font-mono tabular-nums mt-1"
              style={{
                fontSize: "9.5px",
                letterSpacing: "0.06em",
                color: "var(--color-text-dim)",
              }}
            >
              {date.year}
            </div>
          </>
        ) : (
          <div
            className="font-mono mt-2 group-hover:text-[var(--agent-color)] transition-colors duration-200"
            style={{ fontSize: "22px", color: "var(--color-text-dim)" }}
          >
            ◌
          </div>
        )}
      </div>

      {/* ── Vertical column rule ────────────────────────────────────────────── */}
      <div
        className="shrink-0 self-stretch transition-all duration-200 group-hover:w-[2px]"
        aria-hidden
        style={{
          width: selected ? "2px" : "1px",
          background: selected
            ? agentColor
            : `linear-gradient(to bottom, transparent 0%, ${alpha(C.overlay, 0.1)} 18%, ${alpha(C.overlay, 0.1)} 82%, transparent 100%)`,
        }}
      />

      {/* ── Content ─────────────────────────────────────────────────────────── */}
      <div className="flex-1 min-w-0 py-5 px-5">
        {/* Type chip (agent-tinted) + agent slug */}
        <div className="flex items-center gap-2.5 mb-2">
          <span
            className="font-mono uppercase font-semibold rounded-sm"
            style={{
              fontSize: "9.5px",
              letterSpacing: "0.14em",
              padding: "3px 7px 3px 7px",
              background: alpha(agentColor, 0.1),
              color: agentColor,
              border: `1px solid ${alpha(agentColor, 0.22)}`,
              lineHeight: 1,
            }}
          >
            {noteTypeLabel(t, note.type)}
          </span>
          {note.agent && (
            <span
              className="font-mono lowercase"
              style={{
                fontSize: "10px",
                letterSpacing: "0.04em",
                color: "var(--color-text-muted)",
              }}
            >
              {note.agent}
            </span>
          )}
          {note.project && note.project.length > 0 && (
            <>
              <span
                className="font-mono"
                style={{ fontSize: "10px", color: "var(--color-text-dim)" }}
              >
                ·
              </span>
              <span
                className="font-mono"
                style={{
                  fontSize: "10px",
                  color: "var(--color-text-muted)",
                }}
              >
                {note.project}
              </span>
            </>
          )}
        </div>

        {/* Title — sans bold, tight tracking */}
        <div
          className="font-semibold leading-snug mb-1.5"
          style={{
            fontSize: "16.5px",
            letterSpacing: "-0.005em",
            color: "var(--color-text-primary)",
          }}
        >
          {title}
        </div>

        {/* Excerpt */}
        {excerpt && (
          <div
            className="line-clamp-2"
            style={{
              fontSize: "13.5px",
              lineHeight: 1.55,
              color: "var(--color-text-secondary)",
            }}
          >
            {excerpt}
          </div>
        )}

        {/* Tags — flat mono row, no chips (chip noise was part of the slop) */}
        {tags.length > 0 && (
          <div className="flex flex-wrap items-center gap-x-3 gap-y-1 mt-3">
            {tags.slice(0, 5).map((tag) => (
              <span
                key={tag}
                className="font-mono"
                style={{
                  fontSize: "10.5px",
                  color: "var(--color-text-muted)",
                }}
              >
                #{tag}
              </span>
            ))}
            {tags.length > 5 && (
              <span
                className="font-mono"
                style={{
                  fontSize: "10px",
                  color: "var(--color-text-dim)",
                }}
              >
                +{tags.length - 5}
              </span>
            )}
          </div>
        )}
      </div>
    </motion.button>
  );
}
