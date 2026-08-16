"use client";

/**
 * SessionSidebar — Task B5. Groups the merged docker + host session list
 * (`sessions/page.tsx:494-506`'s `agents` array — this component never
 * fetches itself, B6 wires the queries) by project, so the chat view reads
 * as "which project is this agent working on" instead of a flat agent list.
 *
 * Grouping: agent → agent.current_task_id → task → task.project_id →
 * project group. An agent with no current task, or whose task isn't
 * project-bound, lands in the trailing "Ad-hoc" group — never dropped.
 *
 * `hasTranscript` is a lookup the caller supplies (B6), not something this
 * component derives — mirrors the backend's `transcript_allowed` fail-closed
 * gating (services/transcript_chat.py), which the UI must not re-implement.
 * Defaults to "everyone has a transcript" so the sidebar renders sensibly
 * standalone.
 */
import { useState } from "react";
import { ChevronDown, ChevronLeft, ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";
import { EntityIcon } from "@/components/shared/EntityIcon";
import type { Agent, AgentStatus, Task, Project } from "@/lib/types";

const ADHOC_KEY = "__adhoc__";
const ADHOC_LABEL = "Ad-hoc";

type DotStatus = "online" | "warning" | "error" | "busy" | "idle" | "offline";

// StatusDot only speaks the 6-value status vocabulary — collapse the wider
// AgentStatus union onto it instead of teaching StatusDot new states.
function toDotStatus(status: AgentStatus): DotStatus {
  switch (status) {
    case "provisioning":
    case "restarting":
      return "warning";
    case "archived":
      return "offline";
    default:
      return status;
  }
}

interface SessionGroup {
  key: string;
  label: string;
  agents: Agent[];
}

function buildGroups(agents: Agent[], tasks: Task[], projects: Project[]): SessionGroup[] {
  const taskById = new Map(tasks.map((t) => [t.id, t]));
  const projectById = new Map(projects.map((p) => [p.id, p]));
  const buckets = new Map<string, SessionGroup>();

  for (const agent of agents) {
    const task = agent.current_task_id ? taskById.get(agent.current_task_id) : undefined;
    const project = task?.project_id ? projectById.get(task.project_id) : undefined;
    const key = project ? project.id : ADHOC_KEY;
    const label = project ? project.name : ADHOC_LABEL;
    const bucket = buckets.get(key);
    if (bucket) {
      bucket.agents.push(agent);
    } else {
      buckets.set(key, { key, label, agents: [agent] });
    }
  }

  const groups = [...buckets.values()];
  groups.sort((a, b) => {
    if (a.key === ADHOC_KEY) return 1;
    if (b.key === ADHOC_KEY) return -1;
    return a.label.localeCompare(b.label, "de");
  });
  return groups;
}

function taskTitleFor(agent: Agent, tasks: Task[]): string | null {
  if (!agent.current_task_id) return null;
  return tasks.find((t) => t.id === agent.current_task_id)?.title ?? null;
}

interface SessionSidebarProps {
  agents: Agent[];
  tasks: Task[];
  projects: Project[];
  selectedId: string | null;
  onSelect: (agentId: string) => void;
  /** Rail = fixed desktop column. Sheet = collapsed dropdown for <768px. */
  variant?: "rail" | "sheet";
  /** Per-agent lookup; caller-supplied (B6). Omitted = assume every agent has one. */
  hasTranscript?: (agentId: string) => boolean;
  /** Rail-only — collapses the column to a slim icon-avatar strip. The sheet
   *  variant ignores this (it has its own collapsed-by-default toggle). */
  collapsed?: boolean;
  /** Presence of this prop is also what shows the collapse/expand chevron —
   *  omit it to render the rail without one (backward compatible). */
  onToggleCollapse?: () => void;
}

export function SessionSidebar({
  agents,
  tasks,
  projects,
  selectedId,
  onSelect,
  variant = "rail",
  hasTranscript = () => true,
  collapsed = false,
  onToggleCollapse,
}: SessionSidebarProps) {
  const [sheetOpen, setSheetOpen] = useState(false);
  const groups = buildGroups(agents, tasks, projects);
  const selectedAgent = agents.find((a) => a.id === selectedId) ?? null;

  function handleSelect(agentId: string) {
    onSelect(agentId);
    setSheetOpen(false);
  }

  const list = (
    <div role="listbox" aria-label="Sessions" className="flex flex-col gap-3">
      {groups.length === 0 && (
        <div className="px-3 py-6 text-[13px]" style={{ color: C.textMuted }}>
          Keine Sessions aktiv.
        </div>
      )}
      {groups.map((group) => (
        <div key={group.key}>
          <div className="label-sys px-3 pb-1.5 truncate" style={{ color: C.textDim }}>
            {group.label}
          </div>
          <div className="flex flex-col">
            {group.agents.map((agent) => {
              const taskTitle = taskTitleFor(agent, tasks);
              const selected = agent.id === selectedId;
              const showTerminalChip = !hasTranscript(agent.id);
              return (
                <button
                  key={agent.id}
                  type="button"
                  role="option"
                  aria-selected={selected}
                  onClick={() => handleSelect(agent.id)}
                  className="flex items-center gap-2 px-3 py-2 text-left w-full rounded transition-colors"
                  style={{ background: selected ? C.accentSubtle : "transparent" }}
                >
                  <StatusDot status={toDotStatus(agent.status)} size="sm" pulse={agent.status === "busy"} />
                  <span className="flex-1 min-w-0">
                    <span
                      className="block text-[13px] font-medium truncate"
                      style={{ color: selected ? C.textPrimary : C.textSecondary }}
                    >
                      {agent.name}
                    </span>
                    {taskTitle && (
                      <span className="block text-[12px] truncate" style={{ color: C.textMuted }}>
                        {taskTitle}
                      </span>
                    )}
                  </span>
                  {showTerminalChip && (
                    <span
                      className="shrink-0 text-[10px] font-mono px-1.5 py-0.5 rounded"
                      style={{ background: C.bgHover, color: C.textMuted, border: `1px solid ${C.border}` }}
                    >
                      Terminal
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );

  if (variant === "sheet") {
    return (
      <div className="w-full" style={{ background: C.bgSurface, borderBottom: `1px solid ${C.border}` }}>
        <button
          type="button"
          onClick={() => setSheetOpen((v) => !v)}
          aria-expanded={sheetOpen}
          className="w-full flex items-center gap-2 px-3 py-2.5"
        >
          {selectedAgent ? (
            <>
              <StatusDot status={toDotStatus(selectedAgent.status)} size="sm" />
              <span className="text-[13px] font-medium truncate" style={{ color: C.textPrimary }}>
                {selectedAgent.name}
              </span>
            </>
          ) : (
            <span className="text-[13px]" style={{ color: C.textMuted }}>
              Session wählen
            </span>
          )}
          <ChevronDown
            size={14}
            className="ml-auto shrink-0"
            style={{ color: C.textMuted, transform: sheetOpen ? "rotate(180deg)" : undefined }}
          />
        </button>
        {sheetOpen && (
          <div className="px-1 pb-2 max-h-[60vh] overflow-y-auto" style={{ borderTop: `1px solid ${C.border}` }}>
            {list}
          </div>
        )}
      </div>
    );
  }

  // Rail, collapsed: slim icon-avatar strip. Group headers make no sense at
  // this width — every agent renders flat, one icon button each, still fully
  // functional (title = name, click = onSelect).
  // No self-drawn right border — the only caller (sessions/page.tsx) wraps
  // both rail states in an island `div` that owns all four edges now (Codex-
  // island layout); drawing one here too would double up against it.
  if (collapsed) {
    return (
      <div
        className="w-14 shrink-0 h-full flex flex-col items-center py-3 gap-1 overflow-y-auto"
        style={{ background: C.bgSurface }}
      >
        {onToggleCollapse && (
          <button
            type="button"
            onClick={onToggleCollapse}
            aria-label="Seitenleiste ausklappen"
            title="Seitenleiste ausklappen"
            className="flex items-center justify-center w-9 h-9 rounded-md shrink-0 mb-1"
            style={{ color: C.textMuted }}
          >
            <ChevronRight size={14} />
          </button>
        )}
        <div role="listbox" aria-label="Sessions" className="flex flex-col items-center gap-1 w-full">
          {agents.map((agent) => {
            const selected = agent.id === selectedId;
            // Icon-only strip has no room for the open rail's "Terminal"
            // chip — the same information (no transcript, terminal-only
            // agent) folds into the title instead, never a visible chip.
            const title = hasTranscript(agent.id) ? agent.name : `${agent.name} — nur Terminal`;
            return (
              <button
                key={agent.id}
                type="button"
                role="option"
                aria-selected={selected}
                title={title}
                onClick={() => onSelect(agent.id)}
                className="relative flex items-center justify-center w-10 h-10 rounded-md shrink-0"
                style={{
                  background: selected ? C.accentSubtle : "transparent",
                  border: `1px solid ${selected ? C.borderAccent : "transparent"}`,
                }}
              >
                <EntityIcon value={agent.emoji} size={16} />
                <StatusDot
                  status={toDotStatus(agent.status)}
                  size="sm"
                  pulse={agent.status === "busy"}
                  className="absolute bottom-0.5 right-0.5"
                />
              </button>
            );
          })}
        </div>
      </div>
    );
  }

  // Same reasoning as the collapsed branch above — no self-drawn right
  // border, the page's island wrapper owns it.
  return (
    <div
      className="w-64 shrink-0 h-full flex flex-col"
      style={{ background: C.bgSurface }}
    >
      {onToggleCollapse && (
        <div
          className="flex items-center justify-end px-2 py-1.5 shrink-0"
          style={{ borderBottom: `1px solid ${C.border}` }}
        >
          <button
            type="button"
            onClick={onToggleCollapse}
            aria-label="Seitenleiste einklappen"
            title="Seitenleiste einklappen"
            className="flex items-center justify-center w-7 h-7 rounded-md"
            style={{ color: C.textMuted }}
          >
            <ChevronLeft size={14} />
          </button>
        </div>
      )}
      <div className="flex-1 overflow-y-auto py-3">{list}</div>
    </div>
  );
}
