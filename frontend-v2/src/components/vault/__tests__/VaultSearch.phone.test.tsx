/**
 * Round-4 user test t4 NICE-1: on the phone the memory search field still
 * showed the keyboard hints "/" (left) and "⏎" (right) — a phone has no such
 * keys — and their padding cut the placeholder short. The hints are a
 * desktop thing (md and up); below md the field gets its width back.
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { VaultSearch } from "../VaultSearch";

describe("VaultSearch — keyboard hints are desktop-only", () => {
  it("hides '/' and '⏎' below md and frees the padding", () => {
    render(<VaultSearch value="" onChange={() => {}} />);
    for (const hint of ["/", "⏎"]) {
      const el = screen.getByText(hint);
      expect(el).toHaveAttribute("aria-hidden", "true");
      expect(el.className).toMatch(/(^|\s)hidden(\s|$)/);
      expect(el.className).toMatch(/md:block/);
    }
    const input = screen.getByRole("textbox", { name: "Search vault notes" });
    expect(input.className).toMatch(/(^|\s)px-4(\s|$)/);
    expect(input.className).toMatch(/md:pl-9/);
    expect(input.className).toMatch(/md:pr-16/);
  });
});
