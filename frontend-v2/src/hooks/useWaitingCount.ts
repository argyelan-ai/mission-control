"use client";

import { useInbox } from "@/hooks/useInbox";
import { useFailedJourneys } from "@/components/home/JourneysLine";

/**
 * The one number next to Chat (desktop sidebar and phone tab bar): what waits
 * on the operator in the inbox ("For you") — approvals, reviews, head
 * questions — plus last night's failed journey tests. The inbox tab shows the
 * same sum, so the badge always matches the page it leads to.
 */
export function useWaitingCount(): number {
  const { count } = useInbox();
  const { count: journeys } = useFailedJourneys();
  return count + journeys;
}
