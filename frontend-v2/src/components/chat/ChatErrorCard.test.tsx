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
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { ChatMessage } from "./ChatMessage";
import { C } from "@/lib/colors";
import type { MessageEvent } from "@/lib/chatTypes";

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

    // jsdom's getComputedStyle() tries to really resolve border-color (anders als
    // z.B. `color`) und faellt ohne geladenes Stylesheet fuer die Custom Property
    // auf Schwarz zurueck (ADR-084 macht C.error zu var(--color-status-error)) —
    // toHaveStyle({ borderColor }) saehe dann Schwarz statt der Signal-Farbe.
    // Der spezifizierte (nicht der berechnete) Wert ist deshalb der verlaessliche
    // Weg, dass die Farbe aus der Signal-Palette kommt, nicht aus einem
    // handgemalten Rot.
    expect(screen.getByTestId("chat-error-card").style.borderColor).toBe(C.error);
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
    ];
    for (const code of CODES) {
      const { unmount } = render(<ChatMessage ev={mkError({ error: { code, detail: "d" } })} />);
      expect(screen.getByTestId("chat-error-code").textContent).not.toBe("Chat error");
      unmount();
    }
  });
});
