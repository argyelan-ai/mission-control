/**
 * ResultDocPanel — vitest (ADR-075).
 *
 * Deckt ab: Inhalt rendern, Leerzustand (auch als stiller Fehlerfall),
 * Stepper lädt den Runden-Snapshot, Band + Rücksprung auf den Live-Stand,
 * Puls-Hinweis beim Umschreiben und das Kopieren in die Zwischenablage.
 *
 * `src/test-setup.ts` mockt next-intl gegen messages/en.json — geprüft werden
 * daher die englischen Labels.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import { ResultDocPanel } from "./ResultDocPanel";
import { api } from "@/lib/api";
import type { GroupDocument, GroupRoundInfo } from "@/lib/groupTypes";

vi.mock("@/lib/api", () => ({
  api: {
    groups: {
      document: vi.fn(),
      rounds: vi.fn(),
    },
  },
}));

const documentMock = vi.mocked(api.groups.document);
const roundsMock = vi.mocked(api.groups.rounds);

function mkRound(round_no: number, has_doc_snapshot = true): GroupRoundInfo {
  return {
    id: `round-${round_no}`,
    round_no,
    kind: "autonomous",
    brief_seq: null,
    outcome: "ok",
    report: null,
    pending_speakers: [],
    has_doc_snapshot,
    tokens_used: null,
    cost_usd: null,
    started_at: null,
    finished_at: null,
  };
}

function mkDoc(content: string, version: number | null = null): GroupDocument {
  return { rel_path: "groups/g1/result.md", content, version };
}

beforeEach(() => {
  vi.clearAllMocks();
  roundsMock.mockResolvedValue({ rounds: [] });
  documentMock.mockResolvedValue(mkDoc(""));
});

afterEach(() => {
  Reflect.deleteProperty(navigator, "clipboard");
});

describe("ResultDocPanel", () => {
  it("puts the verdict in the header instead of burying it under the goal", async () => {
    // Der ganze Zweck des Umbaus (22.08.2026): wer das Panel öffnet, will EINE
    // Sache wissen. Vorher stand ganz oben der Vorspann für den Lead-Agenten,
    // dann das selbst getippte Ziel — die Antwort kam erst an dritter Stelle.
    documentMock.mockResolvedValue(
      mkDoc(
        "# Spark\n\n> Engine note for the lead.\n\n## Goal\n\nDecide the engine.\n\n" +
          "## Verdict\n\nDFlash2 wins on throughput.\n",
      ),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    expect(await screen.findByTestId("result-verdict")).toHaveTextContent(
      "DFlash2 wins on throughput.",
    );
    expect(documentMock).toHaveBeenCalledWith("g1");
  });

  it("keeps every section shut and opens the one that is asked for", async () => {
    documentMock.mockResolvedValue(
      mkDoc("# S\n\n## Verdict\n\nShort answer.\n\n## Evidence\n\nThe long proof.\n"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByTestId("result-verdict");
    // Zugeklappt heisst: kein Rumpf im Dokument. Die Vorschauzeile bleibt
    // sichtbar — sie ist der Grund, warum man weiss, was sich zu öffnen lohnt.
    expect(screen.queryByTestId("result-section-body")).not.toBeInTheDocument();

    const evidence = screen.getAllByTestId("result-section-toggle")[1];
    fireEvent.click(evidence);

    const body = await screen.findByTestId("result-section-body");
    expect(body).toHaveTextContent("The long proof.");
    // Nur DER angeklickte Abschnitt geht auf, nicht alle.
    expect(screen.getAllByTestId("result-section-body")).toHaveLength(1);
  });

  it("moves the goal behind the sections that carry the answer", async () => {
    documentMock.mockResolvedValue(
      mkDoc("# S\n\n## Goal\n\nDecide.\n\n## Verdict\n\nDone.\n"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByTestId("result-verdict");
    const titles = screen.getAllByTestId("result-section-toggle").map((b) => b.textContent);
    expect(titles[0]).toContain("Verdict");
    expect(titles[titles.length - 1]).toContain("Goal");
  });

  it("tucks the engine's preamble away instead of leading with it", async () => {
    documentMock.mockResolvedValue(
      mkDoc("# S\n\n> Only the lead agent writes this file.\n\n## Verdict\n\nDone.\n"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByTestId("result-verdict");
    // Sichtbar ist nur der Knopf — der Text erst auf Wunsch. Weggeworfen wird
    // er nicht: er erklärt dem Lead seine Pflichten und gehört zum Dokument.
    expect(screen.queryByTestId("result-note-body")).not.toBeInTheDocument();
    fireEvent.click(screen.getByTestId("result-note-toggle"));
    expect(await screen.findByTestId("result-note-body")).toHaveTextContent("lead agent");
  });

  it("shows the empty state when the document has no content", async () => {
    documentMock.mockResolvedValue(mkDoc("   "));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    expect(
      await screen.findByText("No result yet — it grows with the first round."),
    ).toBeInTheDocument();
  });

  it("falls back to the empty state when loading fails, without surfacing an error", async () => {
    documentMock.mockRejectedValue(new Error("API 404: not found"));
    roundsMock.mockRejectedValue(new Error("API 500: boom"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    expect(
      await screen.findByText("No result yet — it grows with the first round."),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("result-timeline")).not.toBeInTheDocument();
  });

  it("hides the timeline while no round has a snapshot", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1, false)] });
    documentMock.mockResolvedValue(mkDoc("live text"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    expect(await screen.findByTestId("result-verdict")).toHaveTextContent("live text");
    expect(screen.queryByTestId("result-timeline")).not.toBeInTheDocument();
  });

  it("loads a round's snapshot when its point on the timeline is tapped", async () => {
    // Zeitleiste statt `‹ 2 von 3 ›`: auf dem Handy passt eine Reihe
    // antippbarer Punkte in eine Zeile, und man sieht, WIE VIELE Runden es gab.
    roundsMock.mockResolvedValue({ rounds: [mkRound(1), mkRound(2)] });
    documentMock.mockImplementation(async (_id: string, version?: number) =>
      version === 1 ? mkDoc("snapshot of round one", 1) : mkDoc("newest text"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={2} />);

    expect(await screen.findByTestId("result-verdict")).toHaveTextContent("newest text");

    fireEvent.click(screen.getByRole("button", { name: "Older version (round 1)" }));

    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g1", 1));
    expect(await screen.findByTestId("result-verdict")).toHaveTextContent("snapshot of round one");
  });

  it("jumps back to the live file, which is not the last snapshot", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1), mkRound(2)] });
    documentMock.mockImplementation(async (_id: string, version?: number) =>
      version === 1 ? mkDoc("snapshot of round one", 1) : mkDoc("newest text"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={2} />);

    await screen.findByTestId("result-verdict");
    fireEvent.click(screen.getByRole("button", { name: "Older version (round 1)" }));
    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g1", 1));

    documentMock.mockClear();
    fireEvent.click(screen.getByTestId("result-newest"));

    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g1"));
    expect(await screen.findByTestId("result-verdict")).toHaveTextContent("newest text");
  });

  it("drops the previous group's document immediately when the group changes", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1), mkRound(2)] });
    documentMock.mockImplementation(async (id: string, version?: number) => {
      if (id === "g1") return version === 1 ? mkDoc("g1 snapshot", 1) : mkDoc("g1 newest");
      return mkDoc("g2 newest");
    });
    const { rerender } = render(<ResultDocPanel groupId="g1" latestVersion={2} />);

    await waitFor(() =>
      expect(screen.getByTestId("result-verdict")).toHaveTextContent("g1 newest"),
    );
    fireEvent.click(screen.getByRole("button", { name: "Older version (round 1)" }));
    await waitFor(() =>
      expect(screen.getByTestId("result-verdict")).toHaveTextContent("g1 snapshot"),
    );

    documentMock.mockClear();
    rerender(<ResultDocPanel groupId="g2" latestVersion={2} />);

    // Kein einziger Frame mit dem Dokument der alten Gruppe unter dem neuen Kopf.
    expect(screen.queryByTestId("result-verdict")).not.toBeInTheDocument();

    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g2"));
    expect(documentMock).not.toHaveBeenCalledWith("g2", 1);
    await waitFor(() =>
      expect(screen.getByTestId("result-verdict")).toHaveTextContent("g2 newest"),
    );
  });

  it("shows the rewriting hint only while updating", async () => {
    documentMock.mockResolvedValue(mkDoc("body"));
    const { rerender } = render(<ResultDocPanel groupId="g1" latestVersion={1} />);

    await screen.findByTestId("result-verdict");
    expect(screen.queryByText("being rewritten…")).not.toBeInTheDocument();

    rerender(<ResultDocPanel groupId="g1" latestVersion={1} updating />);
    expect(screen.getByText("being rewritten…")).toBeInTheDocument();
  });

  it("copies the shown content to the clipboard and confirms for a moment", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    documentMock.mockResolvedValue(mkDoc("copy me"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByTestId("result-verdict");
    fireEvent.click(screen.getByTestId("result-copy"));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("copy me"));
    expect(await screen.findByText("Result copied")).toBeInTheDocument();
  });

  it("stays silent when the browser has no clipboard", async () => {
    Reflect.deleteProperty(navigator, "clipboard");
    documentMock.mockResolvedValue(mkDoc("copy me"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByTestId("result-verdict");
    fireEvent.click(screen.getByTestId("result-copy"));

    await waitFor(() => expect(screen.getByText("Copy")).toBeInTheDocument());
    expect(screen.queryByText("Result copied")).not.toBeInTheDocument();
  });
});
