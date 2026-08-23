/**
 * FinishGroupDialog — vitest (ADR-075, Nachtrag 22.08.2026).
 *
 * Der Kern, den diese Datei bewachen soll: Merken und Aufräumen sind zwei
 * getrennte Entscheidungen. Wer alles löscht, darf die Erkenntnis behalten.
 *
 * `src/test-setup.ts` mockt next-intl gegen messages/en.json — geprüft werden
 * daher die englischen Beschriftungen.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { FinishGroupDialog } from "./FinishGroupDialog";
import { api } from "@/lib/api";
import type { GroupDetail } from "@/lib/groupTypes";

vi.mock("@/lib/api", () => ({
  api: { groups: { memorize: vi.fn(), archive: vi.fn(), remove: vi.fn() } },
}));
vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

const memorizeMock = vi.mocked(api.groups.memorize);
const archiveMock = vi.mocked(api.groups.archive);
const removeMock = vi.mocked(api.groups.remove);

function mkGroup(overrides: Partial<GroupDetail> = {}): GroupDetail {
  return {
    id: "g1",
    thread_id: "t1",
    name: "Spark-Standard",
    goal: "Motor entscheiden",
    lifecycle: "one_shot",
    status: "done",
    lead_agent_id: "a1",
    max_rounds: 3,
    max_duration_minutes: null,
    budget_usd: null,
    budget_tokens: null,
    rounds_completed: 2,
    current_round_no: 2,
    result_doc_rel_path: "groups/x/result.md",
    created_at: "2026-08-22T10:00:00Z",
    members: [],
    ...overrides,
  } as GroupDetail;
}

function renderDialog(props: Partial<Parameters<typeof FinishGroupDialog>[0]> = {}) {
  const onClose = vi.fn();
  const onGone = vi.fn();
  const onChanged = vi.fn();
  render(
    <FinishGroupDialog
      open
      group={props.group ?? mkGroup()}
      messageCount={props.messageCount ?? 7}
      onClose={onClose}
      onGone={onGone}
      onChanged={onChanged}
    />,
  );
  return { onClose, onGone, onChanged };
}

beforeEach(() => {
  vi.clearAllMocks();
  memorizeMock.mockResolvedValue({ memory_id: "m1", title: "Spark-Standard" });
  archiveMock.mockResolvedValue(mkGroup({ archived_at: "2026-08-22T12:00:00Z" }));
  removeMock.mockResolvedValue(mkGroup({ archived_at: "2026-08-22T12:00:00Z" }));
});

describe("FinishGroupDialog", () => {
  it("steht auf Archivieren, nicht auf Löschen", async () => {
    // Ein Dialog, der auf „endgültig löschen" vorausgewählt ist, wartet nur
    // auf einen Fehlgriff — besonders auf dem Handy.
    renderDialog();
    expect(screen.getByTestId("finish-action-archive")).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByTestId("finish-action-delete_all")).toHaveAttribute("aria-pressed", "false");
  });

  it("merkt das Ergebnis und archiviert danach", async () => {
    const user = userEvent.setup();
    const { onChanged, onClose } = renderDialog();

    await user.click(screen.getByTestId("finish-confirm"));

    await waitFor(() => expect(memorizeMock).toHaveBeenCalled());
    expect(memorizeMock.mock.calls[0][0]).toBe("g1");
    expect(archiveMock).toHaveBeenCalledWith("g1");
    expect(removeMock).not.toHaveBeenCalled();
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
    expect(onClose).toHaveBeenCalled();
  });

  it("behält die Erkenntnis, wenn alles gelöscht wird", async () => {
    // Das ist der eigentliche Punkt der ganzen Funktion.
    const user = userEvent.setup();
    const { onGone } = renderDialog();

    await user.click(screen.getByTestId("finish-action-delete_all"));
    await user.click(screen.getByTestId("finish-confirm"));

    await waitFor(() => expect(removeMock).toHaveBeenCalledWith("g1", "all"));
    // Gemerkt wurde VOR dem Löschen — sonst wäre die Quelle beim Schreiben weg.
    expect(memorizeMock).toHaveBeenCalled();
    expect(memorizeMock.mock.invocationCallOrder[0]).toBeLessThan(
      removeMock.mock.invocationCallOrder[0],
    );
    await waitFor(() => expect(onGone).toHaveBeenCalled());
  });

  it("löscht nichts, wenn das Merken scheitert", async () => {
    // Sonst verlöre man beides: die Notiz UND das Material, aus dem man sie
    // hätte neu schreiben können.
    const user = userEvent.setup();
    memorizeMock.mockRejectedValue(new Error("API 422: kein Ergebnis"));
    const { onGone } = renderDialog();

    await user.click(screen.getByTestId("finish-action-delete_all"));
    await user.click(screen.getByTestId("finish-confirm"));

    await waitFor(() => expect(memorizeMock).toHaveBeenCalled());
    expect(removeMock).not.toHaveBeenCalled();
    expect(onGone).not.toHaveBeenCalled();
  });

  it("räumt ohne zu merken, wenn der Haken weg ist", async () => {
    const user = userEvent.setup();
    renderDialog();

    await user.click(screen.getByTestId("finish-memorize")); // Haken raus
    await user.click(screen.getByTestId("finish-action-delete_chat"));
    await user.click(screen.getByTestId("finish-confirm"));

    await waitFor(() => expect(removeMock).toHaveBeenCalledWith("g1", "chat"));
    expect(memorizeMock).not.toHaveBeenCalled();
  });

  it("bietet die Memory-Felder nur an, solange gemerkt wird", async () => {
    const user = userEvent.setup();
    renderDialog();

    expect(screen.getByTestId("finish-memory-title")).toBeInTheDocument();
    await user.click(screen.getByTestId("finish-memorize"));
    expect(screen.queryByTestId("finish-memory-title")).not.toBeInTheDocument();
  });
});
