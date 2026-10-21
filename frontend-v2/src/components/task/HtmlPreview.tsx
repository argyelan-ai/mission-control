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
 *      · `no-referrer` — requests from the page carry no Referer header.
 *      · a CSP: inline code and https: libraries/fonts/images load (many
 *        landing pages use a CDN), but fetch/XHR/WebSocket (`connect-src`)
 *        and forms are off, and plain http: (LAN, MC itself) is never a
 *        source. This is defence in depth, not the boundary — the sandbox is.
 *
 * Known limits — what the sandbox does NOT take away (checked in Chromium
 * and WebKit):
 *  - The page can learn MC's ADDRESS (not its data): its script can read
 *    `location.ancestorOrigins`, which lists MC's origin whatever the
 *    referrer policy says, and could send it out through an allowed https:
 *    image or script URL. No token, cookie or page content goes with it.
 *  - The page shares the tab's history: an in-page `#link` or
 *    `history.pushState` adds entries, so Back first steps through them.
 *    They stay even after the frame is removed (30 pushes → +30 entries,
 *    in both engines), so remounting would not help; browsers throttle a
 *    page that floods the history.
 *
 * Shared by every FilePreview caller (task deliverables, folder deliverables,
 * the workspace tab, the Files page), so the boundary is the same everywhere.
 */
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
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

/** HTML whitespace (tab, LF, FF, CR, space) — not JS `\s`, which is wider. */
function isHtmlSpace(code: number): boolean {
  return code === 0x20 || code === 0x09 || code === 0x0a || code === 0x0c || code === 0x0d;
}

/** Index just past the comment whose body starts at `from` (after `<!--`), or -1. */
function commentEnd(html: string, from: number): number {
  // `<!-->` and `<!--->` are complete (empty) comments in the HTML tokenizer.
  if (html.startsWith(">", from)) return from + 1;
  if (html.startsWith("->", from)) return from + 2;
  // Otherwise the first `--` followed by `>` or `!>` ends it (`--->` too:
  // the search simply moves on by one dash).
  for (let k = html.indexOf("--", from); k !== -1; k = html.indexOf("--", k + 1)) {
    if (html.startsWith(">", k + 2)) return k + 3;
    if (html.startsWith("!>", k + 2)) return k + 4;
  }
  return -1;
}

/**
 * Length of what may stand before the guard while keeping the doctype first:
 * a BOM, whitespace, comments, `<?…>` and then the doctype itself — or 0 when
 * there is no such doctype. Follows the HTML tokenizer, so the guard sits in
 * front of the first thing the browser would run.
 *
 * A hand-written scan on purpose: every step moves forward and nothing is
 * read twice, so it is linear on any input. (A regex with a lazy comment
 * inside a repeated group backtracks exponentially over closed comments
 * when no doctype follows — 200 bytes froze the tab for seconds.)
 */
function doctypeEnd(html: string): number {
  const n = html.length;
  let i = html.charCodeAt(0) === 0xfeff ? 1 : 0;
  while (i < n) {
    if (isHtmlSpace(html.charCodeAt(i))) {
      i++;
    } else if (html.startsWith("<!--", i)) {
      i = commentEnd(html, i + 4);
      if (i === -1) return 0; // unclosed: the rest of the file is a comment
    } else if (html.startsWith("<?", i)) {
      const gt = html.indexOf(">", i + 2);
      if (gt === -1) return 0;
      i = gt + 1;
    } else if (html.slice(i, i + 9).toLowerCase() === "<!doctype") {
      const gt = html.indexOf(">", i + 9);
      return gt === -1 ? 0 : gt + 1;
    } else {
      return 0; // content first: a later doctype is ignored by the browser
    }
  }
  return 0;
}

/**
 * The page plus the guard head, placed right after the doctype: before it
 * would throw the page into quirks mode; anywhere later would let markup
 * that comes first (a script before <head>) run unguarded.
 */
export function buildPreviewDocument(html: string): string {
  const end = doctypeEnd(html);
  return html.slice(0, end) + GUARD_HEAD + html.slice(end);
}

// ── Sandboxed frame ────────────────────────────────────────────────────────

/**
 * How long the loading state may cover the frame at most. `load` waits for
 * every subresource — one stalled CDN image or font would otherwise leave an
 * opaque "Loading page…" over a page that rendered long ago.
 */
export const HTML_PREVIEW_LOADING_CAP_MS = 2000;

function SandboxedFrame({ doc, title }: { doc: string; title: string }) {
  const t = useTranslations("filePreview.html");
  const [loaded, setLoaded] = useState(false);
  useEffect(() => {
    setLoaded(false);
    const cap = window.setTimeout(() => setLoaded(true), HTML_PREVIEW_LOADING_CAP_MS);
    return () => window.clearTimeout(cap);
  }, [doc]);

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

/**
 * Esc for the topmost layer only. The fullscreen view opens over dialogs that
 * close on Esc themselves (the task modal, slide-over panels — all through
 * `useEscapeKey`, a window listener in the bubble phase that was bound
 * before this one). A second bubble listener cannot keep them from firing,
 * so this one listens in the CAPTURE phase on window — the first stop of
 * every key event — and stops the event there: one Esc closes one layer.
 *
 * Bound once, calling the latest handler through a ref, like `useEscapeKey`:
 * re-binding per render can lose a real key press mid-dispatch (see there).
 */
function useTopLayerEscape(onEscape: () => void) {
  const handlerRef = useRef(onEscape);
  useLayoutEffect(() => {
    handlerRef.current = onEscape;
  });

  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key !== "Escape") return;
      e.preventDefault();
      e.stopPropagation();
      handlerRef.current();
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []);
}

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
  useTopLayerEscape(onClose);

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
