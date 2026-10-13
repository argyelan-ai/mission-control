import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ImageViewer } from "./ImageViewer";
import type { ToolImage } from "@/lib/chatTypes";

/** A fetch answer without undici: a real Response around a jsdom Blob is
 *  read differently per Node version (Node 22 in CI never resolved the
 *  blob). An explicit object gives the same result on every runtime. */
function fakeResponse(ok: boolean, type = "image/png") {
  return { ok, status: ok ? 200 : 404, blob: async () => new Blob(["x"], { type }) } as unknown as Response;
}

const images: ToolImage[] = [1, 2, 3].map((n) => ({
  id: String(n).repeat(64),
  mime: n === 2 ? "image/jpeg" : "image/png",
  width: 800,
  height: 600,
  bytes: 10,
}));
const mediaUrl = (id: string, v: "thumb" | "full") => `/m/${id}?variant=${v}`;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(async () => fakeResponse(true)));
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => `blob:mock-${++n}`, revokeObjectURL: () => {} }));
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.body.style.position = "";
});

function pointer(el: Element, type: string, id: number, x: number, y: number) {
  const ev = new Event(type, { bubbles: true }) as Event & Record<string, number>;
  Object.assign(ev, { pointerId: id, clientX: x, clientY: y, button: 0 });
  act(() => {
    el.dispatchEvent(ev);
  });
}

describe("ImageViewer", () => {
  it("pages with the arrow keys and the buttons, wrapping around", () => {
    const onIndex = vi.fn();
    render(<ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={onIndex} onClose={() => {}} />);
    expect(screen.getByText("1 / 3")).toBeInTheDocument();
    fireEvent.keyDown(document, { key: "ArrowRight" });
    expect(onIndex).toHaveBeenLastCalledWith(1);
    fireEvent.keyDown(document, { key: "ArrowLeft" });
    expect(onIndex).toHaveBeenLastCalledWith(2);
    fireEvent.click(screen.getByRole("button", { name: "Next picture" }));
    expect(onIndex).toHaveBeenLastCalledWith(1);
  });

  it("one picture: no counter, no paging", () => {
    render(<ImageViewer images={images.slice(0, 1)} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={() => {}} />);
    expect(screen.queryByText("1 / 1")).toBeNull();
    expect(screen.queryByRole("button", { name: "Next picture" })).toBeNull();
  });

  it("dragging down closes; a short drag snaps back", async () => {
    const onClose = vi.fn();
    render(<ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={onClose} />);
    const stage = screen.getByTestId("image-viewer-stage");
    pointer(stage, "pointerdown", 1, 200, 300);
    pointer(stage, "pointermove", 1, 205, 340);
    pointer(stage, "pointerup", 1, 205, 340);
    expect(onClose).not.toHaveBeenCalled();
    pointer(stage, "pointerdown", 1, 200, 300);
    pointer(stage, "pointermove", 1, 200, 460);
    pointer(stage, "pointerup", 1, 200, 460);
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("a sideways swipe pages", () => {
    const onIndex = vi.fn();
    render(<ImageViewer images={images} index={1} mediaUrl={mediaUrl} onIndex={onIndex} onClose={() => {}} />);
    const stage = screen.getByTestId("image-viewer-stage");
    pointer(stage, "pointerdown", 1, 300, 300);
    pointer(stage, "pointermove", 1, 150, 310);
    pointer(stage, "pointerup", 1, 150, 310);
    expect(onIndex).toHaveBeenLastCalledWith(2);
  });

  it("a pinch zooms, and zoomed in a drag down does NOT close", async () => {
    const onClose = vi.fn();
    render(<ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={onClose} />);
    const stage = screen.getByTestId("image-viewer-stage");
    await waitFor(() => expect(screen.getByTestId("image-viewer-img")).toBeInTheDocument());
    pointer(stage, "pointerdown", 1, 100, 300);
    pointer(stage, "pointerdown", 2, 200, 300);
    pointer(stage, "pointermove", 2, 400, 300);
    pointer(stage, "pointerup", 2, 400, 300);
    pointer(stage, "pointerup", 1, 100, 300);
    // fingers 100 px apart -> 300 px apart = 3x
    expect(screen.getByTestId("image-viewer-img").style.transform).toContain("scale(3)");
    pointer(stage, "pointerdown", 1, 200, 300);
    pointer(stage, "pointermove", 1, 200, 600);
    pointer(stage, "pointerup", 1, 200, 600);
    expect(onClose).not.toHaveBeenCalled();
  });

  it("double tap zooms in and out", async () => {
    render(<ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={() => {}} />);
    const stage = screen.getByTestId("image-viewer-stage");
    await waitFor(() => expect(screen.getByTestId("image-viewer-img")).toBeInTheDocument());
    for (let i = 0; i < 2; i++) {
      pointer(stage, "pointerdown", 1, 200, 300);
      pointer(stage, "pointerup", 1, 200, 300);
    }
    expect(screen.getByTestId("image-viewer-img").style.transform).toContain("scale(2.5)");
    for (let i = 0; i < 2; i++) {
      pointer(stage, "pointerdown", 1, 200, 300);
      pointer(stage, "pointerup", 1, 200, 300);
    }
    expect(screen.getByTestId("image-viewer-img").style.transform).toContain("scale(1)");
  });

  it("download saves the full picture under a neutral name", async () => {
    const clicked: HTMLAnchorElement[] = [];
    const orig = HTMLAnchorElement.prototype.click;
    HTMLAnchorElement.prototype.click = function () {
      clicked.push(this);
    };
    try {
      render(<ImageViewer images={images} index={1} mediaUrl={mediaUrl} onIndex={() => {}} onClose={() => {}} />);
      await waitFor(() => expect(screen.getByTestId("image-viewer-img")).toHaveAttribute("data-full", "true"));
      fireEvent.click(screen.getByRole("button", { name: "Download" }));
      expect(clicked).toHaveLength(1);
      expect(clicked[0].download).toBe(`mc-image-${"2".repeat(12)}.jpg`);
      expect(clicked[0].href).toMatch(/^blob:/);
    } finally {
      HTMLAnchorElement.prototype.click = orig;
    }
  });

  it("copy is offered only where the clipboard takes pictures, and confirms", async () => {
    const { unmount } = render(
      <ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={() => {}} />,
    );
    expect(screen.queryByTestId("image-viewer-copy")).toBeNull();
    unmount();

    const write = vi.fn(async () => {});
    vi.stubGlobal("ClipboardItem", class {
      constructor(public items: Record<string, unknown>) {}
    });
    Object.defineProperty(navigator, "clipboard", { value: { write }, configurable: true });
    render(<ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={() => {}} />);
    await waitFor(() => expect(screen.getByTestId("image-viewer-img")).toHaveAttribute("data-full", "true"));
    fireEvent.click(screen.getByRole("button", { name: "Copy picture" }));
    await waitFor(() => expect(write).toHaveBeenCalledTimes(1));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeInTheDocument();
    Object.defineProperty(navigator, "clipboard", { value: undefined, configurable: true });
  });

  it("locks the page behind it", () => {
    const { unmount } = render(
      <ImageViewer images={images} index={0} mediaUrl={mediaUrl} onIndex={() => {}} onClose={() => {}} />,
    );
    expect(document.body.style.position).toBe("fixed");
    unmount();
    expect(document.body.style.position).toBe("");
  });
});
