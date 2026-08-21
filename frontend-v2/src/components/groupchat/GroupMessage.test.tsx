import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { GroupMessage } from "./GroupMessage";
import type { GroupMessage as GroupMessageData } from "@/lib/groupTypes";

function mkMessage(overrides: Partial<GroupMessageData> = {}): GroupMessageData {
  return {
    id: "m1",
    thread_id: "t1",
    seq: 1,
    sender_type: "agent",
    sender_id: "a1",
    message_type: "text",
    body: "Ein Beitrag.",
    mentions: [],
    created_at: "2026-08-20T14:05:00Z",
    ...overrides,
  };
}

function renderMessage(
  message: GroupMessageData,
  props: Partial<Parameters<typeof GroupMessage>[0]> = {},
) {
  return render(
    <GroupMessage
      message={message}
      senderName={props.senderName !== undefined ? props.senderName : "Sparky"}
      senderEmoji={props.senderEmoji !== undefined ? props.senderEmoji : "🤖"}
      isOwn={props.isOwn ?? false}
      groupWithPrevious={props.groupWithPrevious}
    />,
  );
}

describe("GroupMessage — agent register", () => {
  it("shows the speaker's name above their contribution", () => {
    renderMessage(mkMessage({ body: "Ich habe gemessen." }));
    expect(screen.getByText("Sparky")).toBeInTheDocument();
    expect(screen.getByText("Ich habe gemessen.")).toBeInTheDocument();
  });

  it("renders the body as markdown, not as literal asterisks", () => {
    renderMessage(mkMessage({ body: "Das ist **wichtig**." }));
    const strong = screen.getByText("wichtig");
    expect(strong.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*wichtig\*\*/)).not.toBeInTheDocument();
  });

  it("drops the header when the previous message came from the same speaker", () => {
    renderMessage(mkMessage({ body: "…und weiter." }), { groupWithPrevious: true });
    expect(screen.queryByText("Sparky")).not.toBeInTheDocument();
    expect(screen.getByText("…und weiter.")).toBeInTheDocument();
  });

  it("shows the send time as 24h HH:MM in the header", () => {
    const iso = "2026-08-20T14:05:00Z";
    renderMessage(mkMessage({ created_at: iso }));
    const clock = new Date(iso).toLocaleTimeString("de-CH", {
      hour: "2-digit",
      minute: "2-digit",
      hour12: false,
    });
    expect(clock).toMatch(/^\d{2}:\d{2}$/);
    expect(screen.getByText(clock)).toBeInTheDocument();
  });

  it("omits the name when the sender could not be resolved, instead of showing an id", () => {
    renderMessage(mkMessage({ sender_id: "8f3c-uuid" }), { senderName: null });
    expect(screen.queryByText("8f3c-uuid")).not.toBeInTheDocument();
    expect(screen.getByText("Ein Beitrag.")).toBeInTheDocument();
  });

  it("does not repeat mentions outside the body text", () => {
    // Absendername bewusst verschieden vom Erwähnten, sonst zählt die Kopfzeile mit.
    renderMessage(mkMessage({ body: "@sparky bitte messen.", mentions: ["sparky"] }), {
      senderName: "Mia",
    });
    expect(screen.getAllByText(/sparky/i)).toHaveLength(1);
    expect(screen.getByText("@sparky bitte messen.")).toBeInTheDocument();
  });
});

describe("GroupMessage — own message register", () => {
  it("aligns the bubble to the right and labels it for screen readers only", () => {
    renderMessage(mkMessage({ sender_type: "user", body: "Startet mal." }), { isOwn: true });
    const bubble = screen.getByTestId("group-message-user");
    expect(bubble.className).toContain("ml-auto");
    expect(screen.getByText("You")).toHaveClass("sr-only");
    expect(bubble).toHaveTextContent("Startet mal.");
  });

  it("keeps the operator's own line breaks", () => {
    renderMessage(mkMessage({ sender_type: "user", body: "Zeile 1\nZeile 2" }), { isOwn: true });
    expect(screen.getByTestId("group-message-user").className).toContain("whitespace-pre-wrap");
  });

  it("renders a user message verbatim — no markdown pass on the operator's text", () => {
    renderMessage(mkMessage({ sender_type: "user", body: "Preis **inkl.** MwSt" }), { isOwn: true });
    expect(screen.getByTestId("group-message-user")).toHaveTextContent("Preis **inkl.** MwSt");
    expect(screen.queryByText("inkl.")).not.toBeInTheDocument();
  });
});

describe("GroupMessage — system register", () => {
  it("centers the round letter in quiet mono type and keeps it whole", () => {
    const brief = "Runde 2 abgeschlossen.\nErgebnis aktualisiert.";
    renderMessage(mkMessage({ sender_type: "system", sender_id: null, body: brief }));
    const el = screen.getByTestId("group-message-system");
    expect(el.className).toContain("text-center");
    expect(el.className).toContain("font-mono");
    expect(el.className).toContain("whitespace-pre-wrap");
    expect(el).toHaveTextContent("Ergebnis aktualisiert.");
  });

  it("renders a long system letter in full rather than clamping it", () => {
    const long = `Runde 3\n\n${"Sehr ausführlicher Rundenbrief. ".repeat(40)}ENDE`;
    renderMessage(mkMessage({ sender_type: "system", sender_id: null, body: long }));
    const el = screen.getByTestId("group-message-system");
    expect(el).toHaveTextContent(/ENDE$/);
    expect(el.style.maxHeight).toBe("");
  });

  it("shows no speaker name for a system line", () => {
    renderMessage(mkMessage({ sender_type: "system", sender_id: null, body: "Gate geöffnet." }));
    expect(screen.queryByText("Sparky")).not.toBeInTheDocument();
  });
});

describe("GroupMessage — pending", () => {
  it("dims an unconfirmed message and says what it is waiting for", () => {
    renderMessage(mkMessage({ sender_type: "user", body: "Noch unbestätigt", pending: true }), {
      isOwn: true,
    });
    const bubble = screen.getByTestId("group-message-user");
    expect(bubble.style.opacity).toBe("0.6");
    expect(bubble).toHaveAttribute("title", "Will reach the group with the next round.");
  });

  it("leaves a confirmed message at full opacity and without a title", () => {
    renderMessage(mkMessage({ sender_type: "user", body: "Bestätigt" }), { isOwn: true });
    const bubble = screen.getByTestId("group-message-user");
    expect(bubble.style.opacity).toBe("");
    expect(bubble).not.toHaveAttribute("title");
  });
});
