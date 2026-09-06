/**
 * SlideOverPanel — Body-Scroll-Lock (Review #440 Fund 4, 06.09.2026): keiner
 * der fünf Nutzer (BoxCockpit, RuntimeDetailPanel, LoopDetailPanel,
 * RepoDetailPanel, FilePreviewPanel) rief den bestehenden Hook
 * `useBodyScrollLock` auf — der Hintergrund konnte auf iOS unter einem
 * offenen Bottom-Sheet weiterscrollen. Der Hook sitzt jetzt zentral in
 * `SlideOverPanel` selbst, behebt es für alle fünf auf einen Schlag.
 */
import { describe, it, expect, afterEach } from "vitest";
import { render, cleanup } from "@testing-library/react";
import { SlideOverPanel } from "../SlideOverPanel";

afterEach(() => {
  cleanup();
  // Falls ein Test fehlschlägt, bevor unlock() lief — nicht in den nächsten
  // Test durchsickern lassen.
  document.body.style.position = "";
  document.body.style.overflow = "";
});

describe("SlideOverPanel — body scroll lock", () => {
  it("locks the body (position: fixed) while open", () => {
    render(
      <SlideOverPanel open onClose={() => {}} title="Panel">
        <div>content</div>
      </SlideOverPanel>
    );
    expect(document.body.style.position).toBe("fixed");
    expect(document.body.style.overflow).toBe("hidden");
  });

  it("unlocks the body when closed", () => {
    const { rerender } = render(
      <SlideOverPanel open onClose={() => {}} title="Panel">
        <div>content</div>
      </SlideOverPanel>
    );
    expect(document.body.style.position).toBe("fixed");

    rerender(
      <SlideOverPanel open={false} onClose={() => {}} title="Panel">
        <div>content</div>
      </SlideOverPanel>
    );
    expect(document.body.style.position).toBe("");
    expect(document.body.style.overflow).toBe("");
  });

  it("does not lock the body when never opened", () => {
    render(
      <SlideOverPanel open={false} onClose={() => {}} title="Panel">
        <div>content</div>
      </SlideOverPanel>
    );
    expect(document.body.style.position).toBe("");
  });
});
