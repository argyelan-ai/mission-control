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

/**
 * Dokument-Leser (Marks Wunsch 08.09.2026): Agenten sollen im Raum kurz
 * schreiben und die Details als Datei anhängen. Ein `.md`/`.txt`-Anhang darf
 * dann nicht als roher Text in einem neuen Tab landen — er öffnet sich an Ort
 * und Stelle, sauber als Markdown gesetzt. Bilder und PDF bleiben, wie sie sind.
 */
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { vi, beforeEach, afterEach } from "vitest";
import { ChatAttachmentTile, isReadableDocument } from "./ChatAttachmentTile";

const DOC = "/Users/x/.mc/references/agent/4711/ab12-bericht.md";

describe("isReadableDocument", () => {
  it("erkennt Markdown und Text, nicht aber Bilder oder PDF", () => {
    expect(isReadableDocument("bericht.md")).toBe(true);
    expect(isReadableDocument("notizen.markdown")).toBe(true);
    expect(isReadableDocument("log.txt")).toBe(true);
    expect(isReadableDocument("mockup.png")).toBe(false);
    expect(isReadableDocument("studie.pdf")).toBe(false);
    expect(isReadableDocument("ohne-endung")).toBe(false);
  });
});

describe("ChatAttachmentTile — Dokument-Leser", () => {
  const fetchMock = vi.fn();
  beforeEach(() => {
    fetchMock.mockReset();
    fetchMock.mockResolvedValue({
      ok: true,
      text: () => Promise.resolve("# Ergebnis\n\nDFlash2 ist **schneller**.\n\n| Motor | t/s |\n|---|---|\n| DFlash2 | 423 |"),
    });
    vi.stubGlobal("fetch", fetchMock);
    // jsdom unter dieser Node-Version liefert kein brauchbares localStorage —
    // `getToken()` braucht aber eines (gleiches Muster wie die Settings-Tests).
    vi.stubGlobal("localStorage", {
      getItem: (k: string) => (k === "mc_auth_token" ? "tok" : null),
      setItem: () => undefined,
      removeItem: () => undefined,
      clear: () => undefined,
    });
  });
  afterEach(() => vi.unstubAllGlobals());

  it("öffnet ein .md auf Klick im Chat als gerendertes Markdown und schliesst es wieder", async () => {
    const user = userEvent.setup({ delay: null });
    render(<ChatAttachmentTile att={{ path: DOC, name: "bericht.md", isImage: false }} />);
    const card = screen.getByTestId("attachment-doc");
    expect(card).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByTestId("attachment-doc-body")).not.toBeInTheDocument();

    await user.click(card);
    await waitFor(() => expect(screen.getByTestId("attachment-doc-body")).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(String(fetchMock.mock.calls[0][0])).toContain("subpath=agent%2F4711%2Fab12-bericht.md");
    expect(screen.getByRole("heading", { name: "Ergebnis" })).toBeInTheDocument();
    expect(screen.getByText("schneller").tagName).toBe("STRONG");
    expect(screen.getByRole("cell", { name: "423" })).toBeInTheDocument();
    expect(screen.queryByText("# Ergebnis")).not.toBeInTheDocument();

    await user.click(card);
    expect(screen.queryByTestId("attachment-doc-body")).not.toBeInTheDocument();
  });

  it("sagt ehrlich, wenn das Dokument nicht mehr da ist", async () => {
    fetchMock.mockResolvedValue({ ok: false, status: 404, text: () => Promise.resolve("") });
    const user = userEvent.setup({ delay: null });
    render(<ChatAttachmentTile att={{ path: DOC, name: "bericht.md", isImage: false }} />);
    await user.click(screen.getByTestId("attachment-doc"));
    await waitFor(() => expect(screen.getByTestId("attachment-doc-body")).toHaveTextContent("no longer available"));
  });

  it("lässt PDF und andere Dateien als Link im neuen Tab", () => {
    render(<ChatAttachmentTile att={{ path: DOC.replace(".md", ".pdf"), name: "studie.pdf", isImage: false }} />);
    expect(screen.getByTestId("attachment-card")).toHaveAttribute("target", "_blank");
    expect(screen.queryByTestId("attachment-doc")).not.toBeInTheDocument();
  });
});
