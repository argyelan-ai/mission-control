import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { FilePreview } from "../FilePreview";

// FilePreview picks the renderer by the file NAME, but the bytes come with the
// type the SERVER chose — and the deliverable endpoint types a file by the
// target of a symlink. So "chart.png" can arrive as image/svg+xml (a symlink
// to an SVG), and an <img> of that blob draws the SVG: the freeze is back,
// one rename away. The same for "report.pdf" arriving as text/html: the PDF
// frame would show an HTML page from a blob: URL, i.e. with MC's own origin.
// So FilePreview gives the bytes the type their name stands for. A raster
// type makes the browser refuse SVG bytes (load error, nothing drawn), and a
// PDF type sends the frame to the PDF viewer, never to the HTML parser.

const URL_FILE = "/api/v1/boards/b/tasks/t/deliverables/d/file";
const BOMB = `<svg xmlns="http://www.w3.org/2000/svg"><defs><path id="l0" d="M0 0h1"/></defs><use href="#l0"/></svg>`;

describe("FilePreview — bytes get the type of the file name, not the server's", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn> | undefined;
  let minted: Blob[];
  const origCreate = URL.createObjectURL;
  const origRevoke = URL.revokeObjectURL;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: { getItem: () => "tok", setItem: () => undefined, removeItem: () => undefined, clear: () => undefined },
      configurable: true, writable: true,
    });
    minted = [];
    URL.createObjectURL = vi.fn((b: Blob) => { minted.push(b); return `blob:mc/${minted.length}`; }) as unknown as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn();
  });
  afterEach(() => {
    fetchSpy?.mockRestore();
    fetchSpy = undefined;
    URL.createObjectURL = origCreate;
    URL.revokeObjectURL = origRevoke;
  });

  function serve(body: string, type: string) {
    fetchSpy = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(body, { status: 200, headers: { "content-type": type } }),
    );
  }

  const shownImg = () => waitFor(() => {
    const el = document.querySelector("img");
    expect(el).not.toBeNull();
    return el as HTMLImageElement;
  });

  it("draws a .png that the server sends as image/svg+xml (a symlink) only as image/png", async () => {
    serve(BOMB, "image/svg+xml");
    render(<FilePreview fileUrl={URL_FILE} path="report/chart.png" />);

    const img = await shownImg();
    expect(minted).toHaveLength(1);
    expect(minted[0].type).toBe("image/png");
    expect(img.getAttribute("src")).toBe("blob:mc/1");
  });

  it.each([
    ["pic.png", "image/png"],
    ["photo.JPG", "image/jpeg"],
    ["photo.jpeg", "image/jpeg"],
    ["anim.gif", "image/gif"],
    ["shot.webp", "image/webp"],
    ["scan.bmp", "image/bmp"],
    ["favicon.ico", "image/x-icon"],
  ])("gives %s the raster type %s, whatever the server says", async (path, type) => {
    serve("x", "image/svg+xml; charset=utf-8");
    render(<FilePreview fileUrl={URL_FILE} path={path} />);

    await shownImg();
    expect(minted.map((b) => b.type)).toEqual([type]);
  });

  it("opens a .pdf that the server sends as text/html only as application/pdf", async () => {
    serve("<script>parent.document.title='x'</script>", "text/html");
    render(<FilePreview fileUrl={URL_FILE} path="report.pdf" />);

    await waitFor(() => expect(document.querySelector("iframe")).not.toBeNull());
    expect(minted.map((b) => b.type)).toEqual(["application/pdf"]);
  });

  it("shows the error state, not a broken picture, when the bytes are no picture", async () => {
    serve(BOMB, "image/svg+xml");
    render(<FilePreview fileUrl={URL_FILE} path="report/chart.png" />);

    const img = await shownImg();
    fireEvent.error(img);
    expect(await screen.findByText("Failed to load file")).toBeInTheDocument();
    expect(document.querySelector("img")).toBeNull();
  });
});
