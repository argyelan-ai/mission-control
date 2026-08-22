import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AgentCard } from "./AgentCard";
import type { SubagentRun, ToolEvent } from "@/lib/chatTypes";

const subagentHistory = vi.fn();
vi.mock("@/lib/api", () => ({
  api: { chat: { subagentHistory: (...a: unknown[]) => subagentHistory(...a) } },
}));

function wrap(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function spawn(overrides: Partial<ToolEvent> = {}): ToolEvent {
  return {
    kind: "tool",
    uuid: "u1",
    ts: "2026-08-22T10:00:00Z",
    name: "Agent",
    title: "Agent: Prueft den Zweig",
    detail: { name: "pruefer", description: "Prueft den Zweig", subagent_type: "reviewer" },
    toolUseId: "t1",
    result: "Spawned successfully.\nagent_id: pruefer@session-abc\nname: pruefer",
    status: "done",
    stats: null,
    sidechain: false,
    ...overrides,
  } as ToolEvent;
}

const RUN: SubagentRun = {
  runId: "apruefer", name: "pruefer", agentType: "reviewer",
  description: "Prueft den Zweig", model: "claude-opus-5", color: "green",
  teamName: "session-abc", startedAt: "2026-08-22T10:00:00Z",
};

beforeEach(() => {
  subagentHistory.mockReset();
  subagentHistory.mockResolvedValue({
    events: [{ kind: "message", uuid: "m1", ts: "t", role: "assistant", text: "bin fertig", model: null, sidechain: false }],
    session: { sessionId: "agent-apruefer", live: false, startedAt: null },
    hasMore: false,
    subagent: RUN,
  });
});

describe("AgentCard", () => {
  it("zeigt Name, Auftrag und Modell, ohne den Verlauf zu laden", () => {
    wrap(<AgentCard ev={spawn()} run={RUN} agentId="a1" />);

    expect(screen.getByText("pruefer")).toBeInTheDocument();
    expect(screen.getByText(/Prueft den Zweig/)).toBeInTheDocument();
    expect(screen.getByText("claude-opus-5")).toBeInTheDocument();
    // Erst beim Aufklappen — eine Sitzung kann Dutzende Auftraege haben.
    expect(subagentHistory).not.toHaveBeenCalled();
  });

  it("laedt das Protokoll genau einmal beim Aufklappen", async () => {
    wrap(<AgentCard ev={spawn()} run={RUN} agentId="a1" />);

    fireEvent.click(screen.getByTestId("agent-card-toggle"));

    await waitFor(() => expect(screen.getByText("bin fertig")).toBeInTheDocument());
    expect(subagentHistory).toHaveBeenCalledTimes(1);
    expect(subagentHistory).toHaveBeenCalledWith("a1", "apruefer");
  });

  it("bietet ohne zugeordneten Lauf gar kein Aufklappen an", () => {
    // Zu rund der Haelfte der Auftraege laesst sich kein Lauf sicher
    // zuordnen. Dann ist eine karge Karte richtig — ein Knopf, der das
    // Protokoll eines FREMDEN Auftrags oeffnet, waere falsch.
    wrap(<AgentCard ev={spawn()} agentId="a1" />);

    expect(screen.queryByTestId("agent-card-toggle")).not.toBeInTheDocument();
    expect(screen.getByTestId("agent-card-static")).toBeInTheDocument();
    // Der Name kommt dann aus dem Aufruf selbst.
    expect(screen.getByText("pruefer")).toBeInTheDocument();
  });

  it("druckt die Spawn-Metadaten NIE ab", () => {
    // Im Ergebnis des Aufrufs stehen interne Kennungen, deren Text sich
    // selbst als nicht zitierfaehig bezeichnet. Er wird als Zustand gedeutet,
    // nicht angezeigt.
    const { container } = wrap(<AgentCard ev={spawn()} run={RUN} agentId="a1" />);

    expect(container.textContent).not.toContain("agent_id");
    expect(container.textContent).not.toContain("session-abc");
    expect(container.textContent).not.toContain("Spawned successfully");
  });

  it("nennt einen laufenden Auftrag laufend und einen gescheiterten gescheitert", () => {
    const { rerender } = wrap(<AgentCard ev={spawn({ result: null })} run={RUN} agentId="a1" />);
    expect(screen.getByText(/running|läuft/i)).toBeInTheDocument();

    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    rerender(
      <QueryClientProvider client={qc}>
        <AgentCard ev={spawn({ status: "error" })} run={RUN} agentId="a1" />
      </QueryClientProvider>,
    );
    expect(screen.getByText(/failed|fehlgeschlagen/i)).toBeInTheDocument();
  });

  it("sagt es, wenn das Protokoll nicht zu holen ist", async () => {
    subagentHistory.mockRejectedValue(new Error("API 404: {\"reason\":\"no_transcript\"}"));
    wrap(<AgentCard ev={spawn()} run={RUN} agentId="a1" />);

    fireEvent.click(screen.getByTestId("agent-card-toggle"));

    await waitFor(() =>
      expect(screen.getByText(/no transcript of its own|kein eigenes Protokoll/i)).toBeInTheDocument(),
    );
  });
});
