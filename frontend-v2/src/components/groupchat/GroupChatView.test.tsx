/**
 * GroupChatView — vitest der Integrationsschicht (ADR-075).
 *
 * Geprüft wird, was der Raum ZUSAMMENSETZT: Kopf mit Ziel, Steuerknöpfe je
 * Status, Runden-Trenner an der richtigen Nachricht, Gate-Karte nur beim
 * Warten, und dass Senden am Strom-Haken landet. Die Bausteine selbst haben
 * ihre eigenen Tests.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import type { GroupDetail, GroupMessage, GroupRoundInfo } from "@/lib/groupTypes";
import { EMPTY_GROUP_STREAM_STATE } from "@/lib/groupTypes";

const send = vi.fn();
const streamState = {
  messages: [] as GroupMessage[],
  state: { ...EMPTY_GROUP_STREAM_STATE },
  connected: true,
  loading: false,
  error: false,
  hasMoreBefore: false,
  send,
  sending: false,
  loadOlder: vi.fn(),
  loadingOlder: false,
  docVersion: null as number | null,
  refresh: vi.fn(),
};

vi.mock("@/hooks/useGroupStream", () => ({
  useGroupStream: () => streamState,
}));

const rounds: { rounds: GroupRoundInfo[] } = { rounds: [] };
const startMock = vi.fn();
const pauseMock = vi.fn();
const stopMock = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    groups: {
      rounds: () => Promise.resolve(rounds),
      start: (...args: unknown[]) => startMock(...args),
      pause: (...args: unknown[]) => pauseMock(...args),
      stop: (...args: unknown[]) => stopMock(...args),
      document: () => Promise.resolve({ rel_path: "x", content: "", version: null }),
    },
  },
}));

import { GroupChatView } from "./GroupChatView";

function mkGroup(overrides: Partial<GroupDetail> = {}): GroupDetail {
  return {
    id: "g1",
    thread_id: "t1",
    name: "Spark-Runde",
    goal: "DFlash2 vs vLLM entscheiden",
    status: "idle",
    lifecycle: "one_shot",
    lead_agent_id: "a1",
    max_rounds: 3,
    max_duration_minutes: null,
    budget_usd: null,
    budget_tokens: null,
    rounds_completed: 0,
    current_round_no: 0,
    result_doc_rel_path: "groups/spark/result.md",
    created_at: "2026-08-20T10:00:00Z",
    members: [
      { id: "a1", name: "Alpha", slug: "alpha", emoji: null, role: "lead", archived: false },
      { id: "a2", name: "Beta", slug: "beta", emoji: null, role: "member", archived: false },
    ],
    ...overrides,
  };
}

function mkMessage(overrides: Partial<GroupMessage> = {}): GroupMessage {
  return {
    id: `m${overrides.seq ?? 1}`,
    thread_id: "t1",
    seq: 1,
    sender_type: "agent",
    sender_id: "a2",
    message_type: "message",
    body: "Beitrag mit Quelle",
    mentions: [],
    created_at: "2026-08-20T10:01:00Z",
    ...overrides,
  };
}

beforeEach(() => {
  vi.clearAllMocks();
  streamState.messages = [];
  streamState.state = { ...EMPTY_GROUP_STREAM_STATE };
  streamState.connected = true;
  streamState.docVersion = null;
  rounds.rounds = [];
  startMock.mockResolvedValue(mkGroup({ status: "running" }));
  pauseMock.mockResolvedValue(mkGroup({ status: "paused" }));
  stopMock.mockResolvedValue(mkGroup({ status: "done" }));
});

describe("GroupChatView", () => {
  it("shows the group name and its goal in the header", () => {
    render(<GroupChatView group={mkGroup()} onGroupChanged={vi.fn()} />);
    expect(screen.getByText("Spark-Runde")).toBeInTheDocument();
    expect(screen.getByText(/DFlash2 vs vLLM entscheiden/)).toBeInTheDocument();
  });

  it("offers the first round in the empty room and starts it", async () => {
    const onChanged = vi.fn();
    render(<GroupChatView group={mkGroup()} onGroupChanged={onChanged} />);
    fireEvent.click(screen.getByRole("button", { name: "Start the first round" }));
    await waitFor(() => expect(startMock).toHaveBeenCalledWith("g1"));
    await waitFor(() => expect(onChanged).toHaveBeenCalled());
  });

  it("shows pause while running and start while paused — never both", () => {
    const { rerender } = render(
      <GroupChatView group={mkGroup({ status: "running" })} onGroupChanged={vi.fn()} />,
    );
    expect(screen.getByRole("button", { name: "Pause" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Start rounds" })).not.toBeInTheDocument();

    rerender(<GroupChatView group={mkGroup({ status: "paused" })} onGroupChanged={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Resume" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Pause" })).not.toBeInTheDocument();
  });

  it("asks before stopping and only then calls the API", async () => {
    render(<GroupChatView group={mkGroup({ status: "running" })} onGroupChanged={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Stop" }));
    expect(stopMock).not.toHaveBeenCalled();
    expect(
      screen.getByText("The assignment gets closed. Transcript and result stay readable."),
    ).toBeInTheDocument();
    fireEvent.click(screen.getAllByRole("button", { name: "Stop" })[1]);
    await waitFor(() => expect(stopMock).toHaveBeenCalledWith("g1"));
  });

  it("puts a round divider in front of the round's brief message", async () => {
    streamState.messages = [
      mkMessage({ seq: 1, sender_type: "user", sender_id: null, body: "los gehts" }),
      mkMessage({ seq: 2, sender_type: "system", sender_id: null, body: "# Runden-Brief" }),
    ];
    rounds.rounds = [
      {
        id: "r1", round_no: 1, kind: "autonomous", brief_seq: 2, outcome: null, report: null,
        pending_speakers: [], has_doc_snapshot: false, tokens_used: null, cost_usd: null,
        started_at: null, finished_at: null,
      },
    ];
    render(<GroupChatView group={mkGroup({ status: "running" })} onGroupChanged={vi.fn()} />);
    await waitFor(() => expect(screen.getByText(/Round 1/)).toBeInTheDocument());
  });

  it("shows the gate card only while the group waits for the operator", () => {
    streamState.state = { ...EMPTY_GROUP_STREAM_STATE, gateQuestion: "1M-Kontext wichtiger?" };
    const { rerender } = render(
      <GroupChatView group={mkGroup({ status: "waiting_gate" })} onGroupChanged={vi.fn()} />,
    );
    expect(screen.getByText("The group is asking you")).toBeInTheDocument();
    expect(screen.getByText("1M-Kontext wichtiger?")).toBeInTheDocument();

    rerender(<GroupChatView group={mkGroup({ status: "running" })} onGroupChanged={vi.fn()} />);
    expect(screen.queryByText("The group is asking you")).not.toBeInTheDocument();
  });

  it("hands a typed message to the stream", async () => {
    render(<GroupChatView group={mkGroup()} onGroupChanged={vi.fn()} />);
    const box = screen.getByRole("textbox");
    fireEvent.change(box, { target: { value: "@beta was meinst du?" } });
    fireEvent.keyDown(box, { key: "Enter" });
    await waitFor(() => expect(send).toHaveBeenCalledWith("@beta was meinst du?"));
  });

  it("stays truthful when the stream is gone", () => {
    streamState.connected = false;
    render(<GroupChatView group={mkGroup({ status: "running" })} onGroupChanged={vi.fn()} />);
    expect(screen.getByText("Status unknown — connection lost")).toBeInTheDocument();
  });
});
