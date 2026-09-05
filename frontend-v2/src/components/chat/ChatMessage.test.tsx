import { describe, it, expect, afterEach, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { ChatMessage, USER_CLAMP_MAX_PX } from "./ChatMessage";
import { splitAttachments } from "./attachments";
import type { MessageEvent } from "@/lib/chatTypes";

// Echte Umsetzung, nur mitgezaehlt: der Anhang-Parser darf ueberhaupt nicht
// laufen, wo sein Ergebnis nie gelesen wird (siehe Test weiter unten).
vi.mock("./attachments", async () => {
  const actual = await vi.importActual<typeof import("./attachments")>("./attachments");
  return { ...actual, splitAttachments: vi.fn(actual.splitAttachments) };
});

/** jsdom reports 0 for every layout metric, so the clamp can never see an
 *  overflow on its own. Standing in for scrollHeight is the only way to test
 *  the branch that matters — and mirrors what a long dispatch brief does. */
function stubScrollHeight(px: number) {
  const original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, "scrollHeight");
  Object.defineProperty(HTMLElement.prototype, "scrollHeight", { configurable: true, get: () => px });
  return () => {
    if (original) Object.defineProperty(HTMLElement.prototype, "scrollHeight", original);
    else delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollHeight;
  };
}

function mkEvent(overrides: Partial<MessageEvent> = {}): MessageEvent {
  return {
    kind: "message",
    uuid: "u1",
    ts: "2026-08-15T10:00:00Z",
    role: "assistant",
    text: "Hello there",
    model: "claude-sonnet-5",
    sidechain: false,
    ...overrides,
  };
}

describe("ChatMessage", () => {
  it("renders a user row with the 'Du' label", () => {
    render(<ChatMessage ev={mkEvent({ role: "user", text: "Mach mal X" })} />);
    expect(screen.getByText("Du")).toBeInTheDocument();
    expect(screen.getByText("Mach mal X")).toBeInTheDocument();
  });

  it("renders an assistant row without the 'Du' label", () => {
    render(<ChatMessage ev={mkEvent({ role: "assistant", text: "Erledigt." })} />);
    expect(screen.queryByText("Du")).not.toBeInTheDocument();
    expect(screen.getByText("Erledigt.")).toBeInTheDocument();
  });

  it("keeps the 'Du' attribution available to screen readers only", () => {
    // The right-aligned bubble carries the speaker visually; a visible label
    // would repeat it. Screen readers can't hear alignment, so the text stays.
    render(<ChatMessage ev={mkEvent({ role: "user", text: "Mach mal X" })} />);
    expect(screen.getByText("Du")).toHaveClass("sr-only");
  });

  it("hides the model name by default", () => {
    render(<ChatMessage ev={mkEvent({ model: "claude-sonnet-5" })} />);
    expect(screen.queryByText("claude-sonnet-5")).not.toBeInTheDocument();
  });

  it("shows the model name when the caller flags it as a change", () => {
    render(<ChatMessage ev={mkEvent({ model: "claude-sonnet-5" })} showModel />);
    expect(screen.getByText("claude-sonnet-5")).toBeInTheDocument();
  });

  it("shows no model line for a flagged message that carries no model", () => {
    render(<ChatMessage ev={mkEvent({ model: null, text: "Ohne Modell" })} showModel />);
    expect(screen.getByText("Ohne Modell")).toBeInTheDocument();
  });

  it("renders markdown content: bold as <strong>", () => {
    render(<ChatMessage ev={mkEvent({ text: "Das ist **wichtig**." })} />);
    const strong = screen.getByText("wichtig");
    expect(strong.tagName).toBe("STRONG");
  });

  it("renders fenced code blocks as a block-level <code> (mirrors MemoryPage styling)", () => {
    render(<ChatMessage ev={mkEvent({ text: "```js\nconst x = 1;\n```" })} />);
    const code = screen.getByText((_, el) => el?.tagName === "CODE" && !!el.textContent?.includes("const x = 1;"));
    expect(code.className).toContain("block");
  });

  // ── User bubble: clamp + compact register ─────────────────────────────────
  describe("long user messages", () => {
    let restore: (() => void) | null = null;

    afterEach(() => {
      restore?.();
      restore = null;
    });

    function renderUser(text: string) {
      return render(<ChatMessage ev={mkEvent({ role: "user", text })} />);
    }

    it("leaves a short message unclamped and offers no expander", () => {
      restore = stubScrollHeight(60);
      renderUser("Kurzer Auftrag");
      expect(screen.getByTestId("user-message-content")).toHaveAttribute("data-clamped", "false");
      expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    });

    it("clamps a long dispatch brief and offers an expander", () => {
      restore = stubScrollHeight(USER_CLAMP_MAX_PX * 4);
      renderUser("# Auftrag\n\nSehr langer Brief …");
      const content = screen.getByTestId("user-message-content");
      expect(content).toHaveAttribute("data-clamped", "true");
      expect(content.style.maxHeight).toBe(`${USER_CLAMP_MAX_PX}px`);
      expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
    });

    it("expands and collapses again on the expander", async () => {
      restore = stubScrollHeight(USER_CLAMP_MAX_PX * 4);
      const user = userEvent.setup();
      renderUser("# Auftrag\n\nSehr langer Brief …");

      await user.click(screen.getByRole("button", { name: "Show more" }));
      expect(screen.getByTestId("user-message-content")).toHaveAttribute("data-clamped", "false");

      await user.click(screen.getByRole("button", { name: "Show less" }));
      expect(screen.getByTestId("user-message-content")).toHaveAttribute("data-clamped", "true");
    });

    it("reports nothing hidden while the element cannot be measured (hidden pane)", () => {
      restore = stubScrollHeight(0);
      renderUser("# Auftrag\n\nSehr langer Brief …");
      expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    });

    it("flattens headings inside a user bubble to one body-weight step", () => {
      restore = stubScrollHeight(60);
      renderUser("# Grosse Überschrift");
      const heading = screen.getByText("Grosse Überschrift");
      expect(heading.className).toContain("text-[14px]");
      expect(heading.className).not.toContain("text-lg");
    });

    it("keeps the assistant's own headings at document scale", () => {
      render(<ChatMessage ev={mkEvent({ role: "assistant", text: "# Grosse Überschrift" })} />);
      expect(screen.getByText("Grosse Überschrift").className).toContain("text-lg");
    });
  });
});

// ══════════════════════════════════════════════════════════════════════════
// Anhänge im Verlauf (19.08.2026)
// ══════════════════════════════════════════════════════════════════════════

describe("ChatMessage — Anhänge", () => {
  function mkUserMsg(text: string) {
    return { kind: "message", role: "user", uuid: "u1", ts: "2026-08-19T10:00:00Z", text } as never;
  }

  it("zeigt keinen rohen Pfad, sondern eine Kachel", () => {
    render(<ChatMessage ev={mkUserMsg("Was siehst du?\n[Anhang: /Users/x/.mc/references/chat/rex/2026-08/abc-foto.pdf]")} />);

    expect(screen.getByText("Was siehst du?")).toBeTruthy();
    expect(screen.queryByText(/\[Anhang:/)).toBeNull();
    expect(screen.getByTestId("attachment-card")).toBeTruthy();
  });

  it("zeigt eine Nachricht, die NUR aus einem Anhang besteht", () => {
    render(<ChatMessage ev={mkUserMsg("[Anhang: /Users/x/.mc/references/chat/rex/2026-08/abc-plan.pdf]")} />);
    expect(screen.getByTestId("attachment-card")).toBeTruthy();
  });

  it("lässt eine gewöhnliche Nachricht unverändert", () => {
    render(<ChatMessage ev={mkUserMsg("ganz normaler Text")} />);
    expect(screen.getByText("ganz normaler Text")).toBeTruthy();
    expect(screen.queryByTestId("attachment-card")).toBeNull();
  });
});

describe("ChatMessage — Teamkollegen-Nachricht", () => {
  it("renders a teammate turn WITH a source as an event card", () => {
    render(
      <ChatMessage
        ev={mkEvent({ role: "teammate", teammate: "task-1.md", text: "brief", source: { kind: "task", title: "Beweis" } })}
      />,
    );
    expect(screen.getByTestId("event-card")).toBeTruthy();
    expect(screen.queryByTestId("teammate-row")).toBeNull();
  });

  it("marks the event card live only when told so", () => {
    const ev = mkEvent({ role: "teammate", teammate: "task-1.md", text: "brief", source: { kind: "task", title: "Beweis" } });
    const { rerender } = render(<ChatMessage ev={ev} />);
    expect(screen.getByTestId("event-card")).toHaveAttribute("data-live", "false");
    rerender(<ChatMessage ev={ev} live />);
    expect(screen.getByTestId("event-card")).toHaveAttribute("data-live", "true");
  });

  it("keeps the plain teammate row when the parser claimed no source", () => {
    render(<ChatMessage ev={mkEvent({ role: "teammate", teammate: null, source: null, text: "x" })} />);
    expect(screen.getByTestId("teammate-row")).toBeTruthy();
    expect(screen.queryByTestId("event-card")).toBeNull();
  });

  function mkTeammate(text: string, teammate: string | null = "qwen-research") {
    return { kind: "message", role: "teammate", teammate, uuid: "t1",
             ts: "2026-08-19T10:00:00Z", text, model: null, sidechain: false } as never;
  }

  it("sieht nicht aus wie eine Nachricht des Operators", () => {
    render(<ChatMessage ev={mkTeammate("fertig")} />);
    // Die rechtsbuendige Blase ist dem Operator vorbehalten.
    expect(screen.queryByText("Du")).toBeNull();
    expect(screen.getByTestId("teammate-row")).toBeTruthy();
  });

  it("nennt den Absender", () => {
    render(<ChatMessage ev={mkTeammate("fertig")} />);
    expect(screen.getByText(/qwen-research/)).toBeTruthy();
  });

  it("behauptet keinen Absender, wenn keiner da war", () => {
    render(<ChatMessage ev={mkTeammate("fertig", null)} />);
    expect(screen.getByTestId("teammate-row")).toBeTruthy();
    expect(screen.queryByText(/qwen-research/)).toBeNull();
  });

  // Marks Screenshot 04.09.2026: der eingespielte Auftrag (@task-….md, 2000
  // Zeichen Operating Card) stand als Wand im Verlauf. Er bekommt denselben
  // Klapp-Mechanismus wie ein langer Auftrag in der Operator-Blase.
  describe("Klappe fuer lange Nutzlasten", () => {
    let restore: (() => void) | undefined;
    afterEach(() => { restore?.(); restore = undefined; });

    it("laesst eine kurze Rueckmeldung offen", () => {
      restore = stubScrollHeight(40);
      render(<ChatMessage ev={mkTeammate("fertig")} />);
      expect(screen.getByTestId("teammate-text")).toHaveAttribute("data-clamped", "false");
      expect(screen.queryByRole("button", { name: "Show more" })).not.toBeInTheDocument();
    });

    it("klappt eine lange Nutzlast zu und bietet den Aufklapper", () => {
      restore = stubScrollHeight(USER_CLAMP_MAX_PX * 4);
      render(<ChatMessage ev={mkTeammate("@/home/agent/.omp/tasks/task-1.md\n# Operating Card\n…", "task-1.md")} />);
      expect(screen.getByTestId("teammate-text")).toHaveAttribute("data-clamped", "true");
      expect(screen.getByRole("button", { name: "Show more" })).toBeInTheDocument();
    });
  });

  it("zeigt den Inhalt", () => {
    render(<ChatMessage ev={mkTeammate("Recherche fertig: 128 GB")} />);
    expect(screen.getByText(/128 GB/)).toBeTruthy();
  });

  it("bricht eine lange Nutzlast um, statt den Verlauf breit zu ziehen", () => {
    // Der Text stand in einem blanken <span>, und globals.css hat keine
    // globale Umbruch-Regel (nachgesehen: null Treffer). In Chromium bei 390px
    // nachgemessen:
    //
    //   * ein wirklich unbrechbares Wort (122 Zeichen, keine Satzzeichen)
    //     wird 947,9px breit — die SEITE bekommt einen waagerechten Rollbalken
    //     (scrollWidth 998 statt 390). Mit `break-words`: 310px, drei Zeilen.
    //   * mehrzeilige Nutzlasten kollabieren zu EINER Zeile. Mit
    //     `whitespace-pre-wrap`: zwei Zeilen.
    //
    // Nicht betroffen ist ausgerechnet die haeufigste Nutzlast, das
    // idle_notification-JSON: Chromium bricht sie an den Kommata von selbst um
    // (262,8px, drei Zeilen). Der Umbruch ist trotzdem noetig — Rueckmeldungen
    // sind beliebiger Text, und seit die gebuendelten Bloecke einzeln
    // ankommen, sind mehrzeilige Nutzlasten der Normalfall.
    render(<ChatMessage ev={mkTeammate("Zeile eins\nZeile zwei")} />);
    const body = screen.getByTestId("teammate-text");
    expect(body.className).toContain("break-words");
    expect(body.className).toContain("whitespace-pre-wrap");
  });

  it("laesst den Anhang-Parser gar nicht erst laufen", () => {
    // `splitAttachments` lief fuer JEDE Nachricht — voller split("\n"),
    // Regex je Zeile, join. Gelesen wird das Ergebnis nur im Operator-Zweig.
    // ChatMessage ist nicht memoisiert, das fiel also bei jedem Stream-Tick
    // fuer jede sichtbare Zeile an.
    const spy = vi.mocked(splitAttachments);
    spy.mockClear();
    render(<ChatMessage ev={mkTeammate("fertig")} />);
    expect(spy).not.toHaveBeenCalled();

    spy.mockClear();
    render(<ChatMessage ev={mkEvent({ role: "assistant", text: "Antwort" })} />);
    expect(spy).not.toHaveBeenCalled();

    // Fuer den Operator-Zweig muss er weiterhin laufen — sonst verschwinden
    // die Anhang-Kacheln.
    spy.mockClear();
    render(<ChatMessage ev={mkEvent({ role: "user", text: "Text" })} />);
    expect(spy).toHaveBeenCalled();
  });
});

/**
 * Operator-Befund 01.09.2026 ("meine Nachricht haengt"): Die eigene Zeile
 * stand nach dem Senden 1-3 Sekunden blass da, bis das Transkript sie
 * zurueckspiegelte. Gemessen schreibt Claude Code den User-Turn erst rund
 * eine Sekunde spaeter, plus bis zu einer Sekunde Poll — die Blase ist also
 * genau so lange gedimmt, wie der Chat sich traege anfuehlt.
 *
 * Der Server hat die Zustellung mit 204 quittiert; die Nachricht IST
 * unterwegs. Sie deshalb auszugrauen sagt etwas Falsches. Die ehrliche
 * Warnung bleibt: nach zehn Sekunden ohne Bestaetigung wird die Blase
 * markiert (eigener Test oben).
 */
describe("Echo-Blase", () => {
  it("zeigt eine gerade gesendete Nachricht in voller Deckkraft", () => {
    const { container } = render(
      <ChatMessage ev={mkEvent({ role: "user", text: "hallo" })} echoStatus="pending" />,
    );
    const bubble = container.querySelector('[data-testid="echo-bubble"]') as HTMLElement;
    expect(bubble).not.toBeNull();
    expect(bubble.style.opacity === "" || Number(bubble.style.opacity) >= 1).toBe(true);
  });
});
