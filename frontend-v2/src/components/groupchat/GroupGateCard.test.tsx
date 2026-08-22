/**
 * GroupGateCard — vitest.
 *
 * Prüft, dass die Frage sichtbar ist, beide Knöpfe genau ihren Callback
 * auslösen und `busy` wirklich sperrt (Doppelklick-Schutz — sonst gehen zwei
 * Runden-Entscheide raus).
 * Labels sind englisch, weil src/test-setup.ts next-intl gegen messages/en.json
 * auflöst.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GroupGateCard } from "./GroupGateCard";

describe("GroupGateCard", () => {
  it("shows the title, the question and both buttons", () => {
    render(
      <GroupGateCard question="Ship DFlash2 as default?" onApprove={vi.fn()} onReject={vi.fn()} />
    );
    expect(screen.getByText("The group is asking you")).toBeInTheDocument();
    expect(screen.getByText("Ship DFlash2 as default?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Keep paused" })).toBeInTheDocument();
  });

  it("keeps the multi-line question readable instead of collapsing it", () => {
    render(<GroupGateCard question={"Line one\nLine two"} onApprove={vi.fn()} onReject={vi.fn()} />);
    const question = screen.getByText(/Line one/);
    expect(question).toHaveClass("whitespace-pre-wrap");
    expect(question.textContent).toBe("Line one\nLine two");
  });

  it("points out that a free-text answer works too", () => {
    render(<GroupGateCard question="Go on?" onApprove={vi.fn()} onReject={vi.fn()} />);
    expect(
      screen.getByText("Or answer in the chat below — your message goes into the next round.")
    ).toBeInTheDocument();
  });

  it("calls onApprove only, when the approve button is clicked", async () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<GroupGateCard question="Go on?" onApprove={onApprove} onReject={onReject} />);

    await userEvent.click(screen.getByRole("button", { name: "Continue" }));

    expect(onApprove).toHaveBeenCalledTimes(1);
    expect(onReject).not.toHaveBeenCalled();
  });

  it("calls onReject only, when the reject button is clicked", async () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<GroupGateCard question="Go on?" onApprove={onApprove} onReject={onReject} />);

    await userEvent.click(screen.getByRole("button", { name: "Keep paused" }));

    expect(onReject).toHaveBeenCalledTimes(1);
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("disables both buttons while an answer is in flight", async () => {
    const onApprove = vi.fn();
    const onReject = vi.fn();
    render(<GroupGateCard question="Go on?" onApprove={onApprove} onReject={onReject} busy />);

    const approve = screen.getByRole("button", { name: "Continue" });
    const reject = screen.getByRole("button", { name: "Keep paused" });
    expect(approve).toBeDisabled();
    expect(reject).toBeDisabled();

    await userEvent.click(approve);
    await userEvent.click(reject);
    expect(onApprove).not.toHaveBeenCalled();
    expect(onReject).not.toHaveBeenCalled();
  });
});
