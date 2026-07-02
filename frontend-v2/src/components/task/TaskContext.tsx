"use client";

import { useState, useRef, useEffect } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { ChevronRight, ChevronDown, MessageCircle, ArrowUpRight, GitBranch, Lock } from "lucide-react";
import { api } from "@/lib/api";
import { Pill } from "@/components/shared/Pill";
import type { Task, Agent, TaskStatus } from "@/lib/types";
import { C, LANE } from "@/lib/colors";

// Suppress unused import warning — useMutation/useQueryClient used in parent
const _unused = { useMutation, useQueryClient };
void _unused;

// ── Status color helper ──────────────────────────────────────────────────────

function statusDotColor(status: TaskStatus): string {
  return LANE[status] ?? C.textMuted;
}

const STATUS_LABELS: Record<TaskStatus, { color: string; label: string }> = {
  inbox: { color: LANE.inbox, label: "Inbox" },
  in_progress: { color: LANE.in_progress, label: "Active" },
  review: { color: LANE.review, label: "Review" },
  user_test: { color: LANE.user_test, label: "User Test" },
  done: { color: LANE.done, label: "Done" },
  blocked: { color: LANE.blocked, label: "Blocked" },
  failed: { color: LANE.failed, label: "Failed" },
  aborted: { color: LANE.aborted, label: "Aborted" },
};

// ── Agent Assignment Dropdown ────────────────────────────────────────────────

function AgentAssignDropdown({
  agents,
  currentAgentId,
  onAssign,
}: {
  agents: Agent[];
  currentAgentId: string | null;
  onAssign: (agentId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const current = agents.find((a) => a.id === currentAgentId);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <div
        className="text-[10px] font-semibold uppercase tracking-[0.06em] mb-1"
        style={{ color: C.textMuted }}
      >
        Assigned to
      </div>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-2.5 py-2 rounded-lg text-sm transition-all cursor-pointer"
        style={{
          backgroundColor: open ? "rgba(255, 255, 255, 0.05)" : "rgba(255, 255, 255, 0.02)",
          color: C.textPrimary,
          border: `1px solid ${open ? C.borderActive : C.border}`,
        }}
      >
        <span className="text-base">{current?.emoji || ""}</span>
        <span className="flex-1 text-left text-[13px]">
          {current?.name || "Nicht zugewiesen"}
        </span>
        <ChevronDown
          size={14}
          className="transition-transform"
          style={{
            color: C.textMuted,
            transform: open ? "rotate(180deg)" : "rotate(0deg)",
          }}
        />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.15 }}
            className="absolute left-0 right-0 z-50 mt-1 rounded-lg overflow-hidden"
            style={{
              backgroundColor: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {agents.map((a) => {
              const isActive = a.id === currentAgentId;
              return (
                <button
                  key={a.id}
                  onClick={() => {
                    if (!isActive) onAssign(a.id);
                    setOpen(false);
                  }}
                  className="flex items-center gap-2 w-full px-3 py-2 text-sm transition-colors cursor-pointer"
                  style={{
                    backgroundColor: isActive ? C.accentSubtle : "transparent",
                    color: isActive ? C.accent : C.textPrimary,
                  }}
                  onMouseEnter={(e) => {
                    if (!isActive) e.currentTarget.style.backgroundColor = "rgba(255, 255, 255, 0.05)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.backgroundColor = isActive ? C.accentSubtle : "transparent";
                  }}
                >
                  <span className="text-base shrink-0">{a.emoji || ""}</span>
                  <span className="flex-1 text-left">{a.name}</span>
                  {a.role && (
                    <span
                      className="text-[10px] px-1.5 py-0.5 rounded capitalize"
                      style={{ color: C.textMuted, backgroundColor: C.bgElevated }}
                    >
                      {a.role}
                    </span>
                  )}
                </button>
              );
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Project Assignment Dropdown ──────────────────────────────────────────────

function ProjectAssignDropdown({
  projects,
  currentProjectId,
  onAssign,
}: {
  projects: { id: string; name: string }[];
  currentProjectId: string | null;
  onAssign: (projectId: string | null) => void;
}) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const current = projects.find((p) => p.id === currentProjectId);

  useEffect(() => {
    if (!open) return;
    function handleClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", handleClick);
    return () => document.removeEventListener("mousedown", handleClick);
  }, [open]);

  return (
    <div ref={ref} className="relative">
      <div
        className="text-[10px] font-semibold uppercase tracking-[0.06em] mb-1"
        style={{ color: C.textMuted }}
      >
        Projekt
      </div>
      <button
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full px-2.5 py-2 rounded-lg text-sm transition-all cursor-pointer"
        style={{
          backgroundColor: open ? "rgba(255, 255, 255, 0.05)" : "rgba(255, 255, 255, 0.02)",
          color: C.textPrimary,
          border: `1px solid ${open ? C.borderActive : C.border}`,
        }}
      >
        <span className="flex-1 text-left text-[13px]">
          {current?.name || <span style={{ color: C.textMuted }}>— Kein Projekt (Ad-hoc) —</span>}
        </span>
        <ChevronDown
          size={14}
          className="transition-transform"
          style={{
            color: C.textMuted,
            transform: open ? "rotate(180deg)" : "rotate(0deg)",
          }}
        />
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.15 }}
            className="absolute left-0 right-0 z-50 mt-1 rounded-lg overflow-hidden max-h-72 overflow-y-auto"
            style={{
              backgroundColor: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            <button
              onClick={() => {
                if (currentProjectId !== null) onAssign(null);
                setOpen(false);
              }}
              className="flex items-center gap-2 w-full px-3 py-2 text-sm transition-colors cursor-pointer"
              style={{
                backgroundColor: currentProjectId === null ? C.accentSubtle : "transparent",
                color: currentProjectId === null ? C.accent : C.textSecondary,
                fontStyle: "italic",
              }}
              onMouseEnter={(e) => {
                if (currentProjectId !== null) e.currentTarget.style.backgroundColor = "rgba(255, 255, 255, 0.05)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.backgroundColor = currentProjectId === null ? C.accentSubtle : "transparent";
              }}
            >
              <span className="flex-1 text-left">— Kein Projekt (Ad-hoc) —</span>
            </button>
            {projects.map((p) => {
              const isActive = p.id === currentProjectId;
              return (
                <button
                  key={p.id}
                  onClick={() => {
                    if (!isActive) onAssign(p.id);
                    setOpen(false);
                  }}
                  className="flex items-center gap-2 w-full px-3 py-2 text-sm transition-colors cursor-pointer"
                  style={{
                    backgroundColor: isActive ? C.accentSubtle : "transparent",
                    color: isActive ? C.accent : C.textPrimary,
                  }}
                  onMouseEnter={(e) => {
                    if (!isActive) e.currentTarget.style.backgroundColor = "rgba(255, 255, 255, 0.05)";
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.backgroundColor = isActive ? C.accentSubtle : "transparent";
                  }}
                >
                  <span className="flex-1 text-left">{p.name}</span>
                </button>
              );
            })}
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── TaskContext ──────────────────────────────────────────────────────────────

interface TaskContextProps {
  task: Task;
  agents: Agent[];
  boardId: string;
  onAssign: (agentId: string) => void;
  onAssignProject?: (projectId: string | null) => void;
}

export function TaskContext({ task, agents, boardId, onAssign, onAssignProject }: TaskContextProps) {
  const [childrenOpen, setChildrenOpen] = useState(false);

  const { data: hierarchy } = useQuery({
    queryKey: ["task-hierarchy", boardId, task.id],
    queryFn: () => api.tasks.hierarchy(boardId, task.id),
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", boardId],
    queryFn: () => api.projects.list(boardId),
    enabled: !!boardId && !!onAssignProject,
  });

  const { data: dependencies } = useQuery({
    queryKey: ["task-dependencies", task.id],
    queryFn: () => api.tasks.dependencies(boardId, task.id),
  });

  const channelIcon = (ch: string) => {
    if (ch === "telegram") return <MessageCircle size={10} />;
    if (ch === "discord") return <GitBranch size={10} />;
    if (ch === "agent") return <ArrowUpRight size={10} />;
    return <MessageCircle size={10} />;
  };

  const reportBackColor = (status: string | null) => {
    if (status === "sent") return C.online;
    if (status === "failed") return C.error;
    return C.warning;
  };

  const hasHierarchy = hierarchy && (hierarchy.parent || hierarchy.children.length > 0 || hierarchy.report_back || hierarchy.has_credentials || hierarchy.requester);

  return (
    <div className="space-y-3">
      {/* Hierarchy Context */}
      {hasHierarchy && (
        <div
          className="rounded-lg p-3 space-y-2.5"
          style={{
            backgroundColor: "rgba(255, 255, 255, 0.02)",
            border: `1px solid ${C.border}`,
          }}
        >
          {/* Parent */}
          {hierarchy.parent && (
            <div className="flex items-center gap-2">
              <span
                className="text-[10px] font-semibold uppercase tracking-[0.06em] shrink-0"
                style={{ color: C.textMuted }}
              >
                Parent
              </span>
              <div className="flex items-center gap-1.5 min-w-0">
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{ backgroundColor: statusDotColor(hierarchy.parent.status) }}
                />
                <span
                  className="text-xs truncate"
                  style={{ color: C.textSecondary }}
                  title={hierarchy.parent.title}
                >
                  {hierarchy.parent.title}
                </span>
                <Pill color={STATUS_LABELS[hierarchy.parent.status].color} size="sm">
                  {STATUS_LABELS[hierarchy.parent.status].label}
                </Pill>
              </div>
            </div>
          )}

          {/* Children */}
          {hierarchy.children.length > 0 && (
            <div>
              <button
                onClick={() => setChildrenOpen(!childrenOpen)}
                className="flex items-center gap-2 w-full text-left cursor-pointer"
              >
                <span
                  className="text-[10px] font-semibold uppercase tracking-[0.06em] shrink-0"
                  style={{ color: C.textMuted }}
                >
                  Children
                </span>
                <div className="flex items-center gap-1">
                  {hierarchy.children.map((c) => (
                    <span
                      key={c.id}
                      className="w-1.5 h-1.5 rounded-full"
                      style={{ backgroundColor: statusDotColor(c.status) }}
                      title={`${c.title} (${c.status})`}
                    />
                  ))}
                </div>
                <span className="text-[10px]" style={{ color: C.textMuted }}>
                  {hierarchy.children.length}
                </span>
                <ChevronRight
                  size={10}
                  className="transition-transform ml-auto"
                  style={{
                    color: C.textMuted,
                    transform: childrenOpen ? "rotate(90deg)" : "rotate(0deg)",
                  }}
                />
              </button>

              <AnimatePresence>
                {childrenOpen && (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: "auto", opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.15 }}
                    className="overflow-hidden"
                  >
                    <div className="mt-1.5 pl-1 space-y-1">
                      {hierarchy.children.map((c) => (
                        <div key={c.id} className="flex items-center gap-1.5">
                          <span
                            className="w-2 h-2 rounded-full shrink-0"
                            style={{ backgroundColor: statusDotColor(c.status) }}
                          />
                          <span
                            className="text-xs truncate"
                            style={{ color: C.textSecondary }}
                            title={c.title}
                          >
                            {c.title}
                          </span>
                        </div>
                      ))}
                    </div>
                  </motion.div>
                )}
              </AnimatePresence>
            </div>
          )}

          {/* Report Back */}
          {hierarchy.report_back && hierarchy.report_back.required && (
            <div className="flex items-center gap-2">
              <span
                className="text-[10px] font-semibold uppercase tracking-[0.06em] shrink-0"
                style={{ color: C.textMuted }}
              >
                Report
              </span>
              <span
                className="inline-flex items-center gap-1 text-[10px] font-medium px-1.5 py-0.5 rounded"
                style={{
                  color: reportBackColor(hierarchy.report_back.status),
                  backgroundColor: `${reportBackColor(hierarchy.report_back.status)}26`,
                }}
              >
                {hierarchy.report_back.channel && channelIcon(hierarchy.report_back.channel)}
                {hierarchy.report_back.channel || "report"}
              </span>
            </div>
          )}

          {/* Credentials */}
          {hierarchy.has_credentials && (
            <div className="flex items-center gap-2">
              <Lock size={10} style={{ color: C.warning }} />
              <span className="text-[10px]" style={{ color: C.warning }}>
                Credentials hinterlegt
              </span>
            </div>
          )}
        </div>
      )}

      {/* Agent Assignment */}
      <AgentAssignDropdown
        agents={agents}
        currentAgentId={task.assigned_agent_id}
        onAssign={onAssign}
      />

      {/* Project Assignment — only if parent passes a handler */}
      {onAssignProject && (
        <ProjectAssignDropdown
          projects={projects}
          currentProjectId={task.project_id ?? null}
          onAssign={onAssignProject}
        />
      )}

      {/* Dependencies */}
      {dependencies && dependencies.length > 0 && (
        <div>
          <div
            className="text-[10px] font-semibold uppercase tracking-[0.06em] mb-1.5"
            style={{ color: C.textMuted }}
          >
            Depends on
          </div>
          <div className="flex flex-col gap-1">
            {dependencies.map((dep) => (
              <div key={dep.task_id} className="flex items-center gap-2 text-xs">
                <span
                  className="w-2 h-2 rounded-full shrink-0"
                  style={{
                    backgroundColor: dep.status === "done" ? C.online : C.textMuted,
                  }}
                />
                <span style={{ color: dep.status === "done" ? C.textMuted : C.textPrimary }}>
                  {dep.title}
                </span>
                <span style={{ color: C.textMuted }}>
                  ({dep.status.replace("_", " ")})
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
