/** Plan-mode chip (chat package P3). */
import { beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";

import { api } from "@/lib/api";
import type { ChatCapabilities, ModeEvent } from "@/lib/chatTypes";
import { ModeChip } from "./ModeChip";

vi.mock("@/lib/api", () => ({ api: { chat: { setMode: vi.fn() } } }));
vi.mock("@/lib/notify", () => ({
  notify: { error: vi.fn(), success: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));
import { notify } from "@/lib/notify";

const mockSetMode = vi.mocked(api.chat.setMode);

const CAPS = {
  effortLevels: [],
  canSwitchEffort: false,
  modeOptions: [{ value: "default", name: "Default" }, { value: "plan", name: "Plan" }],
  mode: "default",
} as ChatCapabilities;

const modeEvent = (modeId: string, uuid = "acp00020"): ModeEvent => ({
  kind: "mode", uuid, ts: "2026-10-13T10:00:00Z", modeId,
});

beforeEach(() => {
  mockSetMode.mockReset();
  mockSetMode.mockResolvedValue(undefined);
});

describe("ModeChip", () => {
  it("is absent unless the agent reports a plan mode", () => {
    const { container, rerender } = render(<ModeChip agentId="a1" capabilities={null} liveMode={null} />);
    expect(container).toBeEmptyDOMElement();
    rerender(
      <ModeChip
        agentId="a1"
        capabilities={{ ...CAPS, modeOptions: [] }}
        liveMode={null}
      />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it("switches to plan and shows it", async () => {
    render(<ModeChip agentId="a1" capabilities={CAPS} liveMode={null} />);
    const chip = screen.getByTestId("mode-chip");
    expect(chip).toHaveAttribute("data-mode", "default");
    expect(chip).toHaveAttribute("aria-pressed", "false");
    fireEvent.click(chip);
    await waitFor(() => expect(mockSetMode).toHaveBeenCalledWith("a1", "plan"));
    await waitFor(() => expect(screen.getByTestId("mode-chip")).toHaveAttribute("data-mode", "plan"));
  });

  it("follows the transcript: omp leaves plan mode after the approval", () => {
    const { rerender } = render(<ModeChip agentId="a1" capabilities={CAPS} liveMode={modeEvent("plan")} />);
    expect(screen.getByTestId("mode-chip")).toHaveAttribute("data-mode", "plan");
    rerender(<ModeChip agentId="a1" capabilities={CAPS} liveMode={modeEvent("default", "acp00021")} />);
    expect(screen.getByTestId("mode-chip")).toHaveAttribute("data-mode", "default");
  });

  it("names plan mode on the phone too; standard stays icon-only there", () => {
    const { rerender } = render(<ModeChip agentId="a1" capabilities={CAPS} liveMode={modeEvent("plan")} />);
    expect(screen.getByTestId("mode-chip-label")).toHaveTextContent("Plan");
    expect(screen.getByTestId("mode-chip-label").className).not.toContain("hidden");
    rerender(<ModeChip agentId="a1" capabilities={CAPS} liveMode={modeEvent("default", "acp00021")} />);
    expect(screen.getByTestId("mode-chip-label").className).toContain("hidden sm:inline");
  });

  it("a refusal while a question is open is a calm hint, and the chip stays", async () => {
    mockSetMode.mockRejectedValue(new Error('API 409: {"reason":"question_open"}'));
    render(<ModeChip agentId="a1" capabilities={CAPS} liveMode={modeEvent("plan")} />);
    fireEvent.click(screen.getByTestId("mode-chip"));
    await waitFor(() => expect(notify.info).toHaveBeenCalledWith(
      "Answer the open question first, then switch the mode."));
    expect(notify.error).not.toHaveBeenCalled();
    expect(screen.getByTestId("mode-chip")).toHaveAttribute("data-mode", "plan");
  });

  it("waits while the agent works", () => {
    render(<ModeChip agentId="a1" capabilities={CAPS} liveMode={null} busy />);
    expect(screen.getByTestId("mode-chip")).toBeDisabled();
  });
});
