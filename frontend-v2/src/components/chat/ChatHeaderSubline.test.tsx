/**
 * The muted line under the chat title (variant C, 2026-11-18):
 * "Working for 3 min" / "Ready" — one line, honest, nothing shown that is not
 * known. The model is named once, in the speed line (operator 18.11.2026).
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { act, render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";
import { ChatHeaderSubline, isTurnRunning, sublineKind } from "./ChatHeaderSubline";
import type { StateEvent } from "@/lib/chatTypes";

const st = (status: StateEvent["status"]): StateEvent => ({ kind: "state", status, prompt: null });

function renderLine(props: Partial<React.ComponentProps<typeof ChatHeaderSubline>> = {}, locale: "en" | "de" = "en") {
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : de}>
      <ChatHeaderSubline state={null} aliveness="idle" turnStartedAt={null} {...props} />
    </NextIntlClientProvider>,
  );
}

afterEach(() => vi.useRealTimers());

describe("sublineKind", () => {
  it("maps the status line's states onto four words", () => {
    expect(sublineKind(st("working"), "active")).toBe("working");
    expect(sublineKind(st("task_working"), "active")).toBe("working");
    expect(sublineKind(st("permission_prompt"), "active")).toBe("waiting");
    expect(sublineKind(st("idle"), "idle")).toBe("ready");
    expect(sublineKind(st("idle"), "ended")).toBe("ended");
    expect(sublineKind(null, "idle")).toBeNull();
  });

  it("a message on its way counts as working, whatever the stale probe says", () => {
    expect(sublineKind(st("idle"), "idle", true)).toBe("working");
  });
});

describe("ChatHeaderSubline", () => {
  it("Ready when the chat is idle — and no model id (it stands in the speed line)", () => {
    renderLine({ state: st("idle") });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Ready$/);
  });

  it("counts the running turn from the operator's last message, from one minute on", () => {
    vi.useFakeTimers({ now: new Date("2026-11-18T12:03:20Z") });
    renderLine({ state: st("working"), aliveness: "active", turnStartedAt: "2026-11-18T12:00:00Z" });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Working for 3 min$/);
  });

  it("right after a send it never counts from the previous turn (user test 21.11.: 'Working for 23 h')", () => {
    vi.useFakeTimers({ now: new Date("2026-11-21T12:00:00Z") });
    renderLine({ state: st("idle"), aliveness: "active", turnStartedAt: "2026-11-20T13:00:00Z", sending: true });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Working$/);
  });

  it("counts from the local send until the transcript shows the new message", () => {
    vi.useFakeTimers({ now: new Date("2026-11-21T12:00:00Z") });
    const old = "2026-11-20T13:00:00Z";
    const wrap = (props: Partial<React.ComponentProps<typeof ChatHeaderSubline>>) => (
      <NextIntlClientProvider locale="en" messages={en}>
        <ChatHeaderSubline state={st("working")} aliveness="active" turnStartedAt={old} {...props} />
      </NextIntlClientProvider>
    );
    const { rerender } = render(wrap({ sending: true }));
    // The send flag clears on the first sign of the turn; the transcript may
    // still lack the operator's new message — the old one must not date it.
    rerender(wrap({ sending: false }));
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Working$/);
    act(() => {
      vi.advanceTimersByTime(125_000);
    });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Working for 2 min$/);
    // Once the new message is in the transcript, it is the start again.
    rerender(wrap({ sending: false, turnStartedAt: "2026-11-21T12:01:00Z" }));
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Working for 1 min$/);
  });

  it("says only Working under a minute (K17: no seconds)", () => {
    vi.useFakeTimers({ now: new Date("2026-11-18T12:00:30Z") });
    renderLine({ state: st("working"), aliveness: "active", turnStartedAt: "2026-11-18T12:00:00Z" });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^Working$/);
  });

  it("has its German words in the catalogue (EN + DE, K10)", () => {
    expect(de.cockpit.header.workingFor).toBe("Arbeitet seit {span}");
    expect(de.cockpit.header.ready).toBe("Bereit");
    expect(Object.keys(de.cockpit.header).sort()).toEqual(Object.keys(en.cockpit.header).sort());
  });

  it("an ended session adds no line — the header badge says it", () => {
    renderLine({ state: st("idle"), aliveness: "ended" });
    expect(screen.queryByTestId("chat-header-subline")).toBeNull();
  });

  it("one predicate for a running turn: the speed line's live number agrees with 'Working'", () => {
    expect(isTurnRunning(st("waiting_model"))).toBe(true);
    expect(isTurnRunning(st("task_working"))).toBe(true);
    expect(isTurnRunning(st("idle"), true)).toBe(true);
    expect(isTurnRunning(st("idle"))).toBe(false);
    expect(isTurnRunning(st("permission_prompt"))).toBe(false);
  });

  it("nothing known → no line at all (K3)", () => {
    renderLine();
    expect(screen.queryByTestId("chat-header-subline")).toBeNull();
  });
});
