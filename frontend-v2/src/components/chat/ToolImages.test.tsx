import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { ToolImages } from "./ToolImages";
import { ChatMediaContext } from "./chatMedia";
import type { ToolImage } from "@/lib/chatTypes";

/** A fetch answer without undici: a real Response around a jsdom Blob is
 *  read differently per Node version (Node 22 in CI never resolved the
 *  blob). An explicit object gives the same result on every runtime. */
function fakeResponse(ok: boolean, type = "image/png") {
  return { ok, status: ok ? 200 : 404, blob: async () => new Blob(["x"], { type }) } as unknown as Response;
}

const AGENT = "11111111-2222-3333-4444-555555555555";

function img(n: number, over: Partial<ToolImage> = {}): ToolImage {
  return { id: String(n).repeat(64).slice(0, 64), mime: "image/png", width: 1280, height: 800, bytes: 1000, ...over };
}

function withAgent(ui: React.ReactNode, agentId: string | null = AGENT) {
  return <ChatMediaContext.Provider value={agentId}>{ui}</ChatMediaContext.Provider>;
}

let fetchMock: ReturnType<typeof vi.fn>;

beforeEach(() => {
  fetchMock = vi.fn(async () => fakeResponse(true, "image/webp"));
  vi.stubGlobal("fetch", fetchMock);
  let n = 0;
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: () => `blob:mock-${++n}`, revokeObjectURL: () => {} }));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("ToolImages", () => {
  it("shows nothing outside an agent chat (no address to guess)", () => {
    const { container } = render(withAgent(<ToolImages images={[img(1)]} />, null));
    expect(container).toBeEmptyDOMElement();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("loads the THUMB of each picture from this agent's media route with the Bearer header", async () => {
    render(withAgent(<ToolImages images={[img(1)]} />));
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    const [url, init] = fetchMock.mock.calls[0];
    expect(String(url)).toContain(`/api/v1/agents/${AGENT}/chat/media/${img(1).id}?variant=thumb`);
    expect((init as RequestInit).headers).toMatchObject({ Authorization: expect.stringMatching(/^Bearer /) });
    await waitFor(() => expect(screen.getByTestId("tool-image").querySelector("img")).toBeTruthy());
  });

  it("folds more than four pictures into a +N tile that opens the viewer there", async () => {
    const images = [1, 2, 3, 4, 5, 6].map((n) => img(n));
    render(withAgent(<ToolImages images={images} />));
    const tiles = screen.getAllByTestId("tool-image");
    expect(tiles).toHaveLength(4);
    expect(tiles[3]).toHaveAccessibleName("Open 3 more pictures");
    fireEvent.click(tiles[3]);
    expect(await screen.findByTestId("image-viewer")).toBeInTheDocument();
    expect(screen.getByText("4 / 6")).toBeInTheDocument();
  });

  it("tap opens the full-screen viewer with the FULL picture; Escape closes it", async () => {
    render(withAgent(<ToolImages images={[img(7)]} />));
    fireEvent.click(screen.getByRole("button", { name: "Open picture 1 of 1" }));
    const viewer = await screen.findByRole("dialog", { name: "Picture viewer" });
    expect(viewer).toBeInTheDocument();
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).endsWith(`${img(7).id}?variant=full`))).toBe(true),
    );
    await waitFor(() => expect(screen.getByTestId("image-viewer-img")).toHaveAttribute("data-full", "true"));
    expect(screen.getByRole("button", { name: "Close" })).toHaveFocus();
    act(() => {
      fireEvent.keyDown(document, { key: "Escape" });
    });
    expect(screen.queryByTestId("image-viewer")).toBeNull();
  });

  it("names a picture that cannot be loaded instead of an empty frame", async () => {
    fetchMock.mockImplementation(async () => fakeResponse(false));
    render(withAgent(<ToolImages images={[img(8)]} />));
    expect(await screen.findByText("Picture not available")).toBeInTheDocument();
  });
});
