import { describe, it, expect } from "vitest";
import { splitAttachments } from "./attachments";

describe("splitAttachments", () => {
  it("lässt eine gewöhnliche Nachricht unangetastet", () => {
    const res = splitAttachments("Hallo, wie geht es dir?");
    expect(res.text).toBe("Hallo, wie geht es dir?");
    expect(res.attachments).toEqual([]);
  });

  it("zieht eine Anhang-Zeile aus dem Text", () => {
    const res = splitAttachments("Was siehst du?\n[Anhang: /mc/chat/rex/2026-08/abc-foto.png]");
    expect(res.text).toBe("Was siehst du?");
    expect(res.attachments).toHaveLength(1);
    expect(res.attachments[0].path).toBe("/mc/chat/rex/2026-08/abc-foto.png");
  });

  it("entfernt das Prüfsummen-Präfix aus dem angezeigten Namen", () => {
    const res = splitAttachments("[Anhang: /p/0123456789abcdef-Bildschirmfoto.png]");
    expect(res.attachments[0].name).toBe("Bildschirmfoto.png");
  });

  it("erkennt Bilder an der Endung, unabhängig von Gross-/Kleinschreibung", () => {
    expect(splitAttachments("[Anhang: /p/a.PNG]").attachments[0].isImage).toBe(true);
    expect(splitAttachments("[Anhang: /p/a.heic]").attachments[0].isImage).toBe(true);
    expect(splitAttachments("[Anhang: /p/a.pdf]").attachments[0].isImage).toBe(false);
    expect(splitAttachments("[Anhang: /p/ohneendung]").attachments[0].isImage).toBe(false);
  });

  it("verarbeitet mehrere Anhänge", () => {
    const res = splitAttachments("schau:\n[Anhang: /p/a.png]\n[Anhang: /p/b.pdf]");
    expect(res.text).toBe("schau:");
    expect(res.attachments.map((a) => a.name)).toEqual(["a.png", "b.pdf"]);
  });

  it("erlaubt eine Nachricht, die NUR aus Anhängen besteht", () => {
    const res = splitAttachments("[Anhang: /p/a.png]");
    expect(res.text).toBe("");
    expect(res.attachments).toHaveLength(1);
  });

  it("greift NICHT mitten im Satz", () => {
    // Sonst würde jede Erwähnung der Zeichenfolge eine Kachel erzeugen —
    // auch in einer Antwort des Agenten, die darüber spricht.
    const text = "Schreib mir [Anhang: /p/a.png] in die Zeile";
    expect(splitAttachments(text)).toEqual({ text, attachments: [] });
  });

  it("greift NICHT bei einer leeren Pfadangabe", () => {
    const text = "[Anhang: ]";
    expect(splitAttachments(text).attachments).toEqual([]);
  });

  it("verkraftet leeren Text", () => {
    expect(splitAttachments("")).toEqual({ text: "", attachments: [] });
  });
});
