/**
 * `toFilesRef` — wo die Kachel die Bytes des Anhangs holt.
 *
 * Zwei Quellen, absichtlich unterschiedlich: Was der Composer gerade
 * hochgeladen hat, KENNT seinen Ort (der Endpunkt gibt root/subpath zurück).
 * Was aus dem Transkript zurückgewonnen wurde, kennt nur den absoluten Pfad —
 * mehr steht in einem CLI-Transkript nicht.
 */
import { describe, it, expect } from "vitest";
import { toFilesRef } from "./ChatAttachmentTile";

const ABS = "/Users/x/.mc/references/agent/4711/ab12-foto.png";

describe("toFilesRef", () => {
  it("nimmt root/subpath vom Endpunkt, statt sie auszurechnen", () => {
    expect(
      toFilesRef({ path: "/ganz/woanders/foto.png", root: "references", subpath: "agent/4711/ab12-foto.png" }),
    ).toEqual({ root: "references", subpath: "agent/4711/ab12-foto.png" });
  });

  it("rechnet den Pfad zurück, wenn nur er bekannt ist (Transkript)", () => {
    expect(toFilesRef({ path: ABS })).toEqual({
      root: "references",
      subpath: "agent/4711/ab12-foto.png",
    });
  });

  it("liefert null für einen Pfad ausserhalb des references-Roots", () => {
    // Lieber nichts laden als eine Adresse raten: eine erfundene URL brächte
    // nur einen 404 mit falscher Erklärung.
    expect(toFilesRef({ path: "/etc/passwd" })).toBeNull();
    expect(toFilesRef({ path: "/Users/x/.mc/references/" })).toBeNull();
  });

  it("ignoriert halbe Angaben und fällt auf den Pfad zurück", () => {
    expect(toFilesRef({ path: ABS, root: "references" })).toEqual({
      root: "references",
      subpath: "agent/4711/ab12-foto.png",
    });
  });
});
