"use client";

/**
 * Rendered preview of an HTML file (a landing page an agent built, a bench
 * result) — the page itself instead of its source code.
 *
 * Security boundary — the page is untrusted, model-written content:
 *
 *  - Click to run. The page first shows as a still: the same document in an
 *    `<iframe sandbox="">`, every restriction on, scripts included, under a
 *    "Run page" button. Only that tap mounts the frame that may run scripts.
 *    A sandboxed srcdoc frame runs in MC's own process (on iOS always), so a
 *    `for(;;){}` in the page freezes all of MC, and in WebKit the page's
 *    `history.back()` moves the whole tab (see Known limits). Neither may
 *    happen just because the operator opened a deliverable. The tap counts
 *    for this page only: other content, or leaving the preview for the code
 *    view, starts from the still again. A reload brings MC back from a page
 *    that hangs, and the page comes back as a still.
 *  - The running page lives in an `<iframe sandbox="allow-scripts">` and
 *    NOTHING else. Without `allow-same-origin` it lives in an opaque origin:
 *    its scripts run, but they cannot read MC's localStorage (the session
 *    token), cookies or DOM. Without `allow-top-navigation*`,
 *    `allow-popups*`, `allow-forms` and `allow-modals` it cannot navigate MC
 *    to a new URL, open windows, submit forms or block the app with alert().
 *  - The content goes in as `srcdoc`, never as a URL. A `blob:` URL would
 *    carry MC's origin and runs un-sandboxed the moment someone opens it in
 *    its own tab ("open frame in new tab"); `about:srcdoc` cannot be opened
 *    that way.
 *  - MC draws a label above the frame and in the fullscreen header: "Agent
 *    page · isolated preview – never enter passwords here". The page can draw
 *    anything inside its frame, a fake MC login included, but it cannot
 *    paint over MC's own chrome.
 *  - `buildPreviewDocument` puts a guard head in front of the page:
 *      · `<base href="about:srcdoc">` — a srcdoc page otherwise resolves
 *        relative URLs against MC's own URL: an in-page link (`#pricing`)
 *        would load MC itself into the frame, and `logo.png` would be fetched
 *        from MC. With this base, `#links` stay in the page and relative
 *        files resolve to nothing (multi-file pages are a separate feature).
 *      · `no-referrer` — requests from the page carry no Referer header.
 *      · a CSP: inline code and https: libraries/fonts/images load (many
 *        landing pages use a CDN), the page's own fetch/XHR/WebSocket
 *        (`connect-src`) and forms are off, and plain http: (LAN, MC itself)
 *        is never a source. This is defence in depth, not the boundary — the
 *        sandbox is.
 *
 * Known limits — what the sandbox does NOT take away (checked in Chromium
 * and WebKit, the Safari engine; not on a real iPhone):
 *  - Once started, a page that never yields (an endless loop) freezes MC's
 *    tab until it is reloaded — the frame shares MC's process.
 *  - In WebKit a running page can step the TAB's history: `history.back()` or
 *    `history.go(-n)` leaves MC for the page before it, or steps back through
 *    MC's own routes. It can only reach existing history entries, never a URL
 *    of its choice, and no sandbox flag stops it. Chromium blocks it.
 *  - The page shares the tab's history: an in-page `#link` or
 *    `history.pushState` adds entries, so Back first steps through them.
 *    They stay even after the frame is removed (30 pushes → +30 entries,
 *    in both engines), so remounting would not help; browsers throttle a
 *    page that floods the history.
 *  - The page can learn MC's ADDRESS (not its data): its script can read
 *    `location.ancestorOrigins`, which lists MC's origin whatever the
 *    referrer policy says.
 *  - Data the page has can leave: through an allowed https: image, script or
 *    frame URL, or by navigating its own frame. `connect-src 'none'` only
 *    stops the page's own fetch/XHR/WebSocket. Hence the label: what the
 *    operator types into the page can be sent out.
 *  - In WebKit the page's requests to MC carry MC's session cookie. The
 *    backend does not take that cookie as a login for cross-site
 *    subrequests (`_cookie_allowed` in backend/app/auth.py), and http: is no
 *    source here, so the page cannot load MC's API as the operator.
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
import { Code2, Eye, Loader2, Maximize2, Play, ShieldAlert, X, type LucideIcon } from "lucide-react";
import { C, alpha } from "@/lib/colors";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";

/** The ONLY sandbox token the running frame gets. Tests pin it exactly. */
export const HTML_PREVIEW_SANDBOX = "allow-scripts";

/** The still before the tap: an empty sandbox, every restriction on — no
 *  script runs, so nothing in the page can loop or move the tab. */
export const HTML_STILL_SANDBOX = "";

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

/**
 * The page with the guard head in FRONT of everything it brings (doctype,
 * comments, a script before <head>), so nothing of the page runs before it.
 *
 * Nothing is scanned: a srcdoc document is never in quirks mode (HTML spec,
 * "initial" insertion mode — the quirks rules skip iframe srcdoc documents),
 * so the page's doctype need not come first. Checked in Chromium and WebKit:
 * with the guard before an HTML 4.01 Transitional doctype the page stays in
 * standards mode (CSS1Compat), the CSP meta sits in <head> and is enforced,
 * and the page's own `<html lang>`, `<title>` and styles still apply.
 */
export function buildPreviewDocument(html: string): string {
  return GUARD_HEAD + html;
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

// ── Still (before the tap) ─────────────────────────────────────────────────

/**
 * The page as a still: the same document with scripts off, dimmed, under the
 * "Run page" button. The cover takes every touch, so the still is a picture —
 * no link in it can be followed — and a swipe over it scrolls MC, not the
 * page. Its own element: sandbox flags apply when a frame navigates, so the
 * running page always gets a NEW frame, never this one with a new attribute.
 */
function StillFrame({ doc, title, onRun }: { doc: string; title: string; onRun: () => void }) {
  const t = useTranslations("filePreview.html");
  return (
    <>
      <iframe
        title={title}
        sandbox={HTML_STILL_SANDBOX}
        srcDoc={doc}
        referrerPolicy="no-referrer"
        tabIndex={-1}
        className="absolute inset-0 block w-full h-full border-0"
        style={{ colorScheme: "light", background: "Canvas" }}
      />
      <div
        className="absolute inset-0 flex items-center justify-center p-4"
        style={{ background: alpha(C.scrim, 0.45) }}
      >
        <div
          className="flex flex-col items-center gap-2 p-4 rounded-lg text-center"
          style={{ background: C.bgElevated, border: `1px solid ${C.border}` }}
        >
          <button
            type="button"
            onClick={onRun}
            className="inline-flex items-center justify-center gap-2 min-h-11 px-4 rounded-md text-sm font-medium cursor-pointer transition-colors"
            style={{ background: C.accent, color: C.onAccent }}
            onMouseEnter={(e) => (e.currentTarget.style.background = C.accentHover)}
            onMouseLeave={(e) => (e.currentTarget.style.background = C.accent)}
          >
            <Play size={16} aria-hidden="true" />
            {t("run")}
          </button>
          <p className="text-xs" style={{ color: C.textSecondary }}>
            {t("stillHint")}
          </p>
        </div>
      </div>
    </>
  );
}

/** The still before the tap, the running page after it. */
function PageFrame({ doc, name, running, onRun }: { doc: string; name: string; running: boolean; onRun: () => void }) {
  const t = useTranslations("filePreview.html");
  return running ? (
    <SandboxedFrame doc={doc} title={t("frameTitle", { name })} />
  ) : (
    <StillFrame doc={doc} title={t("stillTitle", { name })} onRun={onRun} />
  );
}

/**
 * "Agent page · isolated preview – never enter passwords here". MC's own
 * chrome, outside the frame: the page can fake a login form inside its
 * frame, but it cannot paint over this line.
 */
function IsolationLabel() {
  const t = useTranslations("filePreview.html");
  return (
    <p className="flex items-start gap-2 text-xs" style={{ color: C.textSecondary }}>
      <ShieldAlert size={14} className="shrink-0 my-px" style={{ color: C.warning }} aria-hidden="true" />
      <span>{t("isolationLabel")}</span>
    </p>
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

function FullscreenView({
  doc,
  name,
  running,
  onRun,
  onClose,
}: {
  doc: string;
  name: string;
  running: boolean;
  onRun: () => void;
  onClose: () => void;
}) {
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
      {/* MC's chrome: the label and which file this is. The page below can
          fill the rest of the screen, never this bar. */}
      <div
        className="flex items-center gap-2 pl-4 pr-2 pt-safe shrink-0"
        style={{ borderBottom: `1px solid ${C.border}` }}
      >
        <div className="flex-1 min-w-0 flex flex-col gap-1 py-2">
          <IsolationLabel />
          <span className="truncate font-mono text-xs" style={{ color: C.textMuted }}>
            {name}
          </span>
        </div>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label={t("closeFullscreen")}
          title={t("closeFullscreen")}
          className="inline-flex items-center justify-center shrink-0 min-h-11 min-w-11 rounded-md cursor-pointer transition-colors"
          style={{ color: C.textSecondary }}
          onMouseEnter={(e) => (e.currentTarget.style.color = C.textPrimary)}
          onMouseLeave={(e) => (e.currentTarget.style.color = C.textSecondary)}
        >
          <X size={20} aria-hidden="true" />
        </button>
      </div>
      <div className="relative flex-1 min-h-0">
        <PageFrame doc={doc} name={name} running={running} onRun={onRun} />
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

  // Click to run: the document the operator started, or null. Bound to the
  // document itself, so other content — the same component handed another
  // file — arrives as a still and never inherits the tap.
  const [runningDoc, setRunningDoc] = useState<string | null>(null);
  const running = runningDoc === doc;
  const run = () => setRunningDoc(doc);

  function changeView(next: View) {
    setView(next);
    // The code view unmounts the page; back on the preview it is a still
    // again and runs only after the next tap.
    if (next !== "preview") setRunningDoc(null);
  }

  return (
    <div data-region="html-preview">
      <div className="flex items-center justify-between gap-2 mb-2">
        <ViewSwitch view={view} onChange={changeView} />
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
        <div className="rounded-dense overflow-hidden" style={{ border: `1px solid ${C.border}` }}>
          <div className="px-3 py-2" style={{ background: C.bgSurface, borderBottom: `1px solid ${C.border}` }}>
            <IsolationLabel />
          </div>
          {/* 60 % of the screen on a phone, never so small that a page is a strip. */}
          <div className="relative w-full h-[60vh] min-h-[320px] max-h-[720px]">
            {/* One copy of the page at a time: while the fullscreen view is
                open the inline frame is unmounted, the box keeps its height
                so the page underneath does not jump. */}
            {!fullscreen && <PageFrame doc={doc} name={name} running={running} onRun={run} />}
          </div>
        </div>
      ) : (
        code
      )}

      {fullscreen && (
        <FullscreenView
          doc={doc}
          name={name}
          running={running}
          onRun={run}
          onClose={() => setFullscreen(false)}
        />
      )}
    </div>
  );
}
