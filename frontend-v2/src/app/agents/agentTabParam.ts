/** Tabs of the agent detail page (/agents/[id]) and their URL form. */
export const AGENT_TABS = ["overview", "skills", "mcp", "config", "memory", "local-memory"] as const;
export type AgentTab = (typeof AGENT_TABS)[number];

/** `?tab=` value → a real tab; anything unknown lands on Overview. */
export function agentTabFromParam(param: string | null | undefined): AgentTab {
  return (AGENT_TABS as readonly string[]).includes(param ?? "") ? (param as AgentTab) : "overview";
}

/** Detail URL for a tab; the default tab keeps the bare URL. */
export function agentTabHref(agentId: string, tab: AgentTab): string {
  return tab === "overview" ? `/agents/${agentId}` : `/agents/${agentId}?tab=${tab}`;
}
