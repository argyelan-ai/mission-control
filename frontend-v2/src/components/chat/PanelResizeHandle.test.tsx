/**
 * PanelResizeHandle — the drag handle on the side panel's left edge
 * (ADR-099). W3C APG "window splitter": role=separator with value/min/max,
 * arrow keys move it, double-click resets.
 */
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { PanelResizeHandle } from "./PanelResizeHandle";

const setup = (props: Partial<React.ComponentProps<typeof PanelResizeHandle>> = {}) => {
  const onResize = vi.fn();
  const onCommit = vi.fn();
  const onReset = vi.fn();
  render(
    <PanelResizeHandle
      width={500}
      islandWidth={1400}
      onResize={onResize}
      onCommit={onCommit}
      onReset={onReset}
      {...props}
    />
  );
  return { onResize, onCommit, onReset, handle: screen.getByRole("separator") };
};

describe("PanelResizeHandle", () => {
  it("is a focusable vertical separator that states its value and limits", () => {
    const { handle } = setup();
    expect(handle).toHaveAttribute("aria-orientation", "vertical");
    expect(handle).toHaveAttribute("aria-valuenow", "500");
    expect(handle).toHaveAttribute("aria-valuemin", "320");
    // 70 % of 1400 = 980; 1400 − 400 = 1000 → 980.
    expect(handle).toHaveAttribute("aria-valuemax", "980");
    expect(handle).toHaveAttribute("tabindex", "0");
    expect(handle).toHaveAccessibleName("Resize panel");
  });

  it("arrow left makes the panel wider, arrow right narrower — 16 px a step, kept and remembered", () => {
    const { handle, onResize, onCommit } = setup();
    fireEvent.keyDown(handle, { key: "ArrowLeft" });
    expect(onResize).toHaveBeenLastCalledWith(516);
    expect(onCommit).toHaveBeenLastCalledWith(516);
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(onResize).toHaveBeenLastCalledWith(484);
  });

  it("Home / End jump to the limits", () => {
    const { handle, onResize } = setup();
    fireEvent.keyDown(handle, { key: "Home" });
    expect(onResize).toHaveBeenLastCalledWith(320);
    fireEvent.keyDown(handle, { key: "End" });
    expect(onResize).toHaveBeenLastCalledWith(980);
  });

  it("does not step past a limit", () => {
    const { handle, onResize } = setup({ width: 320 });
    fireEvent.keyDown(handle, { key: "ArrowRight" });
    expect(onResize).toHaveBeenLastCalledWith(320);
  });

  it("dragging left by 100 px widens the panel by 100 px, and the width is remembered on release", () => {
    const { handle, onResize, onCommit } = setup();
    fireEvent.pointerDown(handle, { clientX: 900, pointerId: 1, button: 0 });
    fireEvent.pointerMove(handle, { clientX: 800, pointerId: 1 });
    expect(onResize).toHaveBeenLastCalledWith(600);
    expect(onCommit).not.toHaveBeenCalled();
    fireEvent.pointerUp(handle, { clientX: 800, pointerId: 1 });
    expect(onCommit).toHaveBeenLastCalledWith(600);
  });

  it("dragging is clamped to the limits", () => {
    const { handle, onResize } = setup();
    fireEvent.pointerDown(handle, { clientX: 900, pointerId: 1, button: 0 });
    fireEvent.pointerMove(handle, { clientX: 0, pointerId: 1 });
    expect(onResize).toHaveBeenLastCalledWith(980);
    fireEvent.pointerMove(handle, { clientX: 1400, pointerId: 1 });
    expect(onResize).toHaveBeenLastCalledWith(320);
  });

  it("double-click resets to the default", () => {
    const { handle, onReset } = setup();
    fireEvent.doubleClick(handle);
    expect(onReset).toHaveBeenCalled();
  });

  it("is desktop-only", () => {
    const { handle } = setup();
    expect(handle.className).toContain("hidden");
    expect(handle.className).toContain("md:block");
  });
});
