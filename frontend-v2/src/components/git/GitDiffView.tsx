"use client";

import { useState } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronDown } from "lucide-react";
import type { CommitDiff, CommitDiffFile } from "@/lib/types";
import { C, BRAND } from "@/lib/colors";

// ── File type detection ──────────────────────────────────────────────────────

const EXT_LANG: Record<string, string> = {
  ts: "TS", tsx: "TSX", js: "JS", jsx: "JSX",
  py: "PY", rs: "RS", go: "GO", java: "JAVA",
  css: "CSS", scss: "SCSS", html: "HTML",
  json: "JSON", yaml: "YAML", yml: "YAML",
  md: "MD", sh: "SH", sql: "SQL", env: "ENV",
  toml: "TOML", lock: "LOCK",
};

// Language badge colors are external tool-brand colors — they stay as-is
// (EXT_COLOR is a per-language identity palette, not an MC status color).
const EXT_COLOR: Record<string, string> = {
  ts: BRAND.typescript,
  tsx: BRAND.react,
  js: BRAND.javascript,
  jsx: BRAND.react,
  py: BRAND.python,
  rs: BRAND.rust,
  go: BRAND.golang,
  java: BRAND.java,
  css: BRAND.css,
  scss: BRAND.scss,
  html: BRAND.html,
  json: BRAND.json,
  yaml: BRAND.yaml,
  yml: BRAND.yaml,
  md: BRAND.markdown,
  sh: BRAND.shell,
  sql: BRAND.sql,
  env: BRAND.env,
};

function getFileExt(filename: string) {
  return filename.split(".").pop()?.toLowerCase() ?? "";
}

function FileStatusBadge({ status }: { status: string }) {
  const cfg = {
    added:    { label: "A", color: C.online,   bg: `${C.online}1A` },
    deleted:  { label: "D", color: C.error,    bg: `${C.error}1A` },
    renamed:  { label: "R", color: C.warning,  bg: `${C.warning}1A` },
    modified: { label: "M", color: C.accent,   bg: `${C.accent}1A` },
  }[status] ?? { label: "M", color: C.accent, bg: `${C.accent}1A` };

  return (
    <span
      className="text-[9px] font-bold rounded px-1 py-0.5 shrink-0 leading-none"
      style={{ color: cfg.color, background: cfg.bg, fontFamily: "var(--font-geist-mono), monospace" }}
    >
      {cfg.label}
    </span>
  );
}

function LangBadge({ ext }: { ext: string }) {
  const label = EXT_LANG[ext];
  const color = EXT_COLOR[ext] ?? C.textMuted;
  if (!label) return null;
  return (
    <span
      className="text-[9px] font-semibold px-1 py-0.5 rounded shrink-0 leading-none"
      style={{ color, background: `${color}18`, border: `1px solid ${color}30`, fontFamily: "var(--font-geist-mono), monospace" }}
    >
      {label}
    </span>
  );
}

// ── Single file diff ─────────────────────────────────────────────────────────

const MAX_LINES = 500;

function FileDiff({ file, defaultOpen = true }: { file: CommitDiffFile; defaultOpen?: boolean }) {
  const [open, setOpen] = useState(defaultOpen);
  const ext = getFileExt(file.filename);
  const allLines = file.hunks.flatMap((h) => h.lines);
  const truncated = allLines.length > MAX_LINES;

  // Detect status from hunks
  const hasAdded = allLines.some((l) => l.type === "add");
  const hasDel = allLines.some((l) => l.type === "del");
  const status = !hasDel ? "added" : !hasAdded ? "deleted" : "modified";

  // Extract just filename for display
  const parts = file.filename.split("/");
  const fname = parts.pop() ?? file.filename;
  const fdir = parts.join("/");

  return (
    <div style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
      {/* File header */}
      <button
        onClick={() => setOpen((x) => !x)}
        className="w-full flex items-center gap-2 px-3 py-2 text-left transition-all duration-150"
        style={{
          background: open ? `${C.accent}0A` : `${C.borderSubtle}`,
          border: "none",
          borderLeft: `2px solid ${open ? `${C.accent}80` : C.border}`,
          cursor: "pointer",
        }}
        onMouseEnter={(e) => { (e.currentTarget as HTMLElement).style.background = `${C.accent}0F`; }}
        onMouseLeave={(e) => { (e.currentTarget as HTMLElement).style.background = open ? `${C.accent}0A` : C.borderSubtle; }}
      >
        {/* Chevron */}
        <motion.span
          animate={{ rotate: open ? 0 : -90 }}
          transition={{ duration: 0.15 }}
          className="shrink-0"
          style={{ color: C.textMuted }}
        >
          <ChevronDown size={11} />
        </motion.span>

        {/* Status badge */}
        <FileStatusBadge status={status} />

        {/* File path */}
        <span className="flex-1 min-w-0 flex items-baseline gap-1.5 font-mono text-[11px]">
          {fdir && (
            <span style={{ color: C.textMuted }}>{fdir}/</span>
          )}
          <span style={{ color: C.textPrimary, fontWeight: 500 }}>{fname}</span>
        </span>

        {/* Lang badge */}
        <LangBadge ext={ext} />

        {/* +/- stats */}
        <span className="shrink-0 flex items-center gap-1.5 ml-1">
          {file.additions > 0 && (
            <span
              className="text-[10px] font-mono font-medium"
              style={{ color: C.online }}
            >
              +{file.additions}
            </span>
          )}
          {file.deletions > 0 && (
            <span
              className="text-[10px] font-mono font-medium"
              style={{ color: C.error }}
            >
              -{file.deletions}
            </span>
          )}
        </span>

        {/* Mini diff bar */}
        <DiffBar additions={file.additions} deletions={file.deletions} />
      </button>

      {/* Diff content */}
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0 }}
            animate={{ height: "auto" }}
            exit={{ height: 0 }}
            transition={{ duration: 0.18, ease: "easeOut" }}
            style={{ overflow: "hidden" }}
          >
            <div style={{ overflowX: "auto", maxHeight: 480, overflowY: "auto" }}>
              <table
                className="font-mono text-[11px]"
                style={{ borderCollapse: "collapse", width: "100%", minWidth: "max-content" }}
              >
                <tbody>
                  {file.hunks.map((hunk, hi) => {
                    const lines = truncated && hi === 0 ? hunk.lines.slice(0, MAX_LINES) : hunk.lines;
                    return (
                      <>
                        {/* Hunk header — uses info tone for structure, not lila */}
                        <tr key={`hunk-${hi}`}>
                          <td
                            colSpan={4}
                            className="text-[10px] select-none"
                            style={{
                              background: `${C.info}12`,
                              color: C.info,
                              padding: "2px 12px",
                              borderTop: `1px solid ${C.info}1F`,
                              borderBottom: `1px solid ${C.info}14`,
                              fontFamily: "var(--font-geist-mono), monospace",
                            }}
                          >
                            {hunk.header}
                          </td>
                        </tr>

                        {/* Diff lines */}
                        {lines.map((line, li) => {
                          const isAdd = line.type === "add";
                          const isDel = line.type === "del";
                          return (
                            <tr
                              key={`${hi}-${li}`}
                              style={{
                                background: isAdd
                                  ? `${C.online}0F`
                                  : isDel
                                  ? `${C.error}0F`
                                  : "transparent",
                              }}
                            >
                              {/* Old line number */}
                              <td
                                className="select-none text-right"
                                style={{
                                  padding: "0 6px",
                                  color: isDel ? `${C.error}59` : C.bgHover,
                                  minWidth: 34,
                                  fontSize: 10,
                                  verticalAlign: "top",
                                  paddingTop: 1,
                                  paddingBottom: 1,
                                  borderRight: `1px solid ${C.borderSubtle}`,
                                }}
                              >
                                {line.old_no ?? ""}
                              </td>

                              {/* New line number */}
                              <td
                                className="select-none text-right"
                                style={{
                                  padding: "0 6px",
                                  color: isAdd ? `${C.online}59` : C.bgHover,
                                  minWidth: 34,
                                  fontSize: 10,
                                  verticalAlign: "top",
                                  paddingTop: 1,
                                  paddingBottom: 1,
                                  borderRight: `1px solid ${C.border}`,
                                }}
                              >
                                {line.new_no ?? ""}
                              </td>

                              {/* Glyph */}
                              <td
                                className="select-none text-center"
                                style={{
                                  padding: "1px 6px",
                                  color: isAdd ? C.online : isDel ? C.error : C.bgHover,
                                  fontSize: 11,
                                  fontWeight: 700,
                                  width: 16,
                                  verticalAlign: "top",
                                }}
                              >
                                {isAdd ? "+" : isDel ? "−" : " "}
                              </td>

                              {/* Content — light tint for add/del lines */}
                              <td
                                style={{
                                  padding: "1px 16px 1px 2px",
                                  color: isAdd ? C.textSecondary : isDel ? C.textMuted : C.textDim,
                                  whiteSpace: "pre",
                                  verticalAlign: "top",
                                }}
                              >
                                {line.content}
                              </td>
                            </tr>
                          );
                        })}
                      </>
                    );
                  })}

                  {truncated && (
                    <tr>
                      <td
                        colSpan={4}
                        className="text-[10px] text-center"
                        style={{ padding: "6px 12px", color: C.textMuted, background: C.borderSubtle }}
                      >
                        … {allLines.length - MAX_LINES} weitere Zeilen
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Mini diff bar (like GitHub's colored blocks) ─────────────────────────────

function DiffBar({ additions, deletions }: { additions: number; deletions: number }) {
  const total = additions + deletions;
  if (total === 0) return null;
  const maxBlocks = 5;
  const addBlocks = Math.round((additions / total) * maxBlocks);
  const delBlocks = maxBlocks - addBlocks;
  return (
    <span className="flex items-center gap-0.5 shrink-0">
      {Array.from({ length: addBlocks }).map((_, i) => (
        <span key={`a${i}`} className="block w-2 h-2 rounded-sm" style={{ background: `${C.online}B3` }} />
      ))}
      {Array.from({ length: delBlocks }).map((_, i) => (
        <span key={`d${i}`} className="block w-2 h-2 rounded-sm" style={{ background: `${C.error}B3` }} />
      ))}
    </span>
  );
}

// ── Main export ──────────────────────────────────────────────────────────────

export function GitDiffView({ diff }: { diff: CommitDiff }) {
  return (
    <div className="text-xs" style={{ borderTop: `1px solid ${C.borderSubtle}` }}>
      {/* Stats header */}
      <div
        className="flex items-center gap-3 px-4 py-2 text-[10px] font-mono"
        style={{
          background: `${C.accent}08`,
          borderBottom: `1px solid ${C.borderSubtle}`,
          color: C.textMuted,
        }}
      >
        <span className="flex items-center gap-2">
          <span style={{ color: C.textPrimary, fontWeight: 500 }}>{diff.message}</span>
        </span>
        <span className="flex items-center gap-3 ml-auto shrink-0">
          <span style={{ color: C.textDim }}>{diff.stats.files} {diff.stats.files === 1 ? "file" : "files"}</span>
          <span style={{ color: C.online, fontWeight: 600 }}>+{diff.stats.additions}</span>
          <span style={{ color: C.error, fontWeight: 600 }}>-{diff.stats.deletions}</span>
        </span>
      </div>

      {/* Files */}
      {diff.files.map((file) => (
        <FileDiff key={file.filename} file={file} defaultOpen={diff.files.length <= 3} />
      ))}
    </div>
  );
}
