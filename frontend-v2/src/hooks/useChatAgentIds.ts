"use client";

import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";

/**
 * The agents that have a chat — exactly the ones the chat list shows: the
 * docker-session and host-session agents, archived ones left out by the
 * backend. Same query keys as the chat page and `useChatList`, so React
 * Query serves one set of requests to all of them.
 *
 * A link to `/sessions?agent=<id>` is only right for an id in this set: the
 * chat page cannot open an agent it does not list (an archived one, a
 * non-session runtime) and would land on another chat instead.
 *
 * `null` while neither list has loaded — callers render no link then.
 */
export function useChatAgentIds(): Set<string> | null {
  const { data: docker, isSuccess: dockerOk } = useQuery({
    queryKey: ["agents", "docker-sessions"],
    queryFn: () => api.agents.listDockerSessions(),
  });
  const { data: host, isSuccess: hostOk } = useQuery({
    queryKey: ["agents", "host-sessions"],
    queryFn: () => api.agents.listHostSessions(),
  });
  return useMemo(() => {
    if (!dockerOk && !hostOk) return null;
    const ids = new Set<string>();
    for (const a of Array.isArray(docker) ? docker : []) ids.add(a.id);
    for (const a of Array.isArray(host) ? host : []) ids.add(a.id);
    return ids;
  }, [docker, host, dockerOk, hostOk]);
}
