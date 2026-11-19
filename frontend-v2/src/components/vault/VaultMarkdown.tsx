"use client";

import { useRef, useState, type ReactNode } from "react";
import Link from "next/link";
import ReactMarkdown, { defaultUrlTransform } from "react-markdown";
import { motion, AnimatePresence } from "framer-motion";
import { useTranslations } from "next-intl";
import { Paperclip } from "lucide-react";
import { useVaultNotePreview } from "@/hooks/useVaultNote";
import { C, alpha } from "@/lib/colors";
import { TASK_WIKILINK_RE } from "./wikilinks";

// Internal link schemes the pre-processing below emits. react-markdown's
// default URL sanitizer blanks every unknown scheme — which turned every
// [[wikilink]] into <a href=""> (a click reopened the same page, round-3
// user test t4 N4). They are let through here; everything else still goes
// through the default sanitizer.
const WIKILINK = "wikilink:";
const EMBED = "vault-embed:";

// encodeURIComponent leaves ( ) alone, and a ")" would end the Markdown
// link early ("Seite (index.html)").
function encodeTarget(target: string): string {
  return encodeURIComponent(target).replace(/\(/g, "%28").replace(/\)/g, "%29");
}

/** Plain text of a link's rendered children (the alias / name). */
function textOf(children: ReactNode): string {
  if (typeof children === "string" || typeof children === "number") return String(children);
  if (Array.isArray(children)) return children.map(textOf).join("");
  return "";
}

function urlTransform(url: string): string {
  if (url.startsWith(WIKILINK) || url.startsWith(EMBED)) return url;
  return defaultUrlTransform(url);
}


/**
 * Drop a leading `# Heading` that only repeats the title the reading panel
 * already shows above the body (deliverable wrappers start with one).
 */
export function stripLeadingTitle(content: string, title: string): string {
  const m = content.match(/^\s*#\s+(.+?)\s*#*\s*(?:\n|$)/);
  if (!m) return content;
  const norm = (v: string) => v.replace(/\s+/g, " ").trim().toLowerCase();
  if (norm(m[1]) !== norm(title)) return content;
  return content.slice(m[0].length).replace(/^\s*\n/, "").trimStart();
}

/** `![[path]]` attachment embed. The file sits behind an authenticated
 *  endpoint, so it is never an <img src>; the reading panel shows the real
 *  preview (AttachmentPreview) and this is only a name tag. */
function EmbedChip({ target }: { target: string }) {
  const t = useTranslations("vault");
  const name = target.split("/").pop() || target;
  return (
    <span
      className="inline-flex items-center gap-2 font-mono text-xs"
      style={{ color: "var(--color-text-muted)" }}
      title={t("attachment")}
    >
      <Paperclip size={12} aria-hidden />
      <span>{name}</span>
    </span>
  );
}

function TaskLink({ taskId }: { taskId: string }) {
  const t = useTranslations("vault");
  return (
    <Link
      href={`/tasks?task=${encodeURIComponent(taskId)}`}
      className="underline font-mono"
      style={{ color: C.accent }}
      aria-label={t("taskLink", { id: taskId.slice(0, 8) })}
      title={t("taskLink", { id: taskId.slice(0, 8) })}
    >
      {taskId.slice(0, 8)}
    </Link>
  );
}

// ── Wikilink chip ─────────────────────────────────────────────────────────────

/**
 * Renders [[name]] as [name] with hover-tooltip showing the first ~100 chars
 * of the linked note. Click navigates via onWikilinkClick.
 *
 * Path resolution: simple stem match via vault.get(name) — this is a
 * best-effort T8 approach. M.4 will introduce proper stem→path resolution
 * via vault.search or a dedicated stem-lookup endpoint.
 */
function WikilinkChip({
  target,
  label,
  onWikilinkClick,
}: {
  target: string;
  /** Shown text — the alias of `[[name|alias]]`, else the target. */
  label: string;
  onWikilinkClick?: (target: string) => void;
}) {
  const t = useTranslations("vault");
  const [hovered, setHovered] = useState(false);
  const [fetchPath, setFetchPath] = useState<string | null>(null);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const { data: preview } = useVaultNotePreview(fetchPath);

  function handleMouseEnter() {
    hoverTimerRef.current = setTimeout(() => {
      // Attempt to resolve stem as a direct path — not always correct but
      // covers simple cases. Stem is the raw [[name]] content.
      setFetchPath(target);
      setHovered(true);
    }, 200);
  }

  function handleMouseLeave() {
    if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    setHovered(false);
  }

  const tooltipText = preview?.content
    ? preview.content.replace(/^---[\s\S]*?---\n?/, "").trim().slice(0, 120)
    : null;

  return (
    <span className="relative inline-block">
      <button
        style={{ color: C.accent }}
        className="underline cursor-pointer font-mono text-[0.9em] bg-transparent border-0 p-0"
        onClick={() => onWikilinkClick?.(target)}
        onMouseEnter={handleMouseEnter}
        onMouseLeave={handleMouseLeave}
        type="button"
        aria-label={t("wikilinkOpen", { target: label })}
      >
        [<span>{label}</span>]
      </button>

      <AnimatePresence>
        {hovered && tooltipText && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.15 }}
            className="absolute bottom-full left-0 mb-2 z-50 pointer-events-none"
            style={{ width: "260px" }}
          >
            <div
              className="rounded-lg px-3 py-2 text-xs leading-relaxed"
              style={{
                background: C.bgHover,
                border: `1px solid ${C.borderAccent}`,
                color: "var(--color-text-secondary)",
                boxShadow: `0 8px 24px ${alpha(C.shadow, 0.5)}`,
              }}
            >
              <div
                className="font-semibold mb-1 uppercase tracking-wider"
                style={{
                  color: C.accent,
                  fontSize: "10px",
                }}
              >
                {target}
              </div>
              <div className="line-clamp-3">{tooltipText}</div>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </span>
  );
}

// ── Markdown component ────────────────────────────────────────────────────────

interface VaultMarkdownProps {
  content: string;
  onWikilinkClick?: (target: string) => void;
  /** Leave out `![[…]]` attachment embeds — set when the panel already shows
   *  the attachment preview below the body. */
  hideEmbeds?: boolean;
}

/**
 * Vault-specific Markdown renderer.
 * - Converts [[name]] wikilinks to WikilinkChip before passing to ReactMarkdown.
 *   Strategy: useMemo regex-replace on content replacing [[name]] with a custom
 *   placeholder <wikilink:name> then detecting it in the link renderer.
 *   Simpler than a remark plugin — documented as T8 limitation.
 * - No `prose-invert` — custom styles on each element.
 * - Accent-Teal (C.accent) only for: inline code text, blockquote border.
 */
export function VaultMarkdown({ content, onWikilinkClick, hideEmbeds = false }: VaultMarkdownProps) {
  // Pre-process: replace [[name]] with a token react-markdown can handle.
  // We inject a fake markdown link [name](wikilink:name) which we intercept
  // in the `a` component renderer. `![[path]]` embeds are handled first —
  // left alone they became `![…](wikilink:…)`, an empty image box.
  // `[[name|alias]]` (Obsidian) links to `name` and shows `alias`; an embed's
  // `|…` part is a display size and is dropped.
  const processed = content
    .replace(/!\[\[([^\]|]+)(?:\|[^\]]*)?\]\]/g, (_, target: string) =>
      hideEmbeds ? "" : `[${target}](${EMBED}${encodeTarget(target)})`
    )
    .replace(
      /\[\[([^\]|]+)(?:\|([^\]]+))?\]\]/g,
      (_, name: string, alias?: string) =>
        `[${alias ?? name}](${WIKILINK}${encodeTarget(name)})`
    );

  return (
    <ReactMarkdown
      urlTransform={urlTransform}
      components={{
        h1: ({ children }) => (
          <h1 className="font-bold text-3xl tracking-tight mt-8 mb-4" style={{ color: "var(--color-text-primary)" }}>
            {children}
          </h1>
        ),
        h2: ({ children }) => (
          <h2 className="font-bold text-2xl tracking-tight mt-6 mb-3" style={{ color: "var(--color-text-primary)" }}>
            {children}
          </h2>
        ),
        h3: ({ children }) => (
          <h3 className="font-semibold text-xl tracking-tight mt-5 mb-2" style={{ color: "var(--color-text-primary)" }}>
            {children}
          </h3>
        ),
        p: ({ children }) => (
          <p className="text-[15px] leading-[1.75] mb-4" style={{ color: "var(--color-text-body)", maxWidth: "68ch" }}>
            {children}
          </p>
        ),
        code: ({ children, className }) => {
          const isBlock = !!className?.includes("language-");
          return isBlock ? (
            <pre className="p-4 rounded-md font-mono text-sm overflow-x-auto my-4" style={{ background: "var(--color-bg-deep)" }}>
              <code style={{ color: "var(--color-text-body)" }}>{children}</code>
            </pre>
          ) : (
            <code
              className="px-1.5 py-0.5 rounded-sm font-mono text-[0.9em]"
              style={{
                background: C.accentSubtle,
                color: C.accent,
              }}
            >
              {children}
            </code>
          );
        },
        blockquote: ({ children }) => (
          <blockquote
            className="pl-4 italic my-4"
            style={{
              borderLeft: `2px solid ${C.borderActive}`,
              color: "var(--color-text-secondary)",
            }}
          >
            {children}
          </blockquote>
        ),
        ul: ({ children }) => (
          <ul className="list-disc list-inside space-y-1 mb-4" style={{ color: "var(--color-text-body)" }}>
            {children}
          </ul>
        ),
        ol: ({ children }) => (
          <ol className="list-decimal list-inside space-y-1 mb-4" style={{ color: "var(--color-text-body)" }}>
            {children}
          </ol>
        ),
        li: ({ children }) => (
          <li className="text-[15px] leading-relaxed">{children}</li>
        ),
        strong: ({ children }) => (
          <strong className="font-semibold" style={{ color: "var(--color-text-primary)" }}>
            {children}
          </strong>
        ),
        em: ({ children }) => (
          <em className="italic" style={{ color: "var(--color-text-secondary)" }}>
            {children}
          </em>
        ),
        hr: () => (
          <hr className="my-8" style={{ borderColor: "var(--color-border-subtle)" }} />
        ),
        a: ({ href, children }) => {
          // Detect wikilink tokens
          if (href?.startsWith(EMBED)) {
            return <EmbedChip target={decodeURIComponent(href.slice(EMBED.length))} />;
          }
          if (href?.startsWith(WIKILINK)) {
            const target = decodeURIComponent(href.slice(WIKILINK.length));
            const task = target.match(TASK_WIKILINK_RE);
            if (task) return <TaskLink taskId={task[1]} />;
            return (
              <WikilinkChip
                target={target}
                label={textOf(children) || target}
                onWikilinkClick={onWikilinkClick}
              />
            );
          }
          return (
            <a
              href={href}
              target="_blank"
              rel="noopener noreferrer"
              style={{ color: C.accent }}
              className="underline"
            >
              {children}
            </a>
          );
        },
      }}
    >
      {processed}
    </ReactMarkdown>
  );
}
