/**
 * ChatChangesInline — the "files changed in this chat" row under the agent's
 * latest answer. Same data as the Changes panel's "This chat" tab
 * (`GET /agents/{id}/chat/changes`); per-chat totals only, so the wording
 * says "in this chat". Shown only when files really changed and the turn is
 * over; tapping opens the Changes panel on "This chat".
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ChatChangesInline } from "./ChatChangesInline";
import { api } from "@/lib/api";
import type { ChatChanges } from "@/lib/chatTypes";
import de from "../../../messages/de.json";

vi.mock("@/lib/api", () => ({
  api: { chat: { changes: vi.fn() } },
}));
const changesMock = vi.mocked(api.chat.changes);

function changes(files: number, additions: number, deletions: number): ChatChanges {
  return { files: [], stats: { files, additions, deletions }, repo: null };
}

function renderInline(
  props: Partial<Parameters<typeof ChatChangesInline>[0]> = {},
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } }),
) {
  const onOpen = vi.fn();
  render(
    <QueryClientProvider client={client}>
      <ChatChangesInline agentId="agent-1" working={false} onOpen={onOpen} {...props} />
    </QueryClientProvider>,
  );
  return { onOpen, client };
}

/** Waits until the query has its answer (data or error), not just its call. */
async function settled(client: QueryClient) {
  await waitFor(() => expect(client.getQueryState(["chat-changes", "agent-1"])?.status).not.toBe("pending"));
}

beforeEach(() => {
  changesMock.mockReset();
  localStorage.clear();
});

describe("ChatChangesInline", () => {
  it("names the chat's changed files with their totals and opens the Changes panel on 'This chat'", async () => {
    changesMock.mockResolvedValue(changes(3, 71, 2));
    localStorage.setItem("mc.chat.changestab", "worktree");
    const user = userEvent.setup();
    const { onOpen } = renderInline();

    const row = await screen.findByTestId("chat-changes-inline");
    expect(row).toHaveTextContent("3 files changed in this chat");
    expect(row).toHaveTextContent("+71");
    expect(row).toHaveTextContent("−2");
    expect(row).toHaveTextContent("Open diff");

    await user.click(row);
    expect(onOpen).toHaveBeenCalledTimes(1);
    expect(localStorage.getItem("mc.chat.changestab")).toBe("chat");
  });

  // Operator pictures 18.11.2026: "1 Datei in diesem Chat geä…" at 390 px.
  it("drops the 'Open diff' word on a phone so the file line is not cut", async () => {
    changesMock.mockResolvedValue(changes(1, 521, 0));
    renderInline();
    const word = await screen.findByTestId("chat-changes-open-word");
    expect(word.className.split(/\s+/)).toEqual(expect.arrayContaining(["hidden", "sm:inline"]));
  });

  it("shows nothing when no file changed", async () => {
    changesMock.mockResolvedValue(changes(0, 0, 0));
    const { client } = renderInline();
    await settled(client);
    expect(screen.queryByTestId("chat-changes-inline")).toBeNull();
  });

  it("shows nothing (and asks nothing) while the agent is still working", () => {
    changesMock.mockResolvedValue(changes(3, 71, 2));
    renderInline({ working: true });
    expect(changesMock).not.toHaveBeenCalled();
    expect(screen.queryByTestId("chat-changes-inline")).toBeNull();
  });

  it("shows nothing while working even when the Changes panel already filled the shared cache", () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["chat-changes", "agent-1"], changes(3, 71, 2));
    renderInline({ working: true }, client);
    expect(screen.queryByTestId("chat-changes-inline")).toBeNull();
  });

  it("shows nothing when the list could not be loaded", async () => {
    changesMock.mockRejectedValue(new Error("API 500"));
    const { client } = renderInline();
    await settled(client);
    expect(screen.queryByTestId("chat-changes-inline")).toBeNull();
  });

  it("drops old numbers when the fresh load fails — no claim it cannot back", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    client.setQueryData(["chat-changes", "agent-1"], changes(3, 71, 2));
    changesMock.mockRejectedValue(new Error("API 500"));
    renderInline({}, client);
    await waitFor(() => expect(client.getQueryState(["chat-changes", "agent-1"])?.status).toBe("error"));
    expect(screen.queryByTestId("chat-changes-inline")).toBeNull();
  });

  it("has the German wording in the catalog", () => {
    expect(de.sessions.diff.inlineFiles).toContain("in diesem Chat geändert");
    expect(de.sessions.diff.inlineOpen).toBe("Diff öffnen");
  });
});
