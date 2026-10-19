/**
 * Hinweiskarte im Chat (Vorfall 19.10.2026, Sparky nach Runtime-Wechsel).
 *
 * Der Chat-Daemon pinnte jede neue Sitzung auf das alte, von der Runtime
 * nicht mehr angebotene Modell. Jetzt nimmt er das aktuelle und schreibt eine
 * `chat_notice`-Zeile — die muss als eigene, ruhige Warnkarte erscheinen
 * (nicht als rote Fehlerkarte, nicht als neutrale Rueckmeldung) und den Satz
 * mit beiden Modellnamen in der Sprache des Operators zeigen.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ChatMessage } from "./ChatMessage";
import { C } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";
import de from "../../../messages/de.json";
import en from "../../../messages/en.json";

const { createTranslator } = await vi.importActual<typeof import("next-intl")>("next-intl");

function mkNotice(overrides: Partial<MessageEvent> = {}): MessageEvent {
  return {
    kind: "message",
    uuid: "n1",
    ts: "2026-10-19T11:08:22Z",
    role: "teammate",
    teammate: "chat_notice",
    text: "Previously chosen model OLD-1 is not available anymore — using NEW-2.",
    model: null,
    sidechain: false,
    source: { kind: "notice", title: "model_fallback" },
    notice: { code: "model_fallback", previous: "OLD-1", current: "NEW-2" },
    ...overrides,
  };
}

describe("Chat-Hinweiskarte (model_fallback)", () => {
  it("renders its own notice card — neither the error card nor the neutral event card", () => {
    render(<ChatMessage ev={mkNotice()} />);

    expect(screen.getByTestId("chat-notice-card")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-error-card")).not.toBeInTheDocument();
    expect(screen.queryByTestId("event-card")).not.toBeInTheDocument();
  });

  it("uses the warning tone of the Signal palette, not the error red", () => {
    render(<ChatMessage ev={mkNotice()} />);

    const card = screen.getByTestId("chat-notice-card");
    expect(card.style.border).toContain(C.warning);
    expect(card.style.border).not.toContain(C.error);
  });

  it("shows the English label and sentence with both model names", () => {
    render(<ChatMessage ev={mkNotice()} />);

    expect(screen.getByTestId("chat-notice-label")).toHaveTextContent("Model switched");
    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent(
      "Previously chosen model OLD-1 is not available anymore — using NEW-2."
    );
  });

  it("formats the German sentence with both model names (real next-intl)", () => {
    // The global mock resolves against EN only — the DE string is checked
    // through the REAL translator, which also proves its ICU placeholders.
    const tDe = createTranslator({ locale: "de", messages: de, namespace: "chat" }) as unknown as (
      key: string,
      values?: Record<string, string>
    ) => string;
    expect(tDe("notice.model_fallback", { previous: "OLD-1", current: "NEW-2" })).toBe(
      "Das zuvor gewählte Modell OLD-1 ist nicht mehr verfügbar — es wird NEW-2 verwendet."
    );
    expect(tDe("notice.model_fallbackLabel")).toBe("Modell gewechselt");
  });

  it("falls back to the daemon's own text for an unknown code — a notice is never swallowed", () => {
    render(
      <ChatMessage
        ev={mkNotice({
          text: "something MC decided",
          source: { kind: "notice", title: "meteor" },
          notice: { code: "meteor" },
        })}
      />
    );

    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent("something MC decided");
    expect(screen.getByTestId("chat-notice-label")).toHaveTextContent("Notice");
  });
});

describe("chat.notice — EN/DE key parity", () => {
  const ns = (m: unknown) => (m as { chat: { notice: Record<string, string> } }).chat.notice;
  it("has the same non-empty keys in both trees", () => {
    expect(Object.keys(ns(en)).sort()).toEqual(Object.keys(ns(de)).sort());
    for (const tree of [ns(en), ns(de)]) {
      for (const [key, value] of Object.entries(tree)) {
        expect(value.trim().length > 0, `chat.notice.${key} is empty`).toBe(true);
      }
    }
  });
});
