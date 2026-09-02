import { describe, it, expect } from "vitest";
import { applyGroupPreviewEvent, type GroupPreviews } from "./useGroupStream";

// Reines Vorschau-Fach des Gruppenraums (Live-Schicht A im Gruppenchat):
// `group.preview {agent_id, text, ts}` pro Mitglied; leerer Text löscht.
// Getestet wird die reine Funktion — der Haken selbst hängt an useSSE.

const A = "agent-a";
const B = "agent-b";

describe("applyGroupPreviewEvent", () => {
  it("legt pro Sprecher eine Vorschau an und ersetzt sie beim nächsten Frame", () => {
    let p: GroupPreviews = {};
    p = applyGroupPreviewEvent(p, "group.preview", { agent_id: A, text: "Ich", ts: "1" });
    p = applyGroupPreviewEvent(p, "group.preview", { agent_id: B, text: "Wir", ts: "2" });
    p = applyGroupPreviewEvent(p, "group.preview", { agent_id: A, text: "Ich denke", ts: "3" });
    expect(p).toEqual({ [A]: { text: "Ich denke", ts: "3" }, [B]: { text: "Wir", ts: "2" } });
  });

  it("löscht die Vorschau bei leerem Text (fertige Antwort / idle / neue Sitzung)", () => {
    let p: GroupPreviews = { [A]: { text: "x", ts: "1" }, [B]: { text: "y", ts: "1" } };
    p = applyGroupPreviewEvent(p, "group.preview", { agent_id: A, text: "", ts: null });
    expect(p).toEqual({ [B]: { text: "y", ts: "1" } });
  });

  it("löscht die Vorschau des Sprechers, sobald seine Nachricht im Raum steht", () => {
    let p: GroupPreviews = { [A]: { text: "x", ts: "1" }, [B]: { text: "y", ts: "1" } };
    p = applyGroupPreviewEvent(p, "group.message_posted", {
      message: { sender_type: "agent", sender_id: A, body: "fertig" },
    });
    expect(p).toEqual({ [B]: { text: "y", ts: "1" } });
  });

  it("lässt fremde Ereignisse unverändert (gleiche Referenz — kein Re-Render)", () => {
    const p: GroupPreviews = { [A]: { text: "x", ts: "1" } };
    expect(applyGroupPreviewEvent(p, "group.turn_started", { speaker: A })).toBe(p);
    expect(applyGroupPreviewEvent(p, "group.message_posted", { message: { sender_type: "user", sender_id: null } })).toBe(p);
  });
});
