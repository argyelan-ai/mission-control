"use client";

import { useMemo } from "react";
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
    refetchInterval: poll,
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
