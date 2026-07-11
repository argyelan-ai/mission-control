import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { XPostApprovalCard, mediaPathToFilesLocation } from "../XPostApprovalCard";
import type { Approval } from "@/lib/types";

const mkApproval = (payload: Record<string, unknown>, overrides: Partial<Approval> = {}): Approval =>
  ({
    id: "a1",
    board_id: "b1",
    agent_id: "agent-1",
    action_type: "x_post",
    description: "Rex requests to post to X",
    status: "pending",
    created_at: new Date().toISOString(),
    resolved_at: null,
    resolver_note: null,
    failure_reason: null,
    expires_at: null,
    confidence: null,
    autonomy_level: null,
    task_id: null,
    payload,
    ...overrides,
  }) as Approval;

describe("mediaPathToFilesLocation", () => {
  it("maps sidecar volume paths onto the shared-deliverables root", () => {
    expect(mediaPathToFilesLocation("/shared-deliverables/bench-1/grid.mp4")).toEqual({
      root: "shared-deliverables",
      subpath: "bench-1/grid.mp4",
    });
  });

  it("maps host ~/.mc/<root>/ paths onto the matching files root", () => {
    expect(
      mediaPathToFilesLocation("/Users/Henry/.mc/deliverables/bench-1/shot.png"),
    ).toEqual({ root: "deliverables", subpath: "bench-1/shot.png" });
    expect(mediaPathToFilesLocation("/Users/Henry/.mc/media/clip.mp4")).toEqual({
      root: "media",
      subpath: "clip.mp4",
    });
  });

  it("returns null for paths outside every browsable root", () => {
    expect(mediaPathToFilesLocation("/etc/passwd")).toBeNull();
    expect(mediaPathToFilesLocation("/Users/Henry/.mc/secrets/token.json")).toBeNull();
  });
});

describe("XPostApprovalCard", () => {
  let fetchSpy: ReturnType<typeof vi.spyOn> | undefined;

  beforeEach(() => {
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: () => "tok",
        setItem: () => undefined,
        removeItem: () => undefined,
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });
  });

  afterEach(() => {
    fetchSpy?.mockRestore();
    fetchSpy = undefined;
  });

  it("renders tweet text and n/280 char counter (text-only v1 draft, no fetch)", () => {
    fetchSpy = vi.spyOn(globalThis, "fetch");
    const text = "One-shot: local DeepSeek vs Claude on a spinning cube.";
    render(<XPostApprovalCard approval={mkApproval({ text })} onResolve={vi.fn()} />);

    expect(screen.getByText(text)).toBeInTheDocument();
    expect(screen.getByText(`${text.length}/280`)).toBeInTheDocument();
    // graceful fallback: no media_paths → no media section, no file fetches
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("marks the counter as over-limit above 280 chars", () => {
    const text = "x".repeat(300);
    render(<XPostApprovalCard approval={mkApproval({ text })} onResolve={vi.fn()} />);
    const counter = screen.getByText("300/280");
    expect(counter).toHaveAttribute("data-over", "true");
  });

  it("renders a <video> player for an .mp4 media path", async () => {
    // String body — jsdom Blob interop pitfall documented in FilePreview.test.tsx
    fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("x", { status: 200 }));

    const { container } = render(
      <XPostApprovalCard
        approval={mkApproval({
          text: "Grid video",
          media_paths: ["/shared-deliverables/bench-1/grid.mp4"],
        })}
        onResolve={vi.fn()}
      />,
    );

    await waitFor(() => expect(container.querySelector("video")).not.toBeNull());
    // FilePreview fetched via the Files content API
    const calledUrl = String(fetchSpy.mock.calls[0][0]);
    expect(calledUrl).toContain("/api/v1/files/content?");
    expect(calledUrl).toContain("root=shared-deliverables");
  });

  it("renders image thumbnails for image paths", async () => {
    // Use mockImplementation (not mockResolvedValue) so each call gets a fresh
    // Response body — the same Response instance can only be .blob()-ed once.
    fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockImplementation(() => Promise.resolve(new Response("x", { status: 200 })));

    const { container } = render(
      <XPostApprovalCard
        approval={mkApproval({
          text: "Screens",
          media_paths: [
            "/Users/Henry/.mc/deliverables/bench-1/a.png",
            "/Users/Henry/.mc/deliverables/bench-1/b.png",
          ],
        })}
        onResolve={vi.fn()}
      />,
    );

    await waitFor(() => expect(container.querySelectorAll("img").length).toBe(2));
  });

  it("shows the raw path as fallback when a media path is not resolvable", () => {
    render(
      <XPostApprovalCard
        approval={mkApproval({ text: "t", media_paths: ["/opt/elsewhere/clip.mp4"] })}
        onResolve={vi.fn()}
      />,
    );
    expect(screen.getByText("/opt/elsewhere/clip.mp4")).toBeInTheDocument();
  });

  it("wires Approve/Reject to onResolve", () => {
    const onResolve = vi.fn();
    render(<XPostApprovalCard approval={mkApproval({ text: "t" })} onResolve={onResolve} />);

    fireEvent.click(screen.getByRole("button", { name: /approve & post/i }));
    expect(onResolve).toHaveBeenCalledWith("approved");

    fireEvent.click(screen.getByRole("button", { name: /reject/i }));
    expect(onResolve).toHaveBeenCalledWith("rejected");
  });
});
