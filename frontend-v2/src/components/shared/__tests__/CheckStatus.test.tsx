import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CheckStatus } from "../CheckStatus";
import { ageBucket } from "@/lib/configChecks";
import type { ConfigCheckItem } from "@/lib/types";

function item(over: Partial<ConfigCheckItem> = {}): ConfigCheckItem {
  return {
    kind: "secret",
    subject_id: "openai_api_key",
    label: "OpenAI",
    secret_key: "openai_api_key",
    configured: true,
    testable: true,
    cost: "free",
    state: "never_tested",
    reason: null,
    reason_params: {},
    message: null,
    duration_ms: null,
    checked_at: null,
    age_seconds: null,
    value_changed: false,
    ...over,
  };
}

const tested = (state: "ok" | "fail", age: number, extra: Partial<ConfigCheckItem> = {}) =>
  item({
    state,
    age_seconds: age,
    checked_at: new Date(Date.now() - age * 1000).toISOString(),
    received_at_ms: Date.now(),
    ...extra,
  });

describe("<CheckStatus>", () => {
  it("green: says it works and how old the result is", () => {
    render(<CheckStatus item={tested("ok", 180)} />);
    const box = screen.getByTestId("check-status");
    expect(box).toHaveAttribute("data-state", "ok");
    expect(box).toHaveTextContent("Works");
    expect(box).toHaveTextContent("tested 3 min ago");
  });

  it("red: says it failed, with the age and the reason on its own line", () => {
    render(
      <CheckStatus
        item={tested("fail", 10, {
          reason: "rejected",
          reason_params: { status: 401 },
          message: "OpenAI rejected the key (HTTP 401)",
        })}
      />,
    );
    const box = screen.getByTestId("check-status");
    expect(box).toHaveAttribute("data-state", "fail");
    expect(box).toHaveTextContent("Failed");
    expect(box).toHaveTextContent("tested just now");
    // Worded from the reason code; the server's sentence is not shown for a known reason.
    expect(screen.getByTestId("check-message")).toHaveTextContent(
      "Rejected (HTTP 401) — the key is expired, revoked or mistyped.",
    );
    expect(screen.getByTestId("check-status")).not.toHaveTextContent("OpenAI rejected the key");
  });

  it("never tested: says so and shows no age", () => {
    render(<CheckStatus item={item()} />);
    expect(screen.getByTestId("check-status")).toHaveTextContent("Never tested");
    expect(screen.queryByTestId("check-age")).not.toBeInTheDocument();
  });

  it("a value replaced since the last test is not shown with the old result", () => {
    render(<CheckStatus item={item({ value_changed: true })} />);
    expect(screen.getByTestId("check-status")).toHaveTextContent("Changed — not tested yet");
  });

  it("no procedure: 'can't be tested yet' — never green, no Test button", () => {
    const onTest = vi.fn();
    render(<CheckStatus item={item({ testable: false })} onTest={onTest} />);
    const box = screen.getByTestId("check-status");
    expect(box).toHaveAttribute("data-state", "notTestable");
    expect(box).toHaveTextContent("Can't be tested yet");
    expect(screen.queryByTestId("check-test")).not.toBeInTheDocument();
  });

  it("Test button runs the check; hidden while it runs", async () => {
    const onTest = vi.fn();
    const { rerender } = render(<CheckStatus item={item()} onTest={onTest} />);
    await userEvent.click(screen.getByRole("button", { name: "Test" }));
    expect(onTest).toHaveBeenCalledTimes(1);

    rerender(<CheckStatus item={item()} onTest={onTest} running />);
    expect(screen.getByTestId("check-status")).toHaveTextContent("Testing…");
    expect(screen.queryByRole("button", { name: "Test" })).not.toBeInTheDocument();
  });

  it("a check that spends quota says so on the Test button (E8a)", () => {
    render(<CheckStatus item={item({ cost: "quota" })} onTest={() => {}} />);
    expect(screen.getByRole("button", { name: "Test" })).toHaveAttribute(
      "title",
      expect.stringContaining("quota"),
    );
  });

  it("age variant: only the age, for cards that already name the state", () => {
    render(<CheckStatus item={tested("ok", 7200)} variant="age" />);
    const box = screen.getByTestId("check-status");
    expect(box).toHaveTextContent("tested 2 hours ago");
    expect(box).not.toHaveTextContent("Works");
  });

  it("age keeps counting after the item arrived", () => {
    const arrived = item({
      state: "ok",
      age_seconds: 30,
      checked_at: new Date().toISOString(),
      received_at_ms: Date.now() - 5 * 60_000,
    });
    render(<CheckStatus item={arrived} />);
    expect(screen.getByTestId("check-status")).toHaveTextContent("tested 5 min ago");
  });
});

describe("ageBucket", () => {
  it("uses fixed forms: just now, minutes, hours, days", () => {
    expect(ageBucket(0)).toEqual({ key: "justNow", n: 0 });
    expect(ageBucket(59)).toEqual({ key: "justNow", n: 0 });
    expect(ageBucket(60)).toEqual({ key: "minutes", n: 1 });
    expect(ageBucket(3599)).toEqual({ key: "minutes", n: 59 });
    expect(ageBucket(3600)).toEqual({ key: "hours", n: 1 });
    expect(ageBucket(86400 * 3)).toEqual({ key: "days", n: 3 });
  });
});
