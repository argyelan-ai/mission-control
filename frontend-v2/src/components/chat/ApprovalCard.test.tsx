/**
 * ApprovalCard — Task B3 vitest.
 *
 * Coverage:
 *   1. renders question + one button per option
 *   2. clicking an option calls onAnswer with that option's key
 *   3. buttons disable after a click (single-shot) until a new prompt object
 *      (fresh state event) arrives, which re-enables them
 *   4. "Im Terminal prüfen" link calls onShowTerminal
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent } from "@testing-library/react";
import { ApprovalCard } from "./ApprovalCard";
import type { ChatPrompt } from "@/lib/chatTypes";

const PROMPT: ChatPrompt = {
  question: "Datei löschen erlauben?",
  options: [
    { key: "y", label: "Ja" },
    { key: "n", label: "Nein" },
  ],
};

describe("ApprovalCard", () => {
  it("renders the question and one button per option", () => {
    render(
      <ApprovalCard prompt={PROMPT} onAnswer={vi.fn()} onShowTerminal={vi.fn()} />,
    );
    expect(screen.getByText("Datei löschen erlauben?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ja" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Nein" })).toBeInTheDocument();
  });

  it("calls onAnswer with the clicked option's key", () => {
    const onAnswer = vi.fn();
    render(
      <ApprovalCard prompt={PROMPT} onAnswer={onAnswer} onShowTerminal={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Ja" }));
    expect(onAnswer).toHaveBeenCalledWith("y");
    expect(onAnswer).toHaveBeenCalledTimes(1);
  });

  it("disables all option buttons after a click (single-shot)", () => {
    render(
      <ApprovalCard prompt={PROMPT} onAnswer={vi.fn()} onShowTerminal={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Ja" }));
    expect(screen.getByRole("button", { name: "Ja" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Nein" })).toBeDisabled();
  });

  it("re-enables buttons once a new prompt (new state event) arrives", () => {
    const { rerender } = render(
      <ApprovalCard prompt={PROMPT} onAnswer={vi.fn()} onShowTerminal={vi.fn()} />,
    );
    fireEvent.click(screen.getByRole("button", { name: "Ja" }));
    expect(screen.getByRole("button", { name: "Ja" })).toBeDisabled();

    const NEXT_PROMPT: ChatPrompt = {
      question: "Noch eine Datei löschen?",
      options: [
        { key: "y", label: "Ja" },
        { key: "n", label: "Nein" },
      ],
    };
    rerender(
      <ApprovalCard prompt={NEXT_PROMPT} onAnswer={vi.fn()} onShowTerminal={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Ja" })).not.toBeDisabled();
  });

  it("calls onShowTerminal when the quiet link is clicked", () => {
    const onShowTerminal = vi.fn();
    render(
      <ApprovalCard prompt={PROMPT} onAnswer={vi.fn()} onShowTerminal={onShowTerminal} />,
    );
    fireEvent.click(screen.getByText("Im Terminal prüfen"));
    expect(onShowTerminal).toHaveBeenCalledTimes(1);
  });
});
