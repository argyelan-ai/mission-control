/**
 * The muted line under the chat title (variant C, 2026-11-18):
 * "model · Working for 3 min" / "model · Ready" — one line, honest, nothing
 * shown that is not known.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { NextIntlClientProvider } from "next-intl";
import en from "../../../messages/en.json";
import de from "../../../messages/de.json";
import { ChatHeaderSubline, sublineKind } from "./ChatHeaderSubline";
import type { StateEvent } from "@/lib/chatTypes";

const st = (status: StateEvent["status"]): StateEvent => ({ kind: "state", status, prompt: null });

function renderLine(props: Partial<React.ComponentProps<typeof ChatHeaderSubline>> = {}, locale: "en" | "de" = "en") {
  return render(
    <NextIntlClientProvider locale={locale} messages={locale === "en" ? en : de}>
      <ChatHeaderSubline model={null} state={null} aliveness="idle" turnStartedAt={null} {...props} />
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
  it("model · Ready when the chat is idle", () => {
    renderLine({ model: "qwen3.8-27b", state: st("idle") });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent("qwen3.8-27b · Ready");
  });

  it("counts the running turn from the operator's last message, from one minute on", () => {
    vi.useFakeTimers({ now: new Date("2026-11-18T12:03:20Z") });
    renderLine({ model: "m", state: st("working"), aliveness: "active", turnStartedAt: "2026-11-18T12:00:00Z" });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent("m · Working for 3 min");
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

  it("an ended session adds no word — the header badge says it", () => {
    renderLine({ model: "m", state: st("idle"), aliveness: "ended" });
    expect(screen.getByTestId("chat-header-subline")).toHaveTextContent(/^m$/);
  });

  it("nothing known → no line at all (K3)", () => {
    renderLine();
    expect(screen.queryByTestId("chat-header-subline")).toBeNull();
  });
});
