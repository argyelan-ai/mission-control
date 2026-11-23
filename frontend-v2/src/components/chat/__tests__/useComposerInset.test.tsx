/**
 * useComposerInset — the overlaid side panel stops above the composer pill.
 * The pill can mount after the panel opened (chat still loading) or be
 * replaced by a new node; the inset must follow, not stay 0 or stale.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { useComposerInset } from "../useComposerInset";

function rect(top: number, bottom: number): DOMRect {
  return { top, bottom, left: 0, right: 100, width: 100, height: bottom - top, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

function Probe() {
  const inset = useComposerInset(true);
  return <span data-testid="inset">{inset}</span>;
}

let island: HTMLElement;

beforeEach(() => {
  // jsdom has no ResizeObserver; a no-op one keeps the hook on its real path.
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  island = document.createElement("div");
  island.setAttribute("data-testid", "chat-island");
  island.getBoundingClientRect = () => rect(0, 800);
  document.body.appendChild(island);
});

afterEach(() => {
  island.remove();
  vi.unstubAllGlobals();
});

function addPill(top: number): HTMLElement {
  const pill = document.createElement("div");
  pill.setAttribute("data-testid", "composer-dropzone");
  pill.getBoundingClientRect = () => rect(top, 790);
  island.appendChild(pill);
  return pill;
}

describe("useComposerInset", () => {
  it("measures a composer that is already there", () => {
    addPill(700);
    render(<Probe />);
    expect(screen.getByTestId("inset")).toHaveTextContent("108"); // 800 - 700 + 8
  });

  it("picks up a composer that mounts after the panel opened", async () => {
    render(<Probe />);
    expect(screen.getByTestId("inset")).toHaveTextContent("0");
    await act(async () => {
      addPill(650);
      await Promise.resolve();
    });
    expect(screen.getByTestId("inset")).toHaveTextContent("158");
  });

  it("follows a composer that is replaced by a new node", async () => {
    const first = addPill(700);
    render(<Probe />);
    expect(screen.getByTestId("inset")).toHaveTextContent("108");
    await act(async () => {
      first.remove();
      addPill(600);
      await Promise.resolve();
    });
    expect(screen.getByTestId("inset")).toHaveTextContent("208");
  });
});
