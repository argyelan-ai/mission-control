import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GroupMessage, contributionExcerpt } from "./GroupMessage";
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
      senderName={props.senderName !== undefined ? props.senderName : "Alpha"}
      senderEmoji={props.senderEmoji !== undefined ? props.senderEmoji : "🤖"}
      isOwn={props.isOwn ?? false}
      groupWithPrevious={props.groupWithPrevious}
      alsoContains={props.alsoContains}
      defaultOpen={props.defaultOpen}
    />,
  );
}

describe("GroupMessage — agent register", () => {
  it("shows the speaker's name on the contribution's header row", () => {
    renderMessage(mkMessage({ body: "Ich habe gemessen." }));
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByTestId("group-contribution-toggle")).toHaveTextContent("Ich habe gemessen.");
  });

  it("renders the body as markdown, not as literal asterisks", async () => {
    renderMessage(mkMessage({ body: "Das ist **wichtig**." }));
    await userEvent.setup({ delay: null }).click(screen.getByTestId("group-contribution-toggle"));
    const strong = screen.getByText("wichtig");
    expect(strong.tagName).toBe("STRONG");
    expect(screen.queryByText(/\*\*wichtig\*\*/)).not.toBeInTheDocument();
  });

  it("keeps the header row even after the same speaker — it is the handle to open", () => {
    // Zugeklappt IST die Kopfzeile der Beitrag. Ohne sie gäbe es nichts zum
    // Anklicken und der zweite Beitrag desselben Sprechers wäre unsichtbar.
    renderMessage(mkMessage({ body: "…und weiter." }), { groupWithPrevious: true });
    expect(screen.getByText("Alpha")).toBeInTheDocument();
    expect(screen.getByTestId("group-contribution-toggle")).toHaveTextContent("…und weiter.");
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
    expect(screen.getByTestId("group-contribution-toggle")).toHaveTextContent("Ein Beitrag.");
  });

  it("does not repeat mentions outside the body text", async () => {
    // Absendername bewusst verschieden vom Erwähnten, sonst zählt die Kopfzeile mit.
    renderMessage(mkMessage({ body: "@alpha bitte messen.", mentions: ["alpha"] }), {
      senderName: "Mia",
    });
    await userEvent.setup({ delay: null }).click(screen.getByTestId("group-contribution-toggle"));
    // Offen weicht der Auszug dem Text — die Erwähnung steht genau einmal da.
    expect(screen.getAllByText(/alpha/i)).toHaveLength(1);
    expect(screen.getByText("@alpha bitte messen.")).toBeInTheDocument();
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

  it("folds a long system letter away instead of walling the transcript", () => {
    // Verhalten bewusst geaendert (Operator-Wunsch 21.08.2026): frueher lief
    // der Rundenbrief in voller Laenge zwischen den Beitraegen — auf dem
    // Handy mehrere Bildschirme Maschinen-Text. Nichts geht verloren: der
    // Knopf traegt die erste Zeile, ein Klick zeigt alles.
    const long = `Runde 3\n\n${"Sehr ausführlicher Rundenbrief. ".repeat(40)}ENDE`;
    renderMessage(mkMessage({ sender_type: "system", sender_id: null, body: long }));
    expect(screen.getByTestId("group-system-toggle")).toHaveTextContent("Runde 3");
    expect(screen.getByTestId("group-message-system")).not.toHaveTextContent(/ENDE$/);
  });

  it("shows no speaker name for a system line", () => {
    renderMessage(mkMessage({ sender_type: "system", sender_id: null, body: "Gate geöffnet." }));
    expect(screen.queryByText("Alpha")).not.toBeInTheDocument();
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

describe("GroupMessage — lange System-Nachrichten", () => {
  const longBody = [
    "# Gruppe: Spark-Standard — Runde 2/3",
    "",
    "## Ziel",
    "Entscheide, ob DFlash2 Standard wird.",
    "",
    "## Deine Aufgabe",
    "x".repeat(400),
  ].join("\n");

  it("collapses a round brief and shows its first line as the label", () => {
    render(
      <GroupMessage
        message={mkMessage({ sender_type: "system", sender_id: null, body: longBody })}
        senderName={null}
        senderEmoji={null}
        isOwn={false}
      />,
    );
    expect(screen.getByTestId("group-system-toggle")).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText("Gruppe: Spark-Standard — Runde 2/3")).toBeInTheDocument();
    expect(screen.queryByTestId("group-system-body")).not.toBeInTheDocument();
  });

  it("reveals the full text on click and hides it again", async () => {
    const user = userEvent.setup({ delay: null });
    render(
      <GroupMessage
        message={mkMessage({ sender_type: "system", sender_id: null, body: longBody })}
        senderName={null}
        senderEmoji={null}
        isOwn={false}
      />,
    );
    await user.click(screen.getByTestId("group-system-toggle"));
    expect(screen.getByTestId("group-system-body")).toHaveTextContent("Entscheide, ob DFlash2 Standard wird.");
    expect(screen.getByTestId("group-system-toggle")).toHaveAttribute("aria-expanded", "true");

    await user.click(screen.getByTestId("group-system-toggle"));
    expect(screen.queryByTestId("group-system-body")).not.toBeInTheDocument();
  });

  it("keeps a short system note open — a timeout line is only useful unbidden", () => {
    render(
      <GroupMessage
        message={mkMessage({
          sender_type: "system",
          sender_id: null,
          body: "⏳ Timeout — übersprungen: @rex (keine Antwort nach 600s).",
        })}
        senderName={null}
        senderEmoji={null}
        isOwn={false}
      />,
    );
    expect(screen.queryByTestId("group-system-toggle")).not.toBeInTheDocument();
    expect(screen.getByTestId("group-message-system")).toHaveTextContent("übersprungen: @rex");
  });
});

describe("GroupMessage — zugeklappte Agenten-Beiträge", () => {
  // Marks Befund 02.09.2026: offen stehende Beiträge machen den Raum laut.
  // Zugeklappt liest man Sprecher + erste Zeile und macht gezielt auf, was
  // man wirklich lesen will. Die frühere 3-Zeilen-Klemme entfällt damit.
  it("starts collapsed: header row with name and first line, no body", () => {
    renderMessage(mkMessage({ body: "Meine Position zuerst.\n\nDann viele Belege." }));
    const toggle = screen.getByTestId("group-contribution-toggle");
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(toggle).toHaveTextContent("Meine Position zuerst.");
    expect(screen.queryByTestId("group-contribution-body")).not.toBeInTheDocument();
    expect(screen.queryByText("Dann viele Belege.")).not.toBeInTheDocument();
  });

  it("opens on click with the full markdown body and closes again", async () => {
    const user = userEvent.setup({ delay: null });
    renderMessage(mkMessage({ body: "Erste Zeile.\n\nDas ist **wichtig**." }));
    await user.click(screen.getByTestId("group-contribution-toggle"));
    expect(screen.getByTestId("group-contribution-toggle")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("wichtig").tagName).toBe("STRONG");

    await user.click(screen.getByTestId("group-contribution-toggle"));
    expect(screen.queryByTestId("group-contribution-body")).not.toBeInTheDocument();
  });

  it("renders a GFM table inside an opened contribution", async () => {
    const user = userEvent.setup({ delay: null });
    const body = "Vergleich:\n\n| Motor | t/s |\n|---|---|\n| DFlash2 | 423 |\n| vLLM | 56 |";
    renderMessage(mkMessage({ body }));
    await user.click(screen.getByTestId("group-contribution-toggle"));
    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByRole("columnheader", { name: "Motor" })).toBeInTheDocument();
    expect(screen.getByRole("cell", { name: "423" })).toBeInTheDocument();
  });

  it("starts open when asked to (lead synthesis) and can still be folded", async () => {
    const user = userEvent.setup({ delay: null });
    renderMessage(mkMessage({ body: "ZIEL ERREICHT: DFlash2 wird Standard." }), { defaultOpen: true });
    expect(screen.getByTestId("group-contribution-toggle")).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByTestId("group-contribution-body")).toHaveTextContent("DFlash2 wird Standard.");
    await user.click(screen.getByTestId("group-contribution-toggle"));
    expect(screen.queryByTestId("group-contribution-body")).not.toBeInTheDocument();
  });

  it("leaves the operator's own bubble alone — no expander there", () => {
    renderMessage(mkMessage({ sender_type: "user", body: "Langer Auftrag. ".repeat(60) }), {
      isOwn: true,
    });
    expect(screen.queryByTestId("group-contribution-toggle")).not.toBeInTheDocument();
    expect(screen.getByTestId("group-message-user")).toHaveTextContent("Langer Auftrag.");
  });

  it("leaves system messages on their own mechanism (regression)", () => {
    const long = `# Gruppe: Spark — Runde 2/3\n\n${"Auftragstext. ".repeat(60)}`;
    renderMessage(mkMessage({ sender_type: "system", sender_id: null, body: long }));
    expect(screen.getByTestId("group-system-toggle")).toHaveTextContent("Gruppe: Spark — Runde 2/3");
    expect(screen.queryByTestId("group-contribution-toggle")).not.toBeInTheDocument();
  });

  it("führt mehrere Maschinen-Aufträge einer Runde als EINE Zeile", async () => {
    const user = userEvent.setup();
    renderMessage(
      mkMessage({
        sender_type: "system",
        body: "# Gruppe: Test — Runde 1/2\n" + "x".repeat(400),
      }),
      { alsoContains: ["@lead — Synthese-Turn Runde 1/2.\n" + "y".repeat(400)] },
    );
    expect(screen.getAllByTestId("group-system-toggle")).toHaveLength(1);
    expect(screen.getByTestId("group-system-toggle")).toHaveTextContent("+1");
    await user.click(screen.getByTestId("group-system-toggle"));
    const body = screen.getByTestId("group-system-body");
    expect(body).toHaveTextContent("Gruppe: Test");
    expect(body).toHaveTextContent("Synthese-Turn");
  });
});

describe("contributionExcerpt — die eine Zeile auf dem Aufklapper", () => {
  it("takes the first non-empty line and strips markdown decoration", () => {
    expect(contributionExcerpt("\n\n## **Fazit**: DFlash2 gewinnt\n\nMehr…")).toBe("Fazit: DFlash2 gewinnt");
  });

  it("reads a table's header row as a phrase, not as pipes", () => {
    expect(contributionExcerpt("| Motor | t/s |\n|---|---|\n| DFlash2 | 423 |")).toBe("Motor · t/s");
  });

  it("drops list markers and quotes", () => {
    expect(contributionExcerpt("- erster Punkt")).toBe("erster Punkt");
    expect(contributionExcerpt("> Zitat")).toBe("Zitat");
  });

  it("is empty for an empty body", () => {
    expect(contributionExcerpt("")).toBe("");
  });
});
