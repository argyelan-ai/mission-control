"use client";

import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { motion, AnimatePresence } from "framer-motion";
import { useVaultNotePreview } from "@/hooks/useVaultNote";
import { C } from "@/lib/colors";

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
  onWikilinkClick,
}: {
  target: string;
  onWikilinkClick?: (target: string) => void;
}) {
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
        aria-label={`Navigate to ${target}`}
      >
        [<span>{target}</span>]
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
                boxShadow: "0 8px 24px rgba(0,0,0,0.5)",
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
export function VaultMarkdown({ content, onWikilinkClick }: VaultMarkdownProps) {
  // Pre-process: replace [[name]] with a token react-markdown can handle.
  // We inject a fake markdown link [name](wikilink:name) which we intercept
  // in the `a` component renderer.
  const processed = content.replace(
    /\[\[([^\]]+)\]\]/g,
    (_, name: string) => `[${name}](wikilink:${encodeURIComponent(name)})`
  );

  return (
    <ReactMarkdown
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
            <pre className="p-4 rounded-md font-mono text-sm overflow-x-auto my-4" style={{ background: "rgba(255,255,255,0.04)" }}>
              <code style={{ color: "var(--color-text-body)" }}>{children}</code>
            </pre>
          ) : (
            <code
              className="px-1.5 py-0.5 rounded font-mono text-[0.9em]"
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
          <hr className="my-8" style={{ borderColor: "rgba(255,255,255,0.05)" }} />
        ),
        a: ({ href, children }) => {
          // Detect wikilink tokens
          if (href?.startsWith("wikilink:")) {
            const target = decodeURIComponent(href.replace("wikilink:", ""));
            return (
              <WikilinkChip target={target} onWikilinkClick={onWikilinkClick} />
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
