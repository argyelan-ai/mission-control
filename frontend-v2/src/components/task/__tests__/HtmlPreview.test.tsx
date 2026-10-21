import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, fireEvent, within, act, waitFor } from "@testing-library/react";
import { FilePreview } from "../FilePreview";
import {
  HTML_PREVIEW_CSP,
  HTML_PREVIEW_LOADING_CAP_MS,
  HTML_PREVIEW_SANDBOX,
  HtmlPreview,
  buildPreviewDocument,
} from "../HtmlPreview";
import { useEscapeKey } from "@/hooks/useEscapeKey";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

// An HTML deliverable (a landing page, a bench result) is shown as the
// rendered page, inside an iframe that is sandboxed to "allow-scripts" ONLY:
// its scripts run, but in an opaque origin — no access to MC's localStorage
// (the session token), cookies, DOM, top-level navigation, popups or forms.
// These tests pin that boundary; adding a single extra sandbox token (e.g.
// allow-same-origin) must turn them red.

const PAGE = `<!DOCTYPE html>
<html lang="en"><head><title>DNA</title></head>
<body><h1>Helix page</h1><a href="#sec">jump</a><script>window.x = 1;</script></body></html>`;

function mockFetch(body: string, status = 200) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { status }));
}

const URL_HTML = "/api/v1/boards/b/tasks/t/deliverables/d/file";

/** Source shown by the code view (the highlighter splits it into spans). */
const shownSource = () => document.querySelector("pre")?.textContent ?? "";

/** Every frame in the document (inline or fullscreen). */
const frames = () => Array.from(document.querySelectorAll("iframe"));
/** Frames whose page can run scripts. A frame WITHOUT a sandbox attribute is
 *  the worst case (no sandbox at all), so it counts as one. */
const scriptFrames = () =>
  frames().filter((f) => {
    const sandbox = f.getAttribute("sandbox");
    return sandbox === null || sandbox.split(/\s+/).includes("allow-scripts");
  });

/** The hint next to the Run button, before the tap. */
const RUN_HINT = "The page runs only when you start it.";

/** Opens an HTML file: it arrives as a Run button, with no frame yet. */
async function renderGate(path = "site/index.html") {
  render(<FilePreview fileUrl={URL_HTML} path={path} />);
  return screen.findByRole("button", { name: "Run page" });
}

/** Opens an HTML file and taps "Run page" — the page itself, running. */
async function renderHtml(path = "site/index.html") {
  fireEvent.click(await renderGate(path));
  return screen.getByTitle(/Rendered page/i) as HTMLIFrameElement;
}

describe("HtmlPreview — rendered HTML deliverables", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn> | undefined;

  beforeEach(() => {
    const storage = {
      getItem: () => "tok",
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    };
    Object.defineProperty(globalThis, "localStorage", {
      value: storage, configurable: true, writable: true,
    });
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
    fetchSpy = undefined;
  });

  it.each(["index.html", "landing.htm", "UPPER.HTML", "dir/sub/page.Html"])(
    "classifies %s as html and shows the rendered page, not source code",
    async (path) => {
      fetchSpy = mockFetch(PAGE);
      const frame = await renderHtml(path);
      expect(frame.tagName).toBe("IFRAME");
      // Default view is the preview: the page source is not printed as code.
      expect(shownSource()).toBe("");
      // The file is fetched with the Bearer header (iframes cannot carry one).
      expect(fetchSpy).toHaveBeenCalledWith(URL_HTML, expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }));
    },
  );

  it("sandboxes the frame to exactly 'allow-scripts' — nothing else", async () => {
    fetchSpy = mockFetch(PAGE);
    const frame = await renderHtml();
    expect(HTML_PREVIEW_SANDBOX).toBe("allow-scripts");
    expect(frame.getAttribute("sandbox")).toBe("allow-scripts");
    const tokens = (frame.getAttribute("sandbox") ?? "").split(/\s+/);
    for (const forbidden of [
      "allow-same-origin",
      "allow-top-navigation",
      "allow-top-navigation-by-user-activation",
      "allow-popups",
      "allow-popups-to-escape-sandbox",
      "allow-forms",
      "allow-modals",
    ]) {
      expect(tokens).not.toContain(forbidden);
    }
    // Content goes in as srcdoc — never a URL the browser could open
    // un-sandboxed (no src, no blob: URL of MC's origin).
    expect(frame.getAttribute("src")).toBeNull();
    expect(frame.getAttribute("srcdoc")).toContain("<h1>Helix page</h1>");
    expect(frame.getAttribute("referrerpolicy")).toBe("no-referrer");
  });

  it("shows a loading state until the page has loaded, then removes it", async () => {
    fetchSpy = mockFetch(PAGE);
    const frame = await renderHtml();
    expect(screen.getByText("Loading page…")).toBeInTheDocument();
    fireEvent.load(frame);
    expect(screen.queryByText("Loading page…")).toBeNull();
  });

  it("shows the error state when the file cannot be fetched — and no frame", async () => {
    fetchSpy = mockFetch("nope", 500);
    render(<FilePreview fileUrl={URL_HTML} path="index.html" />);
    expect(await screen.findByText("Failed to load file")).toBeInTheDocument();
    expect(frames()).toHaveLength(0);
  });

  it("switches between preview and code, preview being the default", async () => {
    fetchSpy = mockFetch(PAGE);
    await renderHtml();
    const group = screen.getByRole("radiogroup", { name: "View" });
    const preview = within(group).getByRole("radio", { name: /Preview/ });
    const code = within(group).getByRole("radio", { name: /Code/ });
    expect(preview).toHaveAttribute("aria-checked", "true");
    expect(code).toHaveAttribute("aria-checked", "false");

    fireEvent.click(code);
    expect(code).toHaveAttribute("aria-checked", "true");
    // The source is shown as code, and the running page is gone.
    await waitFor(() => expect(shownSource()).toContain("window.x = 1;"));
    expect(frames()).toHaveLength(0);

    // Back on the preview the page waits for a tap again: leaving it stopped
    // the page, and no frame exists until the next tap.
    fireEvent.click(preview);
    expect(screen.getByRole("button", { name: "Run page" })).toBeInTheDocument();
    expect(frames()).toHaveLength(0);
    expect(shownSource()).toBe("");
  });

  it("moves between the two views with the arrow keys (radio group)", async () => {
    fetchSpy = mockFetch(PAGE);
    await renderHtml();
    const preview = screen.getByRole("radio", { name: /Preview/ });
    fireEvent.keyDown(preview, { key: "ArrowRight" });
    expect(screen.getByRole("radio", { name: /Code/ })).toHaveAttribute("aria-checked", "true");
  });

  it("opens a fullscreen view with the same sandbox and closes it again", async () => {
    fetchSpy = mockFetch(PAGE);
    await renderHtml();
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));

    const dialog = screen.getByRole("dialog");
    const big = within(dialog).getByTitle(/Rendered page/i) as HTMLIFrameElement;
    expect(big.getAttribute("sandbox")).toBe("allow-scripts");
    expect(big.getAttribute("srcdoc")).toContain("<h1>Helix page</h1>");
    // Only one running copy of the page: the inline frame pauses meanwhile.
    expect(screen.getAllByTitle(/Rendered page/i)).toHaveLength(1);

    // Escape closes it …
    act(() => {
      fireEvent.keyDown(window, { key: "Escape" });
    });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByTitle(/Rendered page/i)).toBeInTheDocument();

    // … and so does the close button (≥ 44 px target).
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
    const close = screen.getByRole("button", { name: "Close fullscreen" });
    expect(close.className).toContain("min-h-11");
    expect(close.className).toContain("min-w-11");
    fireEvent.click(close);
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("keeps the Download control for HTML files", async () => {
    fetchSpy = mockFetch(PAGE);
    await renderHtml();
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
  });

  it("leaves other text formats on the code view (xml, css stay code)", async () => {
    fetchSpy = mockFetch("<root><a>1</a></root>");
    render(<FilePreview fileUrl="/x" path="feed.xml" />);
    await waitFor(() => expect(shownSource()).toContain("<root>"));
    expect(frames()).toHaveLength(0);
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });
});

// Click-to-run (security reviews 2026-10-21). A sandboxed srcdoc frame runs
// in MC's own process (always on iOS): one `for(;;){}` froze all of MC, and in
// WebKit `history.back()` from the page moved the whole tab. A still with
// scripts off (sandbox="") was not enough: pure markup (an SVG <use> tree)
// froze MC for > 45 s in it, and it already loaded the page's https: images,
// styles and frames (a beacon to hosts the page picks, with MC's origin as
// Referer in WebKit). So before the operator's tap there is NO frame at all —
// nothing of the page is parsed — and only the tap mounts the page.
describe("HtmlPreview — click to run", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn> | undefined;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: { getItem: () => "tok", setItem: () => undefined, removeItem: () => undefined, clear: () => undefined },
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
    fetchSpy = undefined;
  });

  it("opens with no frame at all before the tap — nothing of the page is parsed", async () => {
    fetchSpy = mockFetch(PAGE);
    await renderGate();
    expect(frames()).toHaveLength(0);
    // Not as srcdoc, not as markup: the page is nowhere in MC's DOM yet.
    expect(document.body.innerHTML).not.toContain("Helix page");
    expect(screen.getByText(RUN_HINT)).toBeInTheDocument();
    expect(screen.queryByTitle(/Rendered page/i)).toBeNull();
  });

  it("starts the page only on a tap on 'Run page' (a ≥ 44 px button)", async () => {
    fetchSpy = mockFetch(PAGE);
    const run = await renderGate();
    expect(run.className).toContain("min-h-11");

    fireEvent.click(run);
    const live = screen.getByTitle(/Rendered page/i) as HTMLIFrameElement;
    expect(live.getAttribute("sandbox")).toBe(HTML_PREVIEW_SANDBOX);
    expect(live.getAttribute("srcdoc")).toContain("<h1>Helix page</h1>");
    expect(frames()).toEqual([live]);
    expect(screen.queryByRole("button", { name: "Run page" })).toBeNull();
    expect(screen.queryByText(RUN_HINT)).toBeNull();
  });

  it("opens fullscreen with the Run button too, and runs there only after the tap", async () => {
    fetchSpy = mockFetch(PAGE);
    await renderGate();
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
    const dialog = screen.getByRole("dialog");
    expect(frames()).toHaveLength(0);
    // One Run button at a time: the inline one pauses while fullscreen is open.
    expect(screen.getAllByRole("button", { name: "Run page" })).toHaveLength(1);

    fireEvent.click(within(dialog).getByRole("button", { name: "Run page" }));
    expect(within(dialog).getByTitle(/Rendered page/i).getAttribute("sandbox")).toBe("allow-scripts");
    expect(frames()).toHaveLength(1);

    // Started is started: closing fullscreen keeps the page running inline.
    fireEvent.click(within(dialog).getByRole("button", { name: "Close fullscreen" }));
    expect(screen.getByTitle(/Rendered page/i).getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("never carries the tap over to other content: a new page waits for its own tap", () => {
    const view = renderRunning();
    expect(scriptFrames()).toHaveLength(1);
    // The same HtmlPreview instance gets another file (Files page, folder list).
    view.rerender(<HtmlPreview html="<p>another page</p>" name="other.html" code={<pre />} />);
    expect(frames()).toHaveLength(0);
    expect(document.body.innerHTML).not.toContain("another page");
    fireEvent.click(screen.getByRole("button", { name: "Run page" }));
    expect(screen.getByTitle(/Rendered page/i).getAttribute("srcdoc")).toContain("another page");
  });

  it("keeps hostile pages out until the tap: a script loop, history.back(), an SVG <use> bomb", () => {
    // The SVG needs no script: 8 levels of 10 <use> each expand to 10^8 nodes
    // and froze MC for > 45 s even in a frame with sandbox="" (both engines).
    const levels = Array.from({ length: 8 }, (_, i) =>
      `<g id="l${i + 1}">${`<use href="#l${i}"/>`.repeat(10)}</g>`).join("");
    const hostile =
      "<script>for(;;){}</script><script>history.back()</script>" +
      `<svg><defs><path id="l0" d="M0 0h1"/>${levels}</defs><use href="#l8"/></svg>` +
      '<img src="https://beacon.example/pixel">';
    render(<HtmlPreview html={hostile} name="loop.html" code={<pre />} />);
    expect(frames()).toHaveLength(0);
    expect(document.querySelectorAll("svg use, img[src^='https://beacon']")).toHaveLength(0);
    expect(document.body.innerHTML).not.toContain("for(;;){}");
    expect(screen.getByRole("button", { name: "Run page" })).toBeInTheDocument();
  });

  it("counts a frame without any sandbox attribute as one that can run scripts (test helper)", () => {
    // The worst case — no sandbox at all — must never pass for a safe frame.
    const bare = document.createElement("iframe");
    document.body.appendChild(bare);
    try {
      expect(scriptFrames()).toEqual([bare]);
      bare.setAttribute("sandbox", "");
      expect(scriptFrames()).toHaveLength(0);
      bare.setAttribute("sandbox", "allow-forms allow-scripts");
      expect(scriptFrames()).toEqual([bare]);
    } finally {
      bare.remove();
    }
  });
});

// The page can draw anything inside its frame — a fake MC login included.
// MC's own chrome around the frame says what it is; the page cannot paint
// over it.
describe("HtmlPreview — isolation label", () => {
  const LABEL = "Agent page · isolated preview – never enter passwords here";

  it("shows the label above the box, before and after the page runs", () => {
    render(<HtmlPreview html={PAGE} name="index.html" code={<pre />} />);
    const region = document.querySelector('[data-region="html-preview"]') as HTMLElement;
    // Above the Run button in document order: MC's chrome, outside the page.
    const label = within(region).getByText(LABEL);
    const run = screen.getByRole("button", { name: "Run page" });
    expect(label.compareDocumentPosition(run) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();

    fireEvent.click(run);
    // … and above the running frame.
    const frame = screen.getByTitle(/Rendered page/i);
    expect(within(region).getByText(LABEL).compareDocumentPosition(frame) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("shows the label in the fullscreen header too", () => {
    renderRunning();
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText(LABEL)).toBeInTheDocument();
    // … together with the file name, so the operator knows which page it is.
    expect(within(dialog).getByText("index.html")).toBeInTheDocument();
  });

  it("is not shown on the code view (the source runs nothing)", () => {
    render(<HtmlPreview html={PAGE} name="index.html" code={<pre data-testid="src" />} />);
    fireEvent.click(screen.getByRole("radio", { name: /Code/ }));
    expect(screen.queryByText(LABEL)).toBeNull();
  });
});

describe("buildPreviewDocument — the guard head every preview gets", () => {
  const GUARD_START = '<base href="about:srcdoc">';

  // The guard goes FIRST, before anything the page brings — a doctype, a
  // comment, a script. A srcdoc document is never in quirks mode (HTML spec,
  // "initial" insertion mode: the quirks rules skip iframe srcdoc documents),
  // so the doctype does not have to lead. Checked in Chromium and WebKit
  // (2026-10-21): with the guard in front of an HTML 4.01 Transitional
  // doctype the page stays in CSS1Compat, the CSP meta sits in <head> and is
  // enforced, and the page's own <html lang>, <title> and styles still apply.
  it.each([
    ["<!DOCTYPE html><html><head><title>DNA</title></head><body>x</body></html>", "html5 doctype"],
    ['<!DOCTYPE HTML PUBLIC "-//W3C//DTD HTML 4.01 Transitional//EN"><p>x', "quirks-mode doctype"],
    ["<p>bare</p>", "no doctype"],
    ["<!-- generated -->\n<?xml version=\"1.0\"?>\n<!doctype html><p>x</p>", "comment and xml prologue"],
    ["<!--><script>run()</script>--><!doctype html>", "script after an abrupt empty comment"],
    ["<!-- never closed <!doctype html>", "an unclosed comment"],
    ["<script>run()</script><!doctype html>", "a script before the doctype"],
  ])("puts the guard in front of the whole page, unchanged: %s (%s)", (src) => {
    const out = buildPreviewDocument(src);
    expect(out.startsWith(GUARD_START)).toBe(true);
    expect(out).toBe(buildPreviewDocument("") + src);
  });

  it("anchors relative URLs to about:srcdoc, so #links stay in the page and nothing hits MC", () => {
    // Without this base, a srcdoc page resolves "#sec" and "logo.png" against
    // MC's own URL: an in-page link would load MC into the frame.
    expect(buildPreviewDocument(PAGE)).toContain(GUARD_START);
  });

  it("adds a no-referrer policy and the CSP (no fetch/XHR/WebSocket, no forms)", () => {
    const out = buildPreviewDocument(PAGE);
    expect(out).toContain('<meta name="referrer" content="no-referrer">');
    expect(out).toContain(`<meta http-equiv="Content-Security-Policy" content="${HTML_PREVIEW_CSP}">`);
    expect(HTML_PREVIEW_CSP).toContain("default-src 'none'");
    expect(HTML_PREVIEW_CSP).toContain("connect-src 'none'");
    expect(HTML_PREVIEW_CSP).toContain("form-action 'none'");
    // http: (LAN, MC itself over plain http) is never a source.
    expect(HTML_PREVIEW_CSP).not.toMatch(/(^|\s)http:/);
    // The CSP sits in a double-quoted attribute: it must not contain one.
    expect(HTML_PREVIEW_CSP).not.toContain('"');
  });

  it("does not scan the page at all: hostile prologues cost nothing", () => {
    // The old doctype scan was a hand-written tokenizer (and before that a
    // regex that froze the tab for seconds on 200 bytes of closed comments).
    const evil = "<!---->".repeat(20_000) + " ".repeat(200_000) + "<!-- x".repeat(2_000) + "y";
    const t0 = performance.now();
    expect(buildPreviewDocument(evil).startsWith(GUARD_START)).toBe(true);
    expect(performance.now() - t0).toBeLessThan(50);
  });
});

/** Renders HtmlPreview directly and starts the page (tap on "Run page"). */
function renderRunning(html = PAGE) {
  const view = render(<HtmlPreview html={html} name="index.html" code={<pre />} />);
  fireEvent.click(screen.getByRole("button", { name: "Run page" }));
  return view;
}

describe("HtmlPreview — review fixes (loading cap, nested Escape)", () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it("drops the loading overlay after a short cap even when load never fires (stalled CDN)", () => {
    // `load` waits for every subresource. The CSP allows https: CDNs, so one
    // stalled image or font would otherwise leave an opaque "Loading page…"
    // over a page that has already rendered — forever.
    vi.useFakeTimers();
    expect(HTML_PREVIEW_LOADING_CAP_MS).toBeGreaterThan(0);
    expect(HTML_PREVIEW_LOADING_CAP_MS).toBeLessThanOrEqual(3000);
    renderRunning();
    expect(screen.getByText("Loading page…")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(HTML_PREVIEW_LOADING_CAP_MS - 1);
    });
    expect(screen.getByText("Loading page…")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(screen.queryByText("Loading page…")).toBeNull();
    // The frame itself was never touched: no load event, still mounted.
    expect(screen.getByTitle(/Rendered page/i)).toBeInTheDocument();
  });

  it("starts the cap again for the fullscreen frame (its own copy of the page)", () => {
    vi.useFakeTimers();
    renderRunning();
    act(() => {
      vi.advanceTimersByTime(HTML_PREVIEW_LOADING_CAP_MS);
    });
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
    const dialog = screen.getByRole("dialog");
    expect(within(dialog).getByText("Loading page…")).toBeInTheDocument();
    act(() => {
      vi.advanceTimersByTime(HTML_PREVIEW_LOADING_CAP_MS);
    });
    expect(within(dialog).queryByText("Loading page…")).toBeNull();
  });

  /** A task modal: it closes on Esc through the shared hook, as TaskDetailPanel does. */
  function TaskModal({ onClose }: { onClose: () => void }) {
    useEscapeKey(() => onClose());
    return <HtmlPreview html={PAGE} name="index.html" code={<pre />} />;
  }

  it("Escape in fullscreen closes ONLY the fullscreen, not the task modal behind it", () => {
    const closeTask = vi.fn();
    render(<TaskModal onClose={closeTask} />);
    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
    const close = screen.getByRole("button", { name: "Close fullscreen" });
    expect(document.activeElement).toBe(close);

    // A real key press lands on the focused element and bubbles to window.
    fireEvent.keyDown(close, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(closeTask).not.toHaveBeenCalled();

    // With the fullscreen gone, the next Esc belongs to the modal again.
    fireEvent.keyDown(document.body, { key: "Escape" });
    expect(closeTask).toHaveBeenCalledTimes(1);
  });

  it("binds the fullscreen Escape listener once, not once per render", () => {
    const add = vi.spyOn(window, "addEventListener");
    try {
      const { rerender } = renderRunning();
      fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
      const keydownBinds = () => add.mock.calls.filter(([type]) => type === "keydown").length;
      const afterOpen = keydownBinds();
      expect(afterOpen).toBeGreaterThanOrEqual(1);
      // A parent re-render hands FullscreenView a new inline onClose each time.
      rerender(<HtmlPreview html={PAGE} name="index.html" code={<pre />} />);
      rerender(<HtmlPreview html={PAGE} name="index.html" code={<pre />} />);
      expect(keydownBinds()).toBe(afterOpen);
      // … and Esc still closes with the latest handler.
      fireEvent.keyDown(document.body, { key: "Escape" });
      expect(screen.queryByRole("dialog")).toBeNull();
    } finally {
      add.mockRestore();
    }
  });
});

describe("filePreview i18n", () => {
  type Tree = { [k: string]: string | Tree };
  const flat = (t: Tree, p = ""): string[] =>
    Object.entries(t).flatMap(([k, v]) => (typeof v === "string" ? [p + k] : flat(v, `${p}${k}.`)));

  it("has the same keys in English and German", () => {
    const enNs = (en as unknown as Record<string, Tree>).filePreview;
    const deNs = (de as unknown as Record<string, Tree>).filePreview;
    expect(enNs).toBeDefined();
    expect(deNs).toBeDefined();
    expect(flat(deNs).sort()).toEqual(flat(enNs).sort());
  });

  it("speaks German in the German catalog", () => {
    const deNs = (de as unknown as { filePreview: { html: Record<string, string>; download: string } }).filePreview;
    expect(deNs.html.preview).toBe("Vorschau");
    expect(deNs.html.fullscreen).toBe("Vollbild");
    expect(deNs.html.run).toBe("Seite starten");
    expect(deNs.html.runHint).toBe("Die Seite läuft erst, wenn du sie startest.");
    expect(deNs.html.isolationLabel).toBe("Agenten-Seite · isolierte Vorschau – hier nie Passwörter eingeben");
    expect(deNs.download).toBe("Herunterladen");
  });
});
