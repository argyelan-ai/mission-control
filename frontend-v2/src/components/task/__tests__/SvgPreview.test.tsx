import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import { FilePreview } from "../FilePreview";
import { SVG_SOURCE_MAX_BYTES, SVG_SOURCE_MAX_LINES, SVG_HIGHLIGHT_MAX_BYTES } from "../SvgPreview";
import en from "../../../../messages/en.json";
import de from "../../../../messages/de.json";

// An SVG file from an agent is never drawn by MC. Pure markup without any
// script (a <use> tree, 8 levels of 10) blocked MC's main thread for more
// than 20 s in WebKit (the Safari engine, every iPhone browser) whichever way
// it was drawn: as an <img>, as an <img> in a sandboxed frame, as the frame's
// own document, even in a frame from another site. So FilePreview shows an
// SVG as its source (with a line on why) and offers the download. These tests
// pin that: no <img>, no frame, no object/embed, no inline SVG of the file,
// and no image-typed blob URL — for any SVG, the bomb included. Other image
// formats keep their picture.

const URL_SVG = "/api/v1/boards/b/tasks/t/deliverables/d/file";

const LOGO = `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40"><circle id="svgpv-logo" cx="20" cy="20" r="18" fill="teal"/></svg>`;

const LEVELS = Array.from({ length: 8 }, (_, i) => `<g id="l${i + 1}">${`<use href="#l${i}"/>`.repeat(10)}</g>`).join("");
const BOMB = `<svg xmlns="http://www.w3.org/2000/svg" id="svgpv-bomb"><defs><path id="l0" d="M0 0h1"/>${LEVELS}</defs><use href="#l8"/></svg>`;

const NOTE = "MC shows SVG images as code only – an SVG file can freeze the app.";
const DOWNLOAD_HINT = "Download it to see the image.";

function mockFetch(body: string, status = 200) {
  return vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(body, { status }));
}

/** Source shown by the code view (the highlighter splits it into spans). */
const shownSource = () => document.querySelector("pre")?.textContent ?? "";

/** Lets the fetch resolve and every state update after it land, so a
 *  picture that would appear once the bytes are in is in the DOM by now. */
async function settle(fetchSpy: ReturnType<typeof vi.spyOn> | undefined) {
  await waitFor(() => expect(fetchSpy).toHaveBeenCalled());
  for (let i = 0; i < 5; i++) await act(async () => { await Promise.resolve(); });
}

/** Anything that would make the browser draw (parse and lay out) the SVG. */
function drawnSvg() {
  return {
    img: document.querySelectorAll("img").length,
    frame: document.querySelectorAll("iframe, object, embed, frame").length,
    // the file's own elements as live DOM nodes (inline SVG)
    fileNodes: document.querySelectorAll("#svgpv-logo, #svgpv-bomb, use").length,
  };
}

describe("SvgPreview — SVG files are shown as code, never drawn", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn> | undefined;
  let createUrl: ReturnType<typeof vi.fn>;
  const origCreate = URL.createObjectURL;
  const origRevoke = URL.revokeObjectURL;

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
    // jsdom has no object URLs; record every one FilePreview would mint.
    createUrl = vi.fn(() => "blob:mc/test");
    URL.createObjectURL = createUrl as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
    fetchSpy = undefined;
    URL.createObjectURL = origCreate;
    URL.revokeObjectURL = origRevoke;
  });

  it.each(["logo.svg", "CHART.SVG", "dir/sub/icon.Svg"])(
    "shows %s as its source with the note — no picture, no frame, no image blob",
    async (path) => {
      fetchSpy = mockFetch(LOGO);
      render(<FilePreview fileUrl={URL_SVG} path={path} />);

      await settle(fetchSpy);
      expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
      await waitFor(() => expect(shownSource()).toContain("<circle"));
      expect(screen.getByText(NOTE)).toBeInTheDocument();
      // The bytes come in as text (with the Bearer header), never as an
      // object URL an <img> could draw.
      expect(createUrl).not.toHaveBeenCalled();
      expect(fetchSpy).toHaveBeenCalledWith(URL_SVG, expect.objectContaining({
        headers: expect.objectContaining({ Authorization: "Bearer tok" }),
      }));
    },
  );

  it("never draws the <use> bomb — not on open, not after the source is shown", async () => {
    fetchSpy = mockFetch(BOMB);
    render(<FilePreview fileUrl={URL_SVG} path="bomb.svg" />);

    // Right after opening, and once the bytes are in: nothing drawn, nothing minted.
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
    await settle(fetchSpy);
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
    expect(createUrl).not.toHaveBeenCalled();
    await waitFor(() => expect(shownSource()).toContain('<use href="#l8"/>'));
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
    expect(createUrl).not.toHaveBeenCalled();
    // No button that would draw it later either.
    expect(screen.queryByRole("button", { name: /show image|run page|preview/i })).toBeNull();
  });

  it("puts the note above the source, inside the SVG region, and offers the download", async () => {
    fetchSpy = mockFetch(LOGO);
    render(<FilePreview fileUrl={URL_SVG} path="logo.svg" />);
    await waitFor(() => expect(shownSource()).toContain("<circle"));

    const region = document.querySelector('[data-region="svg-preview"]') as HTMLElement;
    expect(region).not.toBeNull();
    const note = within(region).getByText(NOTE);
    const pre = region.querySelector("pre") as HTMLElement;
    expect(note.compareDocumentPosition(pre) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(within(region).getByText(DOWNLOAD_HINT)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /download/i })).toBeInTheDocument();
  });

  it("makes the download — the way to see the picture — a ≥ 44 px target on a phone", async () => {
    fetchSpy = mockFetch(LOGO);
    render(<FilePreview fileUrl={URL_SVG} path="logo.svg" />);
    await waitFor(() => expect(shownSource()).toContain("<circle"));
    const download = screen.getByRole("button", { name: /download/i });
    expect(download.className).toContain("min-h-11");
  });

  it("does not point to a download that is not offered (showDownload=false)", async () => {
    fetchSpy = mockFetch(LOGO);
    render(<FilePreview fileUrl={URL_SVG} path="logo.svg" showDownload={false} />);
    await settle(fetchSpy);
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
    await waitFor(() => expect(shownSource()).toContain("<circle"));

    expect(screen.getByText(NOTE)).toBeInTheDocument();
    expect(screen.queryByText(DOWNLOAD_HINT)).toBeNull();
    expect(screen.queryByRole("button", { name: /download/i })).toBeNull();
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
  });

  it("shows the error state when the file cannot be fetched — and draws nothing", async () => {
    fetchSpy = mockFetch("nope", 500);
    render(<FilePreview fileUrl={URL_SVG} path="logo.svg" />);
    expect(await screen.findByText("Failed to load file")).toBeInTheDocument();
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
    expect(createUrl).not.toHaveBeenCalled();
  });

  it.each(["pic.png", "photo.jpg", "photo.jpeg", "anim.gif", "shot.webp"])(
    "leaves %s as a picture (blob URL in an <img>)",
    async (path) => {
      // String body: jsdom's Blob lacks stream() (see FilePreview.test.tsx).
      fetchSpy = mockFetch("x");
      render(<FilePreview fileUrl={`/api/v1/files/content?root=media&subpath=${path}`} path={path} />);

      const img = await waitFor(() => {
        const el = document.querySelector("img");
        expect(el).not.toBeNull();
        return el as HTMLImageElement;
      });
      expect(img.getAttribute("src")).toBe("blob:mc/test");
      expect(img.getAttribute("alt")).toBe(path);
      expect(createUrl).toHaveBeenCalledTimes(1);
      expect(document.querySelector('[data-region="svg-preview"]')).toBeNull();
      expect(screen.queryByText(NOTE)).toBeNull();
    },
  );
});

// The source view has a cost of its own: highlighted XML with line numbers
// is several DOM nodes per token. A 10 MB SVG (a big chart is enough, no
// bomb needed) blocked MC for more than 21 s that way in Chromium and WebKit,
// a 2 MB one for 3–5.7 s. So the source view is capped: only the start of a
// long file is read and shown, as plain text, with a line that says so; only
// a small file is highlighted.

const SHORTENED = "Long file – only the beginning is shown.";

/** A matplotlib-like chart: one element per line. */
function chartSvg(points: number) {
  let s = `<?xml version="1.0" encoding="utf-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 800 600">\n`;
  for (let i = 0; i < points; i++) {
    s += `  <circle id="p${i}" cx="${(i * 7919) % 800}.123" cy="${(i * 104729) % 600}.456" r="2.5" style="fill: #1f77b4"/>\n`;
  }
  return s + "</svg>\n";
}

/** The shown source without the line numbers. */
function shownCode() {
  const pre = document.querySelector("pre");
  if (!pre) return "";
  const copy = pre.cloneNode(true) as HTMLElement;
  copy.querySelectorAll(".linenumber").forEach((n) => n.remove());
  return copy.textContent ?? "";
}

const shownLines = () => shownCode().split("\n").length;
/** Elements the source view put in the page — the actual cost. */
const preNodes = () => document.querySelectorAll("pre *").length;
/** Plain text: about one line-number span and one text span per line.
 *  Highlighted XML has several spans per line. */
const isPlain = () => preNodes() <= 2 * shownLines() + 3;
const utf8 = (s: string) => new TextEncoder().encode(s).length;

describe("SvgPreview — a long SVG is shown only from its start, as plain text", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn> | undefined;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: { getItem: () => "tok", setItem: () => undefined, removeItem: () => undefined, clear: () => undefined },
      configurable: true, writable: true,
    });
  });
  afterEach(() => {
    fetchSpy?.mockRestore();
    fetchSpy = undefined;
  });

  it("shows only the start of a big chart, unhighlighted, and says so", async () => {
    // Just above the limit: enough to need the cut, small enough that the
    // uncapped view (the red state) still renders in jsdom instead of OOM.
    const points = Math.ceil((1.5 * SVG_SOURCE_MAX_BYTES) / 80);
    const chart = chartSvg(points);
    expect(utf8(chart)).toBeGreaterThan(SVG_SOURCE_MAX_BYTES);
    fetchSpy = mockFetch(chart);
    render(<FilePreview fileUrl={URL_SVG} path="chart.svg" />);

    await waitFor(() => expect(shownCode()).toContain("<?xml"));
    expect(await screen.findByText(SHORTENED)).toBeInTheDocument();
    expect(utf8(shownCode())).toBeLessThanOrEqual(SVG_SOURCE_MAX_BYTES);
    expect(shownCode()).not.toContain(`id="p${points - 1}"`);
    expect(shownLines()).toBeLessThanOrEqual(SVG_SOURCE_MAX_LINES);
    expect(isPlain()).toBe(true);
    // Still never drawn, and the note on why stays.
    expect(drawnSvg()).toEqual({ img: 0, frame: 0, fileNodes: 0 });
    expect(screen.getByText(NOTE)).toBeInTheDocument();
  });

  it("cuts a file of many short lines at the line limit", async () => {
    const lines = SVG_SOURCE_MAX_LINES * 3;
    const svg = `<svg xmlns="http://www.w3.org/2000/svg">\n${"<g/>\n".repeat(lines)}</svg>\n`;
    expect(utf8(svg)).toBeLessThan(SVG_SOURCE_MAX_BYTES); // the line limit, not the byte limit, cuts it
    fetchSpy = mockFetch(svg);
    render(<FilePreview fileUrl={URL_SVG} path="many.svg" />);

    expect(await screen.findByText(SHORTENED)).toBeInTheDocument();
    expect(shownLines()).toBeLessThanOrEqual(SVG_SOURCE_MAX_LINES);
    expect(shownCode()).not.toContain("</svg>");
    expect(isPlain()).toBe(true);
  });

  it("stops reading an endless file after the limit, instead of loading all of it", async () => {
    const CHUNK = 16 * 1024;
    const MAX_CHUNKS = 2 * Math.ceil(SVG_SOURCE_MAX_BYTES / CHUNK) + 4; // stands in for "endless"
    let pulls = 0;
    const enc = new TextEncoder();
    const stream = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (pulls >= MAX_CHUNKS) { controller.close(); return; }
        pulls += 1;
        // Mostly blanks: cheap to render even uncapped, so the red state
        // fails on the count, not on jsdom's memory.
        controller.enqueue(enc.encode(pulls === 1 ? "<svg>".padEnd(CHUNK, " ") : " ".repeat(CHUNK)));
      },
    });
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(stream, { status: 200 }));
    render(<FilePreview fileUrl={URL_SVG} path="endless.svg" />);

    expect(await screen.findByText(SHORTENED)).toBeInTheDocument();
    expect(pulls).toBeLessThanOrEqual(Math.ceil(SVG_SOURCE_MAX_BYTES / CHUNK) + 2);
    expect(utf8(shownCode())).toBeLessThanOrEqual(SVG_SOURCE_MAX_BYTES);
  });

  it("never cuts a character in half at the limit", async () => {
    // 5 ASCII bytes, then 2-byte characters: the limit falls inside one.
    const svg = `<svg>${"ä".repeat(SVG_SOURCE_MAX_BYTES)}</svg>`;
    fetchSpy = mockFetch(svg);
    render(<FilePreview fileUrl={URL_SVG} path="umlaut.svg" />);

    expect(await screen.findByText(SHORTENED)).toBeInTheDocument();
    const code = shownCode();
    expect(code.startsWith("<svg>ä")).toBe(true);
    expect(code).not.toContain("\uFFFD");
    expect(utf8(code)).toBeLessThanOrEqual(SVG_SOURCE_MAX_BYTES);
  });

  it("shows a mid-size file whole, without the shortened line, but unhighlighted", async () => {
    const svg = chartSvg(Math.ceil(SVG_HIGHLIGHT_MAX_BYTES / 70));
    expect(utf8(svg)).toBeGreaterThan(SVG_HIGHLIGHT_MAX_BYTES);
    expect(utf8(svg)).toBeLessThan(SVG_SOURCE_MAX_BYTES);
    fetchSpy = mockFetch(svg);
    render(<FilePreview fileUrl={URL_SVG} path="mid.svg" />);

    await waitFor(() => expect(shownCode()).toContain("</svg>"));
    expect(screen.queryByText(SHORTENED)).toBeNull();
    expect(isPlain()).toBe(true);
  });

  it("keeps a small SVG whole and highlighted, without the shortened line", async () => {
    fetchSpy = mockFetch(LOGO);
    render(<FilePreview fileUrl={URL_SVG} path="logo.svg" />);

    await waitFor(() => expect(shownCode()).toContain("<circle"));
    expect(shownCode()).toContain("</svg>");
    expect(screen.queryByText(SHORTENED)).toBeNull();
    // Highlighted: several spans per line (the positive control for isPlain).
    expect(isPlain()).toBe(false);
  });
});

describe("SvgPreview — wording", () => {
  type Svg = { codeOnly: string; downloadToView: string; shortened: string };
  const enSvg = (en as unknown as { filePreview: { svg: Svg } }).filePreview.svg;
  const deSvg = (de as unknown as { filePreview: { svg: Svg } }).filePreview.svg;

  it("has the same keys in English and German, none empty", () => {
    expect(Object.keys(deSvg).sort()).toEqual(Object.keys(enSvg).sort());
    for (const v of [...Object.values(enSvg), ...Object.values(deSvg)]) {
      expect(typeof v).toBe("string");
      expect(v.trim().length).toBeGreaterThan(0);
    }
  });

  it("pins the English and German wording", () => {
    expect(enSvg.codeOnly).toBe(NOTE);
    expect(enSvg.downloadToView).toBe(DOWNLOAD_HINT);
    expect(deSvg.codeOnly).toBe("MC zeigt SVG-Bilder nur als Code – eine SVG-Datei kann die App einfrieren.");
    expect(deSvg.downloadToView).toBe("Zum Ansehen herunterladen.");
    expect(enSvg.shortened).toBe(SHORTENED);
    expect(deSvg.shortened).toBe("Lange Datei – nur der Anfang wird gezeigt.");
  });
});
