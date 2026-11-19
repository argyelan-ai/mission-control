import type { Task } from "@/lib/types";

/**
 * Who a card belongs to for the operator's agent filter (round-2 Q5,
 * t4 S-neu-4): the agent holding it now, or the agent that delivered it.
 * The hand-off to review clears `assigned_agent_id` (operator review) or
 * moves it to the reviewer — without the deliverer, the filter showed one of
 * an agent's three cards waiting for review.
 */
export function taskMatchesAgent(
  task: Pick<Task, "assigned_agent_id" | "delivered_by_agent_id">,
  agentId: string,
): boolean {
  return task.assigned_agent_id === agentId || task.delivered_by_agent_id === agentId;
}
