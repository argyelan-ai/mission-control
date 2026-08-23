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
  it("renders the current document content as markdown", async () => {
    documentMock.mockResolvedValue(mkDoc("# Verdict\n\nDFlash2 wins on throughput."));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    expect(await screen.findByText("Verdict")).toBeInTheDocument();
    expect(screen.getByText("DFlash2 wins on throughput.")).toBeInTheDocument();
    expect(documentMock).toHaveBeenCalledWith("g1");
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
    expect(screen.queryByText(/Version/)).not.toBeInTheDocument();
  });

  it("hides the version stepper while no round has a snapshot", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1, false)] });
    documentMock.mockResolvedValue(mkDoc("live text"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    expect(await screen.findByText("live text")).toBeInTheDocument();
    expect(screen.queryByTestId("result-prev")).not.toBeInTheDocument();
  });

  it("stepping back loads the older round snapshot", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1), mkRound(2)] });
    documentMock.mockImplementation(async (_id: string, version?: number) =>
      version === 1 ? mkDoc("snapshot of round one", 1) : mkDoc("newest text"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={2} />);

    expect(await screen.findByText("newest text")).toBeInTheDocument();
    expect(await screen.findByText("Version 2 of 2")).toBeInTheDocument();

    fireEvent.click(screen.getByTestId("result-prev"));

    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g1", 1));
    expect(await screen.findByText("snapshot of round one")).toBeInTheDocument();
    expect(screen.getByText("Version 1 of 2")).toBeInTheDocument();
  });

  it("shows the older-version band and jumps back to the newest file", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1), mkRound(2)] });
    documentMock.mockImplementation(async (_id: string, version?: number) =>
      version === 1 ? mkDoc("snapshot of round one", 1) : mkDoc("newest text"),
    );
    render(<ResultDocPanel groupId="g1" latestVersion={2} />);

    await screen.findByText("newest text");
    fireEvent.click(screen.getByTestId("result-prev"));

    expect(await screen.findByText("Older version (round 1)")).toBeInTheDocument();

    documentMock.mockClear();
    fireEvent.click(screen.getByTestId("result-newest"));

    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g1"));
    expect(await screen.findByText("newest text")).toBeInTheDocument();
    expect(screen.queryByText("Older version (round 1)")).not.toBeInTheDocument();
  });

  it("drops the previous group's document immediately when the group changes", async () => {
    roundsMock.mockResolvedValue({ rounds: [mkRound(1), mkRound(2)] });
    documentMock.mockImplementation(async (id: string, version?: number) => {
      if (id === "g1") return version === 1 ? mkDoc("g1 snapshot", 1) : mkDoc("g1 newest");
      return mkDoc("g2 newest");
    });
    const { rerender } = render(<ResultDocPanel groupId="g1" latestVersion={2} />);

    await screen.findByText("g1 newest");
    fireEvent.click(screen.getByTestId("result-prev"));
    await screen.findByText("g1 snapshot");

    documentMock.mockClear();
    rerender(<ResultDocPanel groupId="g2" latestVersion={2} />);

    // Kein einziger Frame mit dem Dokument der alten Gruppe unter dem neuen Kopf.
    expect(screen.queryByText("g1 snapshot")).not.toBeInTheDocument();
    expect(screen.queryByText("Older version (round 1)")).not.toBeInTheDocument();

    await waitFor(() => expect(documentMock).toHaveBeenCalledWith("g2"));
    expect(documentMock).not.toHaveBeenCalledWith("g2", 1);
    expect(await screen.findByText("g2 newest")).toBeInTheDocument();
  });

  it("shows the rewriting hint only while updating", async () => {
    documentMock.mockResolvedValue(mkDoc("body"));
    const { rerender } = render(<ResultDocPanel groupId="g1" latestVersion={1} />);

    await screen.findByText("body");
    expect(screen.queryByText("being rewritten…")).not.toBeInTheDocument();

    rerender(<ResultDocPanel groupId="g1" latestVersion={1} updating />);
    expect(screen.getByText("being rewritten…")).toBeInTheDocument();
  });

  it("copies the shown content to the clipboard and confirms for a moment", async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.defineProperty(navigator, "clipboard", { configurable: true, value: { writeText } });
    documentMock.mockResolvedValue(mkDoc("copy me"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByText("copy me");
    fireEvent.click(screen.getByTestId("result-copy"));

    await waitFor(() => expect(writeText).toHaveBeenCalledWith("copy me"));
    expect(await screen.findByText("Result copied")).toBeInTheDocument();
  });

  it("stays silent when the browser has no clipboard", async () => {
    Reflect.deleteProperty(navigator, "clipboard");
    documentMock.mockResolvedValue(mkDoc("copy me"));
    render(<ResultDocPanel groupId="g1" latestVersion={null} />);

    await screen.findByText("copy me");
    fireEvent.click(screen.getByTestId("result-copy"));

    await waitFor(() => expect(screen.getByText("Copy")).toBeInTheDocument());
    expect(screen.queryByText("Result copied")).not.toBeInTheDocument();
  });
});
