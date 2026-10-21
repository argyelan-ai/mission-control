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

async function renderHtml(path = "site/index.html") {
  render(<FilePreview fileUrl={URL_HTML} path={path} />);
  return (await screen.findByTitle(/Rendered page/i)) as HTMLIFrameElement;
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
    expect(screen.queryByTitle(/Rendered page/i)).toBeNull();
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
    expect(screen.queryByTitle(/Rendered page/i)).toBeNull();

    fireEvent.click(preview);
    expect(screen.getByTitle(/Rendered page/i)).toBeInTheDocument();
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
    expect(screen.queryByTitle(/Rendered page/i)).toBeNull();
    expect(screen.queryByRole("radiogroup")).toBeNull();
  });
});

describe("buildPreviewDocument — the guard head every preview gets", () => {
  const GUARD_START = '<base href="about:srcdoc">';

  it("keeps the doctype first (standards mode) and puts the guard right after it", () => {
    const out = buildPreviewDocument(PAGE);
    expect(out.startsWith("<!DOCTYPE html>" + GUARD_START)).toBe(true);
    expect(out.endsWith(PAGE.slice("<!DOCTYPE html>".length))).toBe(true);
  });

  it("skips a BOM, whitespace, comments and an xml prologue before the doctype", () => {
    const head = "\uFEFF  <!-- generated -->\n<?xml version=\"1.0\"?>\n<!doctype html>";
    const out = buildPreviewDocument(head + "<p>x</p>");
    expect(out).toBe(head + out.slice(head.length, out.indexOf("<p>")) + "<p>x</p>");
    expect(out.slice(head.length).startsWith(GUARD_START)).toBe(true);
  });

  it("prepends the guard when there is no doctype", () => {
    expect(buildPreviewDocument("<p>bare</p>").startsWith(GUARD_START)).toBe(true);
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

  it("stays fast on a pathological prologue (no catastrophic backtracking)", () => {
    const evil = " ".repeat(200_000) + "<!-- x".repeat(2_000) + "y";
    const started = performance.now();
    buildPreviewDocument(evil);
    expect(performance.now() - started).toBeLessThan(500);
  });

  it("stays linear on CLOSED comments with no doctype (the slow path of a lazy comment pattern)", () => {
    // A lazy `<!--[\s\S]*?-->` inside a repeated group can split a run of n
    // closed comments 2^(n-1) ways once no doctype follows — on MC's main
    // thread, during render. 28 comments (196 bytes) take ~0.9 s even after
    // V8 has compiled the hot regex to native code (~12 s before that), and
    // microseconds for a linear scan. The small case comes first so a
    // regression fails here instead of hanging on the big one.
    const small = "<!---->".repeat(28) + "<p>x";
    const t0 = performance.now();
    expect(buildPreviewDocument(small).startsWith(GUARD_START)).toBe(true);
    expect(performance.now() - t0).toBeLessThan(100);

    const big = "<!---->".repeat(20_000) + "<!-- a -- b --!> <?x?>\n".repeat(5_000) + "<p>x";
    const t1 = performance.now();
    expect(buildPreviewDocument(big).startsWith(GUARD_START)).toBe(true);
    expect(performance.now() - t1).toBeLessThan(250);
  });

  // The guard must sit before the first thing the BROWSER would run. These
  // follow the HTML tokenizer: `<!-->` and `<!--->` are complete (empty)
  // comments, `--!>` ends a comment, `--->` too.
  it.each([
    ["<!-->", "abrupt empty comment"],
    ["<!--->", "abrupt empty comment with a dash"],
    ["<!-- a --!>", "comment closed by --!>"],
    ["<!-- a --->", "comment closed by --->"],
    ["<!-- -- inner -- -->", "dashes inside a comment"],
  ])("treats %s as a complete comment before the doctype (%s)", (comment) => {
    const head = `${comment}\n<!DocType html>`;
    const out = buildPreviewDocument(head + "<p>x</p>");
    expect(out.slice(head.length).startsWith(GUARD_START)).toBe(true);
    expect(out.endsWith("<p>x</p>")).toBe(true);
  });

  it.each([
    ["<!--><script>run()</script>--><!doctype html>", "script after an abrupt empty comment"],
    ["<!-- a --!><script>run()</script>--><!doctype html>", "script after a --!> close"],
    ["<p>text</p><!doctype html>", "content before the doctype"],
    ["<!-- never closed <!doctype html>", "an unclosed comment swallows the doctype"],
    ["<?pi never closed <!doctype html", "an unclosed processing instruction"],
  ])("puts the guard first when markup precedes the doctype: %s (%s)", (src) => {
    expect(buildPreviewDocument(src)).toBe(
      buildPreviewDocument("") + src,
    );
  });

  it("accepts a doctype without a space before its name (`<!DOCTYPEhtml>`)", () => {
    const out = buildPreviewDocument("<!DOCTYPEhtml><p>x</p>");
    expect(out.startsWith("<!DOCTYPEhtml>" + GUARD_START)).toBe(true);
  });
});

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
    render(<HtmlPreview html={PAGE} name="index.html" code={<pre />} />);
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
    render(<HtmlPreview html={PAGE} name="index.html" code={<pre />} />);
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
      const { rerender } = render(<HtmlPreview html={PAGE} name="index.html" code={<pre />} />);
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
    expect(deNs.download).toBe("Herunterladen");
  });
});
