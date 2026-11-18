"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { buildChatList, sectionChats, type ChatListSections } from "@/lib/chatList";
import { useInbox } from "@/hooks/useInbox";
import { useRecentChats } from "./MobileTabBar";

/**
 * The chats of the drawer and the desktop column, loaded once. The query keys
 * are the sessions page's own (`["agents", "docker-sessions"]`, …), so on the
 * chat page React Query serves one set of requests to both. Approvals and
 * head questions come from `useInbox` — the same source as the tab badge, so
 * "Waiting for you" and the number on the Chat tab never disagree about
 * approvals and questions.
 */
export function useChatList(): ChatListSections & { waitingCount: number; loaded: boolean } {
  const { data: dockerAgents, isSuccess: dockerLoaded } = useQuery({
    queryKey: ["agents", "docker-sessions"],
    queryFn: () => api.agents.listDockerSessions(),
    refetchInterval: 10_000,
  });
  const { data: hostAgents, isSuccess: hostLoaded } = useQuery({
    queryKey: ["agents", "host-sessions"],
    queryFn: () => api.agents.listHostSessions(),
    refetchInterval: 10_000,
  });
  const { data: groups } = useQuery({
    queryKey: ["groups"],
    queryFn: () => api.groups.list({ includeArchived: true }),
    refetchInterval: 10_000,
  });
  const inbox = useInbox();
  const recent = useRecentChats();

  const sections = useMemo(
    () =>
      sectionChats(
        buildChatList({
          agents: [...(Array.isArray(dockerAgents) ? dockerAgents : []), ...(Array.isArray(hostAgents) ? hostAgents : [])],
          groups: (Array.isArray(groups) ? groups : []).filter((g) => !g.archived_at),
          headRuns: inbox.headQuestions,
          approvals: inbox.approvals,
          recent,
        }),
      ),
    [dockerAgents, hostAgents, groups, inbox.headQuestions, inbox.approvals, recent],
  );

  return { ...sections, waitingCount: inbox.count, loaded: dockerLoaded || hostLoaded };
}
