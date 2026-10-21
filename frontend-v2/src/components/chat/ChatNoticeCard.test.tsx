/**
 * Hinweiskarte im Chat (Vorfall 19.10.2026, omp-Agent nach Runtime-Wechsel).
 *
 * Der Chat-Daemon pinnte jede neue Sitzung auf das alte, von der Runtime
 * nicht mehr angebotene Modell. Jetzt nimmt er das aktuelle und schreibt eine
 * `chat_notice`-Zeile — die muss als eigene, ruhige Warnkarte erscheinen
 * (nicht als rote Fehlerkarte, nicht als neutrale Rueckmeldung) und den Satz
 * mit beiden Modellnamen in der Sprache des Operators zeigen.
 */
import { afterEach, describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

import { ChatMessage } from "./ChatMessage";
import { ChatNoticeCard, NOTICE_VALUES } from "./ChatNoticeCard";
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

/* ── S1-R7: notices whose sentence needs no values ─────────────────────────
   chat_started_fresh (S1-R4: a recreate found no pointer, a new chat began)
   and sink_migration_skipped (S1-R3/F7: the storage move did not run) carry
   only a code. Before S1-R7 the card translated a sentence only together
   with `previous` + `current`, so these showed the daemon's English text
   under a German label — two languages in one card (K10). */
describe("Chat-Hinweiskarte — Saetze ohne Werte (S1-R7)", () => {
  const tDe = () =>
    createTranslator({ locale: "de", messages: de, namespace: "chat" }) as unknown as (key: string) => string;

  it("[6] chat_started_fresh: translated label and sentence without any values", () => {
    render(
      <ChatMessage
        ev={mkNotice({
          text: "The chat could not tell which conversation was open — a new one started. Earlier conversations are kept.",
          source: { kind: "notice", title: "chat_started_fresh" },
          notice: { code: "chat_started_fresh" },
        })}
      />
    );

    // One word for one thing (K10): the same label as every other new
    // conversation (incident 21.10.2026, `new_conversation` below).
    expect(screen.getByTestId("chat-notice-label")).toHaveTextContent("New conversation started");
    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent(
      "After the restart it was unclear which conversation was open. A new one started. Earlier conversations are kept."
    );
    expect(tDe()("notice.chat_started_freshLabel")).toBe("Neues Gespräch begonnen");
    expect(tDe()("notice.chat_started_fresh")).toBe(
      "Nach dem Neustart war nicht klar, welches Gespräch offen war. Ein neues hat begonnen. Frühere Gespräche bleiben gespeichert."
    );
  });

  it("sink_migration_skipped: translated label and sentence", () => {
    render(
      <ChatMessage
        ev={mkNotice({
          text: "Older chat copies were not moved this time.",
          source: { kind: "notice", title: "sink_migration_skipped" },
          notice: { code: "sink_migration_skipped" },
        })}
      />
    );

    expect(screen.getByTestId("chat-notice-label")).toHaveTextContent("Storage not moved");
    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent(
      "Older chat copies were not moved this time. The chat works as usual; the next start tries again."
    );
    expect(tDe()("notice.sink_migration_skippedLabel")).toBe("Ablage nicht umgezogen");
  });

  it("never fills a sentence that needs values when the values are missing — the daemon's text stays", () => {
    render(
      <ChatMessage
        ev={mkNotice({ text: "model fallback without names", notice: { code: "model_fallback" } })}
      />
    );

    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent("model fallback without names");
  });

  it("knows every placeholder a notice sentence uses (EN and DE)", () => {
    // The card fills a sentence only when ALL its values arrived; which
    // values a code needs is written down once (NOTICE_VALUES). A new
    // sentence with a {placeholder} must be registered there, or it would be
    // shown with a raw {name} — this test fails first.
    const ns = (m: unknown) => (m as { chat: { notice: Record<string, string> } }).chat.notice;
    for (const tree of [ns(en), ns(de)]) {
      for (const [key, value] of Object.entries(tree)) {
        if (key === "label" || key.endsWith("Label")) continue;
        const used = [...value.matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort();
        expect(used, `chat.notice.${key}`).toEqual([...(NOTICE_VALUES[key] ?? [])].sort());
      }
    }
  });
});

/* ── Incident 21.10.2026: every chat reset leaves a card ───────────────────
   A `/new` reached the chat daemon and the operator's chat was empty — no
   word that a new conversation had begun, nor that the old one was kept.
   The daemon now writes `chat_notice` `new_conversation` as the first line
   of every conversation that replaces another (`previous`/`current` = the
   two session ids). The card says when: the clock time of the line. */
describe("Chat-Hinweiskarte — neues Gespräch (Vorfall 21.10.2026)", () => {
  // A local clock time, so the test reads the same in every time zone.
  const at = new Date(2026, 9, 21, 17, 7, 46);
  const tDe = () =>
    createTranslator({ locale: "de", messages: de, namespace: "chat" }) as unknown as (key: string) => string;
  const mkNew = (overrides: Partial<MessageEvent> = {}) =>
    mkNotice({
      uuid: "nc1",
      ts: at.toISOString(),
      text: "New conversation started — the previous one is saved.",
      source: { kind: "notice", title: "new_conversation" },
      notice: { code: "new_conversation", previous: "01a1-old", current: "01a2-new" },
      ...overrides,
    });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("shows label, clock time and the promise in the operator's language", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 21, 18, 0, 0));
    render(<ChatMessage ev={mkNew()} />);

    const card = screen.getByTestId("chat-notice-card");
    expect(card).toHaveAttribute("data-code", "new_conversation");
    expect(screen.getByTestId("chat-notice-label")).toHaveTextContent("New conversation started");
    const time = screen.getByTestId("chat-notice-time");
    expect(time).toHaveTextContent(/^17:07$/);
    expect(time).toHaveAttribute("dateTime", at.toISOString());
    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent(
      "The previous conversation is saved."
    );
    // The session ids are data for the later "show previous" link, never text.
    expect(card).not.toHaveTextContent("01a1-old");
    expect(card).not.toHaveTextContent("01a2-new");
    expect(screen.queryByTestId("chat-error-card")).not.toBeInTheDocument();

    expect(tDe()("notice.new_conversationLabel")).toBe("Neues Gespräch begonnen");
    expect(tDe()("notice.new_conversation")).toBe("Das vorherige Gespräch ist gespeichert.");
  });

  it("names the day too when the conversation began on another day", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 23, 9, 0, 0));
    render(<ChatMessage ev={mkNew()} />);

    const time = screen.getByTestId("chat-notice-time").textContent ?? "";
    expect(time).toContain("17:07");
    expect(time).toMatch(/21/);
  });

  it("the recreate card (chat_started_fresh) carries the clock time as well", () => {
    vi.useFakeTimers({ toFake: ["Date"] });
    vi.setSystemTime(new Date(2026, 9, 21, 18, 0, 0));
    render(
      <ChatMessage
        ev={mkNotice({
          ts: at.toISOString(),
          source: { kind: "notice", title: "chat_started_fresh" },
          notice: { code: "chat_started_fresh" },
        })}
      />
    );
    expect(screen.getByTestId("chat-notice-time")).toHaveTextContent(/^17:07$/);
  });

  it("a notice that is no new conversation shows no time", () => {
    render(<ChatMessage ev={mkNotice({ ts: at.toISOString() })} />);
    expect(screen.getByTestId("chat-notice-card")).toBeInTheDocument();
    expect(screen.queryByTestId("chat-notice-time")).not.toBeInTheDocument();
  });

  it("an unreadable time leaves the time out, never shows 'Invalid Date'", () => {
    render(<ChatMessage ev={mkNew({ ts: "not-a-time" })} />);
    expect(screen.queryByTestId("chat-notice-time")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-notice-card")).not.toHaveTextContent("Invalid");
    expect(screen.getByTestId("chat-notice-text")).toHaveTextContent(
      "The previous conversation is saved."
    );
  });

  it("keeps a place for the later 'show previous' link (P4) — empty until then", () => {
    const { rerender } = render(<ChatNoticeCard ev={mkNew()} />);
    expect(screen.queryByTestId("chat-notice-action")).not.toBeInTheDocument();

    rerender(<ChatNoticeCard ev={mkNew()} action={<a href="#previous">show previous</a>} />);
    expect(screen.getByTestId("chat-notice-action")).toHaveTextContent("show previous");
  });

  it("a normal chat without a reset renders no notice card", () => {
    render(
      <>
        <ChatMessage
          ev={{ kind: "message", uuid: "u1", ts: at.toISOString(), role: "user", text: "hallo",
                model: null, sidechain: false }}
        />
        <ChatMessage
          ev={{ kind: "message", uuid: "a1", ts: at.toISOString(), role: "assistant", text: "Hallo!",
                model: "model-a", sidechain: false }}
        />
      </>
    );
    expect(screen.queryByTestId("chat-notice-card")).not.toBeInTheDocument();
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
