/**
 * Fehlerkarte im Chat (Spec docs/specs/chat-over-acp.md, „Errors").
 *
 * Bei einem ACP-Agenten ist der Chat die einzige Oberflaeche — ein Fehler, der
 * nur im Log steht, ist fuer den Operator unsichtbar. Der Daemon schreibt ihn
 * darum als Transkript-Zeile mit `source.kind === "error"`, und die muss sich
 * vom ruhigen Ereignis-Streifen klar abheben: roter Rahmen, Code-Chip, Text.
 *
 * Geprueft wird ueber `ChatMessage`, weil genau die Weiche dort sitzt: ohne
 * sie liefe die Zeile in die neutrale `EventCard` und saehe aus wie eine
 * gewoehnliche Rueckmeldung.
 */
import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import type { ReactElement } from "react";
import { ChatMessage } from "./ChatMessage";
import { ChatResendContext, type ChatResend } from "./chatResend";
import { C } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";
import de from "../../../messages/de.json";
import en from "../../../messages/en.json";

const { createTranslator } = await vi.importActual<typeof import("next-intl")>("next-intl");
/** The global mock answers in English only — German strings are checked
 *  through the REAL translator against messages/de.json. */
const tDe = createTranslator({ locale: "de", messages: de, namespace: "chat" }) as unknown as (
  key: string
) => string;
const tEn = createTranslator({ locale: "en", messages: en, namespace: "chat" }) as unknown as (
  key: string
) => string;

function mkError(overrides: Partial<MessageEvent> = {}): MessageEvent {
  return {
    kind: "message",
    uuid: "e1",
    ts: "2026-09-13T10:00:00Z",
    role: "teammate",
    text: "chat error",
    model: null,
    sidechain: false,
    source: { kind: "error", title: "chat_error" },
    error: { code: "rpc_error", detail: "unknown config option: model" },
    ...overrides,
  };
}

describe("Chat-Fehlerkarte", () => {
  it("renders a distinct error card instead of the neutral event card", () => {
    render(<ChatMessage ev={mkError()} />);

    expect(screen.getByTestId("chat-error-card")).toBeInTheDocument();
    expect(screen.queryByTestId("event-card")).not.toBeInTheDocument();
  });

  it("carries the danger border from the Signal palette", () => {
    render(<ChatMessage ev={mkError()} />);

    // jsdom expandiert die Kurzform `border` nicht zurueck — geprueft wird
    // die Farbe, und die muss aus der Signal-Palette kommen, nicht aus einem
    // handgemalten Rot.
    expect(screen.getByTestId("chat-error-card").style.border).toContain(C.error);
  });

  it("shows the translated code chip and the detail below it", () => {
    render(<ChatMessage ev={mkError()} />);

    const chip = screen.getByTestId("chat-error-code");
    expect(chip).toHaveAttribute("data-code", "rpc_error");
    expect(chip).toHaveTextContent("Protocol error");
    expect(screen.getByText("unknown config option: model")).toBeInTheDocument();
  });

  it("falls back to the generic label for a code we have no string for", () => {
    render(<ChatMessage ev={mkError({ error: { code: "meteor_strike", detail: "boom" } })} />);

    const chip = screen.getByTestId("chat-error-code");
    expect(chip).toHaveAttribute("data-code", "meteor_strike");
    expect(chip).toHaveTextContent("Chat error");
  });

  it("falls back to the line's own text when the daemon sent no detail", () => {
    render(
      <ChatMessage
        ev={mkError({ text: "the turn produced no text", error: { code: "empty_turn", detail: null } })}
      />
    );

    expect(screen.getByTestId("chat-error-code")).toHaveTextContent("No answer");
    expect(screen.getByText("the turn produced no text")).toBeInTheDocument();
  });

  it("labels every code the daemon can emit", () => {
    // Kein Code darf auf den Sammelbegriff zurueckfallen — sonst stuenden zwei
    // verschiedene Stoerungen wortgleich im Verlauf.
    const CODES = [
      "rpc_error",
      "provider_error",
      "empty_turn",
      "process_exit",
      "busy",
      "session_reset",
      "turn_interrupted",
      "model_unreachable",
    ];
    for (const code of CODES) {
      const { unmount } = render(<ChatMessage ev={mkError({ error: { code, detail: "d" } })} />);
      expect(screen.getByTestId("chat-error-code").textContent).not.toBe("Chat error");
      unmount();
    }
  });
});

/* ── S1-R7: known codes speak the operator's language, "Send again" ──────────
   Bauplan S1-Rest §9 (with the corrections F3/F4 of its review): a card the
   daemon writes after a restart or a dead model must say in the UI language
   what happened — the raw detail ("turn 4", omp's ECONNREFUSED line) is only
   small print below — and, when the backend handed over the operator's own
   text, offer ONE tap that sends exactly that text again. */

const RETRY = "Count to 20, run `sleep 2` after each number.";

function renderCard(ev: MessageEvent, resend: Partial<ChatResend> = {}): {
  resendSpy: ReturnType<typeof vi.fn>;
  rerender: (ui: ReactElement) => void;
  wrap: (ctx: Partial<ChatResend>) => ReactElement;
} {
  const resendSpy = vi.fn();
  const wrap = (ctx: Partial<ChatResend>) => (
    <ChatResendContext.Provider value={{ resend: resendSpy, canResend: () => true, ...ctx }}>
      <ChatMessage ev={ev} />
    </ChatResendContext.Provider>
  );
  const { rerender } = render(wrap(resend));
  return { resendSpy, rerender, wrap };
}

const interrupted = (overrides: Partial<MessageEvent> = {}) =>
  mkError({
    uuid: "e-int",
    text: "The chat restarted while a reply was running — nothing was resent automatically.",
    error: { code: "turn_interrupted", detail: "turn 4", retryText: RETRY },
    ...overrides,
  });

const unreachable = () =>
  mkError({
    uuid: "e-down",
    text: "The model is not answering — check the Runtimes page. Your message is not lost.",
    error: {
      code: "model_unreachable",
      detail: "ECONNREFUSED: Unable to connect. Is the computer able to access the url?",
      retryText: "Where do the tests stand?",
    },
  });

describe("Chat-Fehlerkarte — Nochmal senden (S1-R7)", () => {
  it("[1] shows 'Send again' on a card that carries the operator's text (EN + DE)", () => {
    renderCard(interrupted());

    expect(screen.getByTestId("chat-error-resend")).toHaveTextContent("Send again");
    expect(screen.getByTestId("chat-error-resend")).toBeEnabled();
    expect(tDe("error.resend")).toBe("Nochmal senden");
    expect(tDe("error.resent")).toBe("Gesendet");
  });

  it("[2] one tap sends exactly that text once, then locks as 'Sent' — a double tap sends nothing more", () => {
    const { resendSpy } = renderCard(interrupted());

    const button = screen.getByTestId("chat-error-resend");
    fireEvent.click(button);
    fireEvent.click(button);

    expect(resendSpy).toHaveBeenCalledTimes(1);
    expect(resendSpy).toHaveBeenCalledWith(RETRY);
    expect(screen.getByTestId("chat-error-resend")).toBeDisabled();
    expect(screen.getByTestId("chat-error-resend")).toHaveTextContent("Sent");
  });

  it("[2b] stays locked while the send is on its way, and offers itself again only if it did not go out", () => {
    // ChatView stops offering the card the moment the echo exists; when the
    // echo disappears WITHOUT a user line (the request failed, or the
    // operator took it back), the card is the newest offer again.
    const { resendSpy, rerender, wrap } = renderCard(interrupted());
    fireEvent.click(screen.getByTestId("chat-error-resend"));

    rerender(wrap({ canResend: () => false }));
    expect(screen.getByTestId("chat-error-resend")).toBeDisabled();

    rerender(wrap({ canResend: () => true }));
    expect(screen.getByTestId("chat-error-resend")).toBeEnabled();
    expect(screen.getByTestId("chat-error-resend")).toHaveTextContent("Send again");
    expect(resendSpy).toHaveBeenCalledTimes(1);
  });

  it("[3] shows no button when ChatView does not offer this card (an older card, a reply after it)", () => {
    renderCard(interrupted(), { canResend: () => false });

    expect(screen.queryByTestId("chat-error-resend")).not.toBeInTheDocument();
  });

  it("[4] shows no button on a card without the text (older cards, older agent image)", () => {
    renderCard(interrupted({ error: { code: "turn_interrupted", detail: "turn 4" } }));

    expect(screen.queryByTestId("chat-error-resend")).not.toBeInTheDocument();
  });

  it("[9] shows no button when the text was too long to hand over (backend left it out, never shortened)", () => {
    renderCard(interrupted({ error: { code: "turn_interrupted", detail: "turn 4", retryText: null } }));
    expect(screen.queryByTestId("chat-error-resend")).not.toBeInTheDocument();
  });

  it("shows no button outside ChatView (a head's transcript, a read-only view)", () => {
    render(<ChatMessage ev={interrupted()} />);

    expect(screen.queryByTestId("chat-error-resend")).not.toBeInTheDocument();
  });

  it("[5] model_unreachable: the hint sentence, a link to the Runtimes page and the button", () => {
    renderCard(unreachable());

    expect(screen.getByTestId("chat-error-code")).toHaveTextContent("Model not responding");
    expect(screen.getByTestId("chat-error-body")).toHaveTextContent(
      "Check the Runtimes page. Your message is not lost."
    );
    const link = screen.getByTestId("chat-error-runtimes-link");
    expect(link).toHaveAttribute("href", "/runtimes");
    expect(link).toHaveTextContent("Open Runtimes");
    expect(screen.getByTestId("chat-error-resend")).toBeInTheDocument();
    expect(tDe("error.model_unreachable")).toBe("Modell antwortet nicht");
    expect(tDe("error.modelUnreachableHint")).toBe(
      "Prüfe die Runtimes-Seite. Deine Nachricht ist nicht verloren."
    );
    expect(tDe("error.runtimesLink")).toBe("Zur Runtimes-Seite");
  });

  it("puts the translated sentence first and the raw detail only small below it", () => {
    renderCard(unreachable());

    expect(screen.getByTestId("chat-error-body")).toHaveTextContent("Check the Runtimes page.");
    expect(screen.getByTestId("chat-error-detail")).toHaveTextContent(
      "ECONNREFUSED: Unable to connect. Is the computer able to access the url?"
    );
    expect(screen.getByTestId("chat-error-detail").className).toContain("text-xs");
  });

  it("turn_interrupted: the sentence alone — the turn counter ('turn 4') tells the operator nothing (K3/K10)", () => {
    renderCard(interrupted());

    expect(screen.getByTestId("chat-error-code")).toHaveTextContent("Interrupted");
    expect(screen.getByTestId("chat-error-body")).toHaveTextContent(
      "The chat restarted while a reply was running. Nothing was resent automatically."
    );
    expect(screen.queryByTestId("chat-error-detail")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-error-card")).not.toHaveTextContent("turn 4");
    expect(tDe("error.turn_interrupted")).toBe("Unterbrochen");
  });

  it("process_exit during a turn: says the chat reconnects, and drops the bare turn counter", () => {
    renderCard(
      mkError({
        text: "The ACP process died — reconnecting the session.",
        error: { code: "process_exit", detail: "child exited during turn 7" },
      })
    );

    expect(screen.getByTestId("chat-error-body")).toHaveTextContent(
      "The agent process ended unexpectedly. The chat is reconnecting."
    );
    expect(screen.queryByTestId("chat-error-detail")).not.toBeInTheDocument();
    expect(tDe("error.process_exitBody")).toBe(
      "Der Agent-Prozess wurde unerwartet beendet. Der Chat verbindet sich neu."
    );
  });

  it("process_exit with the child's own error keeps that cause as small print", () => {
    renderCard(mkError({ error: { code: "process_exit", detail: "BrokenPipeError: [Errno 32] Broken pipe" } }));

    expect(screen.getByTestId("chat-error-body")).toHaveTextContent("The chat is reconnecting.");
    expect(screen.getByTestId("chat-error-detail")).toHaveTextContent("BrokenPipeError: [Errno 32] Broken pipe");
  });

  it("process_exit after a failed respawn: its own sentence (no 'reconnecting'), the exception as small print", () => {
    renderCard(
      mkError({ error: { code: "process_exit", detail: "restart failed: FileNotFoundError: omp not found" } })
    );

    expect(screen.getByTestId("chat-error-body")).toHaveTextContent(
      "The agent process ended and could not be restarted."
    );
    expect(screen.getByTestId("chat-error-body")).not.toHaveTextContent("reconnecting");
    expect(screen.getByTestId("chat-error-detail")).toHaveTextContent("FileNotFoundError: omp not found");
    expect(screen.getByTestId("chat-error-detail")).not.toHaveTextContent("restart failed");
    expect(tDe("error.process_exit_restart_failedBody")).toBe(
      "Der Agent-Prozess wurde beendet und liess sich nicht neu starten."
    );
  });

  it("clamps a long raw detail under a sentence (session_reset carries exception text up to 4000 chars)", () => {
    // jsdom measures every box as 0 px high; give the content a real height
    // so ClampedContent can see that it overflows.
    const spy = vi.spyOn(HTMLElement.prototype, "scrollHeight", "get").mockReturnValue(2000);
    try {
      const long = `RuntimeError: load failed\n${"x".repeat(3990)}`;
      renderCard(mkError({ error: { code: "session_reset", detail: long } }));

      const detail = screen.getByTestId("chat-error-detail");
      expect(detail).toHaveAttribute("data-clamped", "true");
      expect(detail.className).toContain("whitespace-pre-wrap");
      expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
    } finally {
      spy.mockRestore();
    }
  });

  it("quotes the operator's text it will send — the original bubble may be in another session or far up", () => {
    // F1 of the plan review: after a model change the restart opens a NEW
    // session, so the card lands in a transcript without the operator's line.
    const { rerender, wrap } = renderCard(interrupted());

    const quote = screen.getByTestId("chat-error-retry-text");
    expect(quote).toHaveTextContent(`“${RETRY}”`);
    expect(screen.getByTestId("chat-error-card")).toContainElement(quote);

    fireEvent.click(screen.getByTestId("chat-error-resend"));
    rerender(wrap({ canResend: () => false }));
    expect(screen.getByTestId("chat-error-retry-text")).toHaveTextContent(RETRY);
  });

  it("does not quote the text on a card that offers nothing (an older card, after a reload)", () => {
    renderCard(interrupted(), { canResend: () => false });

    expect(screen.queryByTestId("chat-error-retry-text")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-error-card")).not.toHaveTextContent(RETRY);
  });

  it("[10] session_reset with detail model_missing shows the translated sentence, not the code word", () => {
    renderCard(
      mkError({
        text: "Could not restore model OLD-1 — started a new conversation.",
        error: { code: "session_reset", detail: "model_missing" },
      })
    );

    expect(screen.getByTestId("chat-error-body")).toHaveTextContent(
      "The previous conversation ran on a model that is not set up right now."
    );
    // K3: the sub-code chose the sentence — it is not printed a second time.
    expect(screen.queryByTestId("chat-error-detail")).not.toBeInTheDocument();
    expect(screen.queryByTestId("chat-error-resend")).not.toBeInTheDocument();
    expect(tDe("error.session_reset_model_missingBody")).toBe(
      "Das vorherige Gespräch lief auf einem Modell, das gerade nicht eingetragen ist."
    );
  });

  it("session_reset says only WHY — the new-conversation card above it says that one began (K3, K10)", () => {
    // Critic finding (incident 21.10.2026): amber "New conversation started
    // · the previous one is saved" and right below a red "Session restarted
    // · … a new conversation started, the old one is kept" — the same thing
    // twice, with two nouns. One noun ("conversation"), one statement each.
    renderCard(mkError({ error: { code: "session_reset", detail: "model_missing" } }));

    expect(screen.getByTestId("chat-error-code")).toHaveTextContent("Conversation not resumed");
    expect(tDe("error.session_reset")).toBe("Gespräch nicht fortgesetzt");
    for (const key of [
      "error.session_resetBody",
      "error.session_reset_model_missingBody",
      "error.session_reset_transcript_missingBody",
    ]) {
      expect(tEn(key)).not.toMatch(/new conversation|new one|session/i);
      expect(tDe(key)).not.toMatch(/neues|Sitzung/i);
    }
  });

  it("session_reset with detail transcript_missing has its own sentence", () => {
    renderCard(mkError({ error: { code: "session_reset", detail: "transcript_missing" } }));

    expect(screen.getByTestId("chat-error-body")).toHaveTextContent(
      "The history of the previous conversation was not found."
    );
    expect(screen.queryByTestId("chat-error-detail")).not.toBeInTheDocument();
  });

  it("keeps today's layout for a code without a sentence: the detail is the text", () => {
    renderCard(mkError({ error: { code: "rpc_error", detail: "unknown config option: model" } }));

    expect(screen.queryByTestId("chat-error-body")).not.toBeInTheDocument();
    expect(screen.getByTestId("chat-error-detail")).toHaveTextContent("unknown config option: model");
  });
});

/* ── Layout B (operator choice 20.10.) ──────────────────────────────────────
   "Send again" is the card's last row: a hairline across the card's content,
   then the button over the full width, its label centred. The Runtimes link
   stays above the line, at the text's left edge — it never shares a row with
   the button (variant A put both in one row, button pushed right). jsdom does
   not lay out, so the row is checked through its parts: last child of the
   card, not inside a flex row, full width, centred, the line on its top edge;
   the pictures at 390/320 px and large text show the real layout. */

const BUTTON_CARDS: Array<[string, () => MessageEvent]> = [
  ["interrupted", () => interrupted()],
  ["model not answering", unreachable],
];

describe("Chat-Fehlerkarte — Nochmal senden als volle Zeile (Layout B)", () => {
  it.each(BUTTON_CARDS)("%s: the button is the card's last row, full width and centred", (_name, mk) => {
    renderCard(mk());

    const card = screen.getByTestId("chat-error-card");
    const button = screen.getByRole("button", { name: "Send again" });
    expect(button).toHaveAttribute("data-testid", "chat-error-resend");
    expect(card.lastElementChild).toBe(button);
    expect(button.parentElement).toBe(card);
    expect(button.className).toMatch(/(^|\s)w-full(\s|$)/);
    expect(button.className).toMatch(/(^|\s)justify-center(\s|$)/);
    expect(button.className).not.toContain("ml-auto");
  });

  it.each(BUTTON_CARDS)("%s: a hairline in the card's border tone sits on the row's top edge", (_name, mk) => {
    renderCard(mk());

    const borderTop = screen.getByTestId("chat-error-resend").style.borderTop;
    expect(borderTop).toContain("1px solid");
    // The calm card tone, never the card's red frame (K6: the signal once).
    expect(borderTop).toContain(C.border);
    expect(borderTop).not.toContain(C.error);
  });

  it.each(BUTTON_CARDS)("%s: finger target at least 44 px at every width (K11)", (_name, mk) => {
    renderCard(mk());

    const cls = screen.getByTestId("chat-error-resend").className;
    expect(cls).toMatch(/(^|\s)min-h-11(\s|$)/);
    // no smaller height from a breakpoint on (a tablet is a finger, too)
    expect(cls).not.toMatch(/:min-h-(?:[0-9]|10)(\s|$)/);
  });

  /* jsdom cannot measure the row, so these guard every class or style that
     could make it narrower than the card's content: a max width, auto
     margins, a fixed or content width, an inline box, a breakpoint that
     changes width or display, or a card that lays its rows out as flex/grid
     items (which would shrink the button to its label). A small centred
     button would pass "w-full" alone; it does not pass this. */
  it.each(BUTTON_CARDS)("%s: nothing narrows, centres-as-a-box or hides the row", (_name, mk) => {
    renderCard(mk());

    const button = screen.getByTestId("chat-error-resend");
    const tokens = button.className.split(/\s+/).filter(Boolean);
    const narrowing = tokens.filter((tk) =>
      /^(?:[a-z0-9]+:)*(?:max-w-|w-(?!full$)|mx-|ml-|mr-|ms-|me-|-mx-|-ml-|-mr-|inline|hidden$|invisible$|contents$|self-|basis-|shrink|grow|flex-(?:none|auto|initial|1)$|absolute$|fixed$|float-)/.test(tk)
    );
    expect(narrowing).toEqual([]);
    // w-full holds at every width: no breakpoint variant of anything layout-ish
    expect(tokens.filter((tk) => /^(?:sm|md|lg|xl|2xl):(?!hover|focus|disabled)/.test(tk))).toEqual([]);
    for (const prop of ["width", "maxWidth", "marginLeft", "marginRight", "marginInline", "display", "position"] as const) {
      expect(button.style[prop]).toBe("");
    }
    const cardCls = screen.getByTestId("chat-error-card").className;
    expect(cardCls).not.toMatch(/(^|\s)(?:[a-z0-9]+:)*(?:flex|inline-flex|grid|inline-grid|inline-block|items-|justify-|place-)/);
  });

  it.each(BUTTON_CARDS)("%s: the hover surface is shaped like the card's controls, its top straight on the hairline", (_name, mk) => {
    renderCard(mk());

    const tokens = screen.getByTestId("chat-error-resend").className.split(/\s+/);
    // DESIGN.md Buttons: rounded-md (10 px) — at the two corners that are
    // free. The top edge IS the hairline, so it stays straight: a rounded top
    // would bend the line's ends (and, on hover, leave wedges under it).
    expect(tokens).toContain("rounded-b-md");
    expect(tokens.filter((tk) => /^(?:[a-z0-9]+:)*rounded(?:$|-(?!b-md$))/.test(tk))).toEqual([]);
  });

  it("model not answering: the Runtimes link is a finger target of 44 px at every width, like the button (K11)", () => {
    renderCard(unreachable());

    const cls = screen.getByTestId("chat-error-runtimes-link").className;
    expect(cls).toMatch(/(^|\s)min-h-11(\s|$)/);
    // one finger-target rule in the card: no smaller link from a breakpoint on
    expect(cls).not.toMatch(/:min-h-(?:[0-9]|10)(\s|$)/);
  });

  it("model not answering: the Runtimes link stays above the line, never in the button's row", () => {
    renderCard(unreachable());

    const link = screen.getByTestId("chat-error-runtimes-link");
    const button = screen.getByTestId("chat-error-resend");
    expect(link.compareDocumentPosition(button) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(link.contains(button)).toBe(false);
    // the link is not wrapped into a shared action row with the button
    expect(link.parentElement).toBe(screen.getByTestId("chat-error-card"));
    expect(link.parentElement?.className ?? "").not.toMatch(/(^|\s)flex(\s|$)/);
  });

  it("after the tap the row keeps its place and shape ('Sent', locked, same line)", () => {
    renderCard(interrupted());
    fireEvent.click(screen.getByTestId("chat-error-resend"));

    const button = screen.getByRole("button", { name: "Sent" });
    expect(button).toBeDisabled();
    expect(screen.getByTestId("chat-error-card").lastElementChild).toBe(button);
    expect(button.className).toMatch(/(^|\s)w-full(\s|$)/);
    expect(button.style.borderTop).toContain(C.border);
  });

  it("a card without the button draws no line (nothing below the text)", () => {
    renderCard(interrupted(), { canResend: () => false });

    const card = screen.getByTestId("chat-error-card");
    expect(screen.queryByTestId("chat-error-resend")).not.toBeInTheDocument();
    for (const el of Array.from(card.querySelectorAll<HTMLElement>("*"))) {
      expect(el.style.borderTop).toBe("");
    }
  });
});
