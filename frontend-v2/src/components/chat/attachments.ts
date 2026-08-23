/**
 * Anhang-Referenzen im Nachrichtentext erkennen.
 *
 * Der Composer hängt Anhänge als eigene Zeile `[Anhang: /pfad/datei.png]` an
 * die Nachricht — die CLI liest die Datei über genau diesen Pfad. Im Verlauf
 * soll davon aber keine Pfadzeile stehen, sondern eine Kachel.
 *
 * Warum überhaupt geparst wird, statt die Anhänge getrennt zu übertragen:
 * Der Verlauf ist das TRANSKRIPT der CLI, nicht unsere Datenbank. Was dort
 * steht, ist der Text, den der Agent bekommen hat — mehr gibt es nicht. Die
 * Kachel wird deshalb aus dem Text zurückgewonnen; das hält den Sendeweg frei
 * von einem Nebenkanal, den die CLI ohnehin nicht kennt.
 */

export interface ParsedAttachmentRef {
  path: string;
  name: string;
  isImage: boolean;
  /** Der Ort in der Sprache des Files-Endpunkts, WENN er bekannt ist: der
   *  Anhang-Endpunkt liefert ihn direkt mit. Aus dem Transkript zurueck-
   *  gewonnene Anhaenge haben ihn nicht — dort steht nur der Pfad, den der
   *  Agent gesehen hat. `ChatAttachmentTile` faellt dann darauf zurueck. */
  root?: string;
  subpath?: string;
}

export interface ParsedMessage {
  /** Der Text ohne die Anhang-Zeilen (kann leer sein — ein Bild allein ist
   *  eine vollwertige Nachricht). */
  text: string;
  attachments: ParsedAttachmentRef[];
}

const IMAGE_EXTENSIONS = new Set([
  "png", "jpg", "jpeg", "gif", "webp", "heic", "heif", "bmp", "avif",
]);

// Bewusst eng: die Zeile muss ALLEIN stehen und exakt so aussehen, wie der
// Composer sie schreibt. Ein Agent, der in seiner Antwort zufällig über
// "[Anhang: …]" schreibt, darf keine Kachel erzeugen — und ein Nutzer, der
// die Zeichenfolge mitten in einem Satz tippt, ebenso wenig.
const ATTACHMENT_LINE = /^\[Anhang:\s*(.+?)\]$/;

export function splitAttachments(text: string): ParsedMessage {
  const attachments: ParsedAttachmentRef[] = [];
  const kept: string[] = [];

  for (const line of (text ?? "").split("\n")) {
    const match = ATTACHMENT_LINE.exec(line.trim());
    if (!match) {
      kept.push(line);
      continue;
    }
    const path = match[1].trim();
    if (!path) {
      kept.push(line);
      continue;
    }
    const name = path.split("/").pop() || path;
    // Der Prüfsummen-Präfix (16 Hex + Bindestrich) ist Technik der Ablage,
    // kein Teil des Namens, den Mark der Datei gegeben hat.
    const display = name.replace(/^[0-9a-f]{16}-/, "");
    const ext = display.includes(".") ? display.split(".").pop()!.toLowerCase() : "";
    attachments.push({ path, name: display, isImage: IMAGE_EXTENSIONS.has(ext) });
  }

  return { text: kept.join("\n").trim(), attachments };
}
