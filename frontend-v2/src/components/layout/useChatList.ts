"use client";

import { useMemo, useSyncExternalStore } from "react";
import { useQuery } from "@tanstack/react-query";
import { usePathname } from "next/navigation";
import { api } from "@/lib/api";
import { buildChatList, sectionChats, type ChatListSections } from "@/lib/chatList";
import { useInbox } from "@/hooks/useInbox";

/**
 * The chats of the drawer and the desktop column, loaded once. The query keys
 * are the sessions page's own (`["agents", "docker-sessions"]`, …), so on the
 * chat page React Query serves one set of requests to both. Approvals and
 * head questions come from `useInbox` — the same source as the tab badge, so
 * "Waiting for you" and the number on the Chat tab never disagree about
 * approvals and questions.
 */
/**
 * How often the chat list asks again: as before on the chat page, much
 * calmer everywhere else (shell review E1 — the docker-sessions list inspects
 * every container, and the desktop column now sits on every page).
 */
export function chatListPollMs(pathname: string): number {
  return pathname.startsWith("/sessions") ? 10_000 : 60_000;
}

/**
 * The record answers 404 while CHAT_RECORD_ENABLED is off: then it is not
 * asked again (one request per page load, not one per poll). Any other
 * failure keeps the poll — the record may be back on the next one.
 */
export function recordPollMs(error: unknown, poll: number): number | false {
  return error instanceof Error && error.message.startsWith("API 404") ? false : poll;
}

/**
 * The further chat open on the chat page right now (chats slice S2), so the
 * list marks ITS row and not the agent's: the agent row and its further
 * chats share one agent id, and the shell has no `?chat=` of its own (it
 * sits on every page, outside the sessions page's Suspense boundary).
 * `ChatView` sets it while a further chat is open and clears it on leave.
 */
let openChatId: string | null = null;
const openChatListeners = new Set<() => void>();

export function setOpenChat(id: string | null): void {
  if (openChatId === id) return;
  openChatId = id;
  for (const l of openChatListeners) l();
}

function subscribeOpenChat(l: () => void): () => void {
  openChatListeners.add(l);
  return () => openChatListeners.delete(l);
}

export function useOpenChat(): string | null {
  return useSyncExternalStore(subscribeOpenChat, () => openChatId, () => null);
}

/** The key of the row to mark: the open further chat, else the chat the
 *  sessions page opened last; nothing off the chat page. */
export function activeChatKey(
  pathname: string,
  recent: { kind: string; id: string } | undefined,
  openChat: string | null,
): string | null {
  if (!pathname.startsWith("/sessions")) return null;
  if (openChat) return `chat:${openChat}`;
  return recent ? `${recent.kind}:${recent.id}` : null;
}

export function useChatList(): ChatListSections & { waitingCount: number; loaded: boolean } {
  const poll = chatListPollMs(usePathname() ?? "/");
  const { data: dockerAgents, isSuccess: dockerLoaded } = useQuery({
    queryKey: ["agents", "docker-sessions"],
    queryFn: () => api.agents.listDockerSessions(),
    refetchInterval: poll,
  });
  const { data: hostAgents, isSuccess: hostLoaded } = useQuery({
    queryKey: ["agents", "host-sessions"],
    queryFn: () => api.agents.listHostSessions(),
    refetchInterval: poll,
  });
  const { data: groups } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api.groups.list({ includeArchived: true }),
    refetchInterval: poll,
  });
  // Further chats of an agent (chats slice S2). The record answers 404
  // while its flag is off: then the list is exactly the one before.
  const { data: record } = useQuery({
    queryKey: ["chat-record", "chats"],
    queryFn: () => api.chatRecord.listChats(),
    refetchInterval: (query) => recordPollMs(query.state.error, poll),
    retry: false,
  });
  const inbox = useInbox();

  const sections = useMemo(
    () =>
      sectionChats(
        buildChatList({
          agents: [...(Array.isArray(dockerAgents) ? dockerAgents : []), ...(Array.isArray(hostAgents) ? hostAgents : [])],
          groups: (Array.isArray(groups) ? groups : []).filter((g) => !g.archived_at),
          headRuns: inbox.headQuestions,
          approvals: inbox.approvals,
          recordChats: Array.isArray(record?.chats) ? record.chats : [],
        }),
      ),
    [dockerAgents, hostAgents, groups, record, inbox.headQuestions, inbox.approvals],
  );

  return { ...sections, waitingCount: inbox.count, loaded: dockerLoaded || hostLoaded };
}
