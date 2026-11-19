import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, fireEvent, waitFor, within } from "@testing-library/react";
import { FilePreview } from "../FilePreview";
import { HTML_PREVIEW_MINT_TIMEOUT_MS, HTML_PREVIEW_SANDBOX, HtmlPreview, hasFolderRefs } from "../HtmlPreview";
import { api } from "@/lib/api";

// Multi-file HTML deliverables (a page with its own style.css, logo.png,
// app.js, about.html). A srcdoc frame has no folder to resolve those
// against, so for a deliverable the preview asks the backend for a short-
// lived, signed URL of the page's folder and loads the frame FROM it — still
// sandboxed to exactly "allow-scripts". A page without such references stays
// on srcdoc (part 1), and so does every caller that is not a deliverable.

const MULTI = `<!doctype html><html><head><link rel="stylesheet" href="style.css"></head>
<body><img src="logo.png"><script src="app.js"></script></body></html>`;
const SINGLE = `<!doctype html><html><head><style>body{color:red}</style></head>
<body><h1>One file</h1><a href="#s">jump</a><img src="https://cdn.example/x.png"></body></html>`;
const VIEW = "/api/v1/deliverable-view/dv1.claims.sig/index.html";

const frame = () => screen.findByTitle(/Rendered page/i) as Promise<HTMLIFrameElement>;
/** Click to run (#925): the page, and with it the folder-URL mint, starts on the tap. */
const tapRun = async () => fireEvent.click(await screen.findByRole("button", { name: "Run page" }));

describe("hasFolderRefs — does the page need the files next to it?", () => {
  it.each([
    ['<link rel="stylesheet" href="style.css">', "stylesheet"],
    ['<img src="logo.png">', "image"],
    ['<script src="js/app.js"></script>', "script"],
    ['<a href="about.html">About</a>', "link to another page of the folder"],
    ['<img srcset="https://cdn.example/a.png 1x, img/b.png 2x">', "srcset candidate"],
    ['<video poster="poster.jpg"></video>', "video poster"],
    ['<div style="background:url(bg.png)"></div>', "style attribute url()"],
    ["<style>body{background:url('img/a.png')}</style>", "stylesheet url()"],
    ['<style>@import "theme.css";</style>', "@import"],
    ['<script type="module">import { x } from "./lib.js";</script>', "module import"],
    ['<svg><image href="pic.png"></image></svg>', "svg image"],
    ['<img src="../shared/logo.png">', "a parent folder"],
  ])("yes: %s (%s)", (html) => {
    expect(hasFolderRefs(html)).toBe(true);
  });

  it.each([
    [SINGLE, "inline page with https: and #links"],
    ['<img src="data:image/png;base64,AAAA">', "data: URL"],
    ['<script src="//cdn.example/x.js"></script>', "protocol-relative"],
    ['<img src="/logo.png">', "root-relative (MC's origin, never the folder)"],
    ['<a href="mailto:a@example.com">m</a><a href="javascript:void 0">j</a>', "other schemes"],
    ['<a href="?tab=2">q</a><a href="">e</a>', "query-only and empty"],
    ["<style>body{background:url(https://cdn.example/a.png)}</style>", "absolute url()"],
    ["", "empty file"],
  ])("no: %s (%s)", (html) => {
    expect(hasFolderRefs(html)).toBe(false);
  });

  it("stays linear on a huge unterminated url( run", () => {
    const css = "<style>" + "url(".repeat(200_000) + "</style>";
    const t0 = performance.now();
    hasFolderRefs(css);
    expect(performance.now() - t0).toBeLessThan(1500);
  });
});

describe("HtmlPreview — a multi-file page loads from its signed folder URL", () => {
  afterEach(() => vi.restoreAllMocks());

  it("loads the frame from the minted URL, sandboxed to exactly allow-scripts", async () => {
    const mint = vi.fn().mockResolvedValue(VIEW);
    render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);
    await tapRun();

    const f = await frame();
    await waitFor(() => expect(f.getAttribute("src")).toBe(VIEW));
    // Never both: srcdoc would win over src in a browser.
    expect(f.hasAttribute("srcdoc")).toBe(false);
    expect(f.getAttribute("sandbox")).toBe(HTML_PREVIEW_SANDBOX);
    expect(HTML_PREVIEW_SANDBOX).toBe("allow-scripts");
    expect(f.getAttribute("referrerpolicy")).toBe("no-referrer");
    expect(mint).toHaveBeenCalledTimes(1);
  });

  it("mints no folder URL and builds no frame before the tap (click to run)", async () => {
    const mint = vi.fn().mockResolvedValue(VIEW);
    render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);

    // Opening the deliverable alone: the Run button, no frame, no request.
    await screen.findByRole("button", { name: "Run page" });
    expect(screen.queryByTitle(/Rendered page/i)).toBeNull();
    expect(mint).not.toHaveBeenCalled();

    await tapRun();
    const f = await frame();
    await waitFor(() => expect(f.getAttribute("src")).toBe(VIEW));
    expect(mint).toHaveBeenCalledTimes(1);
  });

  it("keeps a single-file page on srcdoc — no URL is minted", async () => {
    const mint = vi.fn().mockResolvedValue(VIEW);
    render(<HtmlPreview html={SINGLE} name="index.html" code={<pre />} folderUrl={mint} />);
    await tapRun();

    const f = await frame();
    expect(f.getAttribute("srcdoc")).toContain("One file");
    expect(f.hasAttribute("src")).toBe(false);
    expect(mint).not.toHaveBeenCalled();
  });

  it("falls back to srcdoc when the folder URL cannot be minted", async () => {
    const mint = vi.fn().mockRejectedValue(new Error("API 404"));
    render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);
    await tapRun();

    const f = await frame();
    await waitFor(() => expect(f.getAttribute("srcdoc")).toContain("style.css"));
    expect(f.hasAttribute("src")).toBe(false);
    expect(f.getAttribute("sandbox")).toBe("allow-scripts");
  });

  it("falls back to srcdoc when minting does not answer in time (no endless spinner)", async () => {
    vi.useFakeTimers();
    try {
      let resolve: (url: string) => void = () => undefined;
      const mint = vi.fn(() => new Promise<string>((r) => { resolve = r; }));
      render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);
      fireEvent.click(screen.getByRole("button", { name: "Run page" })); // sync: fake timers

      await act(async () => { vi.advanceTimersByTime(HTML_PREVIEW_MINT_TIMEOUT_MS - 1); });
      expect(screen.queryByTitle(/Rendered page/i)).toBeNull(); // still asking
      await act(async () => { vi.advanceTimersByTime(1); });

      const f = screen.getByTitle(/Rendered page/i) as HTMLIFrameElement;
      expect(f.getAttribute("srcdoc")).toContain("style.css");
      expect(f.hasAttribute("src")).toBe(false);
      expect(f.getAttribute("sandbox")).toBe("allow-scripts");

      // A late answer does not swap the page under the reader.
      await act(async () => { resolve(VIEW); });
      const after = screen.getByTitle(/Rendered page/i) as HTMLIFrameElement;
      expect(after.hasAttribute("src")).toBe(false);
      expect(after.getAttribute("srcdoc")).toContain("style.css");
    } finally {
      vi.useRealTimers();
    }
  });

  it("shows the loading state while the URL is minted (no empty frame)", async () => {
    let resolve: (url: string) => void = () => undefined;
    const mint = vi.fn(() => new Promise<string>((r) => { resolve = r; }));
    render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);
    await tapRun();

    expect(screen.getByText("Loading page…")).toBeInTheDocument();
    expect(screen.queryByTitle(/Rendered page/i)).toBeNull();
    resolve(VIEW);
    expect((await frame()).getAttribute("src")).toBe(VIEW);
  });

  it("does not mint again when the parent re-renders with a new inline function", async () => {
    const mint = vi.fn().mockResolvedValue(VIEW);
    const { rerender } = render(
      <HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={() => mint()} />,
    );
    await tapRun();
    await waitFor(() => expect(mint).toHaveBeenCalledTimes(1));
    rerender(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={() => mint()} />);
    rerender(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={() => mint()} />);
    await frame();
    expect(mint).toHaveBeenCalledTimes(1);
  });

  it("mints a fresh URL for the fullscreen view, same sandbox", async () => {
    const mint = vi.fn().mockResolvedValueOnce(VIEW).mockResolvedValueOnce(VIEW + "?2");
    render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} folderUrl={mint} />);
    await tapRun();
    await waitFor(() => expect(mint).toHaveBeenCalledTimes(1));

    fireEvent.click(screen.getByRole("button", { name: "Fullscreen" }));
    const dialog = screen.getByRole("dialog");
    const f = (await within(dialog).findByTitle(/Rendered page/i)) as HTMLIFrameElement;
    await waitFor(() => expect(f.getAttribute("src")).toBe(VIEW + "?2"));
    expect(f.getAttribute("sandbox")).toBe("allow-scripts");
    expect(mint).toHaveBeenCalledTimes(2);
  });

  it("without a folder URL (workspace tab, Files page) a multi-file page stays on srcdoc", async () => {
    render(<HtmlPreview html={MULTI} name="index.html" code={<pre />} />);
    await tapRun();
    const f = await frame();
    expect(f.getAttribute("srcdoc")).toContain("style.css");
    expect(f.hasAttribute("src")).toBe(false);
  });
});

describe("FilePreview → HtmlPreview: deliverables pass their folder URL", () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: { getItem: () => "tok", setItem: () => undefined, removeItem: () => undefined, clear: () => undefined },
      configurable: true,
      writable: true,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  it("hands htmlFolderUrl through to the preview", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(MULTI, { status: 200 }));
    const mint = vi.fn().mockResolvedValue(VIEW);
    render(
      <FilePreview fileUrl="/api/v1/boards/b/tasks/t/deliverables/d/file" path="index.html" htmlFolderUrl={mint} />,
    );
    await tapRun();
    const f = await frame();
    await waitFor(() => expect(f.getAttribute("src")).toBe(VIEW));
  });
});

describe("api.tasks.deliverables.viewUrl", () => {
  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: { getItem: () => "tok", setItem: () => undefined, removeItem: () => undefined, clear: () => undefined },
      configurable: true,
      writable: true,
    });
  });
  afterEach(() => vi.restoreAllMocks());

  const reply = (body: unknown) =>
    new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });

  it("POSTs the page's subpath and returns the folder URL", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(reply({ url: VIEW, expires_in: 600 }));

    const url = await api.tasks.deliverables.viewUrl("b", "t", "d", "site/index.html");

    expect(url).toBe(VIEW);
    const [path, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(path).toBe("/api/v1/boards/b/tasks/t/deliverables/d/view-url");
    expect(init.method).toBe("POST");
    expect(JSON.parse(String(init.body))).toEqual({ subpath: "site/index.html" });
    expect((init.headers as Record<string, string>).Authorization).toBe("Bearer tok");
  });

  it("sends no subpath for a single-file deliverable", async () => {
    const spy = vi.spyOn(globalThis, "fetch").mockResolvedValue(reply({ url: VIEW, expires_in: 600 }));
    await api.tasks.deliverables.viewUrl("b", "t", "d");
    const [, init] = spy.mock.calls[0] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ subpath: null });
  });

  it.each([
    "https://evil.example/x.html",
    "//evil.example/api/v1/deliverable-view/x/i.html",
    "/api/v1/files/content?root=vault",
    "javascript:alert(1)",
  ])("never hands a frame a URL outside the view route: %s", async (bad) => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(reply({ url: bad, expires_in: 600 }));
    await expect(api.tasks.deliverables.viewUrl("b", "t", "d")).rejects.toThrow();
  });
});
