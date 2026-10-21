"use client";

/**
 * Rendered preview of an HTML file (a landing page an agent built, a bench
 * result) — the page itself instead of its source code.
 *
 * Security boundary — the page is untrusted, model-written content:
 *
 *  - It runs in an `<iframe sandbox="allow-scripts">` and NOTHING else. Without
 *    `allow-same-origin` the page lives in an opaque origin: its scripts run,
 *    but they cannot read MC's localStorage (the session token), cookies or
 *    DOM. Without `allow-top-navigation*`, `allow-popups*`, `allow-forms` and
 *    `allow-modals` it cannot navigate MC away, open windows, submit forms or
 *    block the app with alert().
 *  - The content goes in as `srcdoc`, never as a URL. A `blob:` URL would
 *    carry MC's origin and runs un-sandboxed the moment someone opens it in
 *    its own tab ("open frame in new tab"); `about:srcdoc` cannot be opened
 *    that way.
 *  - `buildPreviewDocument` puts a guard head in front of the page:
 *      · `<base href="about:srcdoc">` — a srcdoc page otherwise resolves
 *        relative URLs against MC's own URL: an in-page link (`#pricing`)
 *        would load MC itself into the frame, and `logo.png` would be fetched
 *        from MC. With this base, `#links` stay in the page and relative
 *        files resolve to nothing (multi-file pages are a separate feature).
 *      · `no-referrer` — CDN requests do not learn MC's address.
 *      · a CSP: inline code and https: libraries/fonts/images load (many
 *        landing pages use a CDN), but fetch/XHR/WebSocket (`connect-src`)
 *        and forms are off, and plain http: (LAN, MC itself) is never a
 *        source. This is defence in depth, not the boundary — the sandbox is.
 *
 * Shared by every FilePreview caller (task deliverables, folder deliverables,
 * the workspace tab, the Files page), so the boundary is the same everywhere.
 */
import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useTranslations } from "next-intl";
import { Code2, Eye, Loader2, Maximize2, X, type LucideIcon } from "lucide-react";
import { C } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

/** The ONLY sandbox token the preview frame gets. Tests pin it exactly. */
export const HTML_PREVIEW_SANDBOX = "allow-scripts";

/** CSP every previewed page runs under (see the file comment). */
export const HTML_PREVIEW_CSP = [
  "default-src 'none'",
  "script-src 'unsafe-inline' 'unsafe-eval' https: data: blob:",
  "style-src 'unsafe-inline' https: data:",
  "img-src https: data: blob:",
  "font-src https: data:",
  "media-src https: data: blob:",
  "frame-src https:",
  "connect-src 'none'",
  "form-action 'none'",
].join("; ");

const GUARD_HEAD =
  '<base href="about:srcdoc">' +
  '<meta name="referrer" content="no-referrer">' +
  `<meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_CSP}">`;

// Everything that may stand before a doctype: BOM, whitespace, comments, an
// xml prologue. One character or one closed construct per step, so the
// pattern stays linear on hostile input.
const PROLOGUE = /^\uFEFF?(?:\s|<!--[\s\S]*?-->|<\?[^>]*>)*<!doctype\b[^>]*>/i;

/**
 * The page plus the guard head, placed right after the doctype: before it
 * would throw the page into quirks mode; anywhere later would let markup
 * that comes first (a script before <head>) run unguarded.
 */
export function buildPreviewDocument(html: string): string {
  const m = PROLOGUE.exec(html);
  if (!m) return GUARD_HEAD + html;
  return m[0] + GUARD_HEAD + html.slice(m[0].length);
}

// ── Sandboxed frame ────────────────────────────────────────────────────────

function SandboxedFrame({ doc, title }: { doc: string; title: string }) {
  const t = useTranslations("filePreview.html");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => setLoaded(false), [doc]);

  return (
    <>
      <iframe
        title={title}
        sandbox={HTML_PREVIEW_SANDBOX}
        srcDoc={doc}
        referrerPolicy="no-referrer"
        onLoad={() => setLoaded(true)}
        className="absolute inset-0 block w-full h-full border-0"
        // The page's own ground, as in a browser tab: a page without a
        // background is white (the system colour Canvas), a page that asks
        // for a dark scheme gets a dark canvas from the browser.
        style={{ colorScheme: "light", background: "Canvas" }}
      />
      {!loaded && (
        <div
          role="status"
          className="absolute inset-0 flex items-center justify-center gap-2 text-xs"
          style={{ background: C.bgDeep, color: C.textMuted }}
        >
          <Loader2 size={16} className="animate-spin" aria-hidden="true" />
          {t("loading")}
        </div>
      )}
    </>
  );
}

// ── Preview / Code switch ──────────────────────────────────────────────────

type View = "preview" | "code";
const VIEWS: { id: View; icon: LucideIcon }[] = [
  { id: "preview", icon: Eye },
  { id: "code", icon: Code2 },
];

function ViewSwitch({ view, onChange }: { view: View; onChange: (v: View) => void }) {
  const t = useTranslations("filePreview.html");
  const refs = useRef<Record<View, HTMLButtonElement | null>>({ preview: null, code: null });

  function onKey(e: KeyboardEvent<HTMLButtonElement>, current: View) {
    const step = e.key === "ArrowRight" || e.key === "ArrowDown" ? 1 : e.key === "ArrowLeft" || e.key === "ArrowUp" ? -1 : 0;
    if (!step) return;
    e.preventDefault();
    const i = VIEWS.findIndex((v) => v.id === current);
    const next = VIEWS[(i + step + VIEWS.length) % VIEWS.length].id;
    onChange(next);
    refs.current[next]?.focus();
  }

  return (
    <div
      role="radiogroup"
      aria-label={t("viewLabel")}
      className="inline-flex rounded-full p-1"
      style={{ border: `1px solid ${C.border}` }}
    >
      {VIEWS.map(({ id, icon: Icon }) => {
        const active = id === view;
        return (
          <button
            key={id}
            ref={(el) => { refs.current[id] = el; }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(id)}
            onKeyDown={(e) => onKey(e, id)}
            className="inline-flex items-center gap-2 min-h-11 md:min-h-8 px-3 rounded-full text-sm cursor-pointer transition-colors"
            style={{
              background: active ? C.bgElevated : "transparent",
              color: active ? C.textPrimary : C.textSecondary,
            }}
          >
            <Icon size={14} aria-hidden="true" />
            {t(id)}
          </button>
        );
      })}
    </div>
  );
}

// ── Fullscreen ─────────────────────────────────────────────────────────────

function FullscreenView({ doc, name, onClose }: { doc: string; name: string; onClose: () => void }) {
  const t = useTranslations("filePreview.html");
  const closeRef = useRef<HTMLButtonElement>(null);
  useBodyScrollLock(true);

  // Focus moves into the dialog and back to the opening button afterwards.
  useEffect(() => {
    const before = document.activeElement as HTMLElement | null;
    closeRef.current?.focus();
    return () => before?.focus?.();
  }, []);

  // Escape closes — while focus is in MC. Inside the page the page owns the
  // keyboard (a sandboxed frame never hands keys up), so the close button
  // stays visible at all times.
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === "Escape") {
        e.preventDefault();
        onClose();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label={t("fullscreenTitle", { name })}
      className="fixed inset-0 z-[200] flex flex-col"
      style={{ background: C.bgDeep }}
    >
      <div
        className="flex items-center gap-2 pl-4 pr-2 pt-safe shrink-0"
        style={{ borderBottom: `1px solid ${C.border}` }}
      >
        <span className="flex-1 min-w-0 truncate font-mono text-xs" style={{ color: C.textSecondary }}>
          {name}
        </span>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label={t("closeFullscreen")}
          title={t("closeFullscreen")}
          className="inline-flex items-center justify-center min-h-11 min-w-11 rounded-md cursor-pointer transition-colors"
          style={{ color: C.textSecondary }}
          onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
          onMouseLeave={(e) => (e.currentTarget.style.color = C.textSecondary)}
        >
          <X size={20} aria-hidden="true" />
        </button>
      </div>
      <div className="relative flex-1 min-h-0">
        <SandboxedFrame doc={doc} title={t("frameTitle", { name })} />
      </div>
    </div>,
    document.body,
  );
}

// ── Main ───────────────────────────────────────────────────────────────────

interface HtmlPreviewProps {
  /** The page source (already fetched with the Bearer header). */
  html: string;
  /** File name, for the frame title and the fullscreen header. */
  name: string;
  /** The source view for the "Code" switch (FilePreview's code renderer). */
  code: ReactNode;
}

export function HtmlPreview({ html, name, code }: HtmlPreviewProps) {
  const t = useTranslations("filePreview.html");
  const [view, setView] = useState<View>("preview");
  const [fullscreen, setFullscreen] = useState(false);
  const doc = useMemo(() => buildPreviewDocument(html), [html]);

  return (
    <div data-region="html-preview">
      <div className="flex items-center justify-between gap-2 mb-2">
        <ViewSwitch view={view} onChange={setView} />
        {view === "preview" && (
          <button
            type="button"
            onClick={() => setFullscreen(true)}
            className="inline-flex items-center gap-2 min-h-11 md:min-h-8 px-3 rounded-md text-sm cursor-pointer transition-colors"
            style={{ color: C.textSecondary }}
            onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
            onMouseLeave={(e) => (e.currentTarget.style.color = C.textSecondary)}
          >
            <Maximize2 size={14} aria-hidden="true" />
            {t("fullscreen")}
          </button>
        )}
      </div>

      {view === "preview" ? (
        // 60 % of the screen on a phone, never so small that a page is a strip.
        <div
          className="relative w-full h-[60vh] min-h-[320px] max-h-[720px] rounded-dense overflow-hidden"
          style={{ border: `1px solid ${C.border}` }}
        >
          {/* One running copy of the page at a time: while the fullscreen
              view is open the inline frame is unmounted, the box keeps its
              height so the page underneath does not jump. */}
          {!fullscreen && <SandboxedFrame doc={doc} title={t("frameTitle", { name })} />}
        </div>
      ) : (
        code
      )}

      {fullscreen && (
        <FullscreenView doc={doc} name={name} onClose={() => setFullscreen(false)} />
      )}
    </div>
  );
}
