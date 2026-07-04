"use client";

import { useState, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import { GraduationCap, ChevronRight, Bot } from "lucide-react";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import type { Agent, BoardMemory } from "@/lib/types";
import { MergeCandidateBadge } from "./MergeCandidateBadge";
import { C, STATUS_TEXT } from "@/lib/colors";

/**
 * AgentLessonMatrix — Links Agent-Liste, rechts deren Lessons.
 * Agent-Lessons sind Memories mit memory_type="lesson" und agent_id gesetzt.
 */

function groupLessonsByAgent(lessons: BoardMemory[], agents: Agent[]): Map<string, { agent: Agent; lessons: BoardMemory[] }> {
  const agentMap = new Map(agents.map((a) => [a.id, a]));
  const grouped = new Map<string, { agent: Agent; lessons: BoardMemory[] }>();

  for (const lesson of lessons) {
    if (!lesson.agent_id) continue;
    const agent = agentMap.get(lesson.agent_id);
    if (!agent) continue;

    if (!grouped.has(lesson.agent_id)) {
      grouped.set(lesson.agent_id, { agent, lessons: [] });
    }
    grouped.get(lesson.agent_id)!.lessons.push(lesson);
  }

  // Sort by lesson count descending
  return new Map(
    Array.from(grouped.entries()).sort(([, a], [, b]) => b.lessons.length - a.lessons.length)
  );
}

export function AgentLessonMatrix({
  lessons,
  onOpen,
}: {
  lessons: BoardMemory[];
  onOpen: (entry: BoardMemory) => void;
}) {
  const [selectedAgentId, setSelectedAgentId] = useState<string | null>(null);

  const { data: agents } = useQuery({
    queryKey: ["agents-all"],
    queryFn: () => api.agents.list(undefined, true),
    staleTime: 60_000,
  });

  const grouped = useMemo(
    () => groupLessonsByAgent(lessons, agents ?? []),
    [lessons, agents]
  );

  // Auto-select first agent if nothing selected
  const activeAgentId = selectedAgentId ?? Array.from(grouped.keys())[0] ?? null;
  const activeGroup = activeAgentId ? grouped.get(activeAgentId) : null;

  if (lessons.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-3">
        <GraduationCap size={32} style={{ color: "var(--color-text-muted)" }} />
        <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>No agent lessons yet.</p>
        <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
          Lessons are automatically extracted from reflections.
        </p>
      </div>
    );
  }

  return (
    <div className="flex gap-4 min-h-[400px]">
      {/* Left: Agent list */}
      <div
        className="w-56 shrink-0 rounded-xl overflow-hidden"
        style={{ border: "1px solid rgba(255,255,255,0.06)" }}
      >
        <div className="px-3 py-2.5 text-[10px] font-bold uppercase tracking-wider" style={{ color: "var(--color-text-muted)", background: "rgba(255,255,255,0.02)" }}>
          Agents ({grouped.size})
        </div>
        <div className="divide-y" style={{ borderColor: "rgba(255,255,255,0.04)" }}>
          {Array.from(grouped.entries()).map(([agentId, { agent, lessons: agentLessons }]) => {
            const isActive = agentId === activeAgentId;
            return (
              <button
                key={agentId}
                onClick={() => setSelectedAgentId(agentId)}
                className="w-full flex items-center gap-2.5 px-3 py-2.5 text-left cursor-pointer transition-colors"
                style={{
                  background: isActive ? `${C.error}0F` : "transparent",
                  borderLeft: isActive ? `2px solid ${C.error}` : "2px solid transparent",
                }}
                onMouseEnter={(e) => { if (!isActive) e.currentTarget.style.background = "rgba(255,255,255,0.03)"; }}
                onMouseLeave={(e) => { if (!isActive) e.currentTarget.style.background = "transparent"; }}
              >
                <Bot size={14} style={{ color: isActive ? C.error : "var(--color-text-muted)" }} />
                <div className="flex-1 min-w-0">
                  <div className="text-sm font-medium truncate" style={{ color: isActive ? "var(--color-text-primary)" : "var(--color-text-secondary)" }}>
                    {agent.name}
                  </div>
                  <div className="text-[10px]" style={{ color: "var(--color-text-muted)" }}>
                    {agentLessons.length} {agentLessons.length === 1 ? "Lesson" : "Lessons"}
                  </div>
                </div>
                {isActive && <ChevronRight size={12} style={{ color: C.error }} />}
              </button>
            );
          })}
        </div>
      </div>

      {/* Right: Lessons for selected agent */}
      <div className="flex-1 min-w-0">
        <AnimatePresence mode="wait">
          {activeGroup ? (
            <motion.div
              key={activeAgentId}
              initial={{ opacity: 0, x: 8 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0, x: -8 }}
              transition={{ duration: 0.2 }}
              className="space-y-2"
            >
              <div className="flex items-center gap-2 mb-3 px-1">
                <GraduationCap size={14} style={{ color: C.error }} />
                <span className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
                  {activeGroup.agent.name}
                </span>
                <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                  — {activeGroup.lessons.length} Lessons
                </span>
              </div>

              {activeGroup.lessons.map((lesson, i) => (
                <motion.div
                  key={lesson.id}
                  initial={{ opacity: 0, y: 6 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ delay: i * 0.04, duration: 0.2 }}
                  onClick={() => onOpen(lesson)}
                  className="rounded-xl p-4 cursor-pointer transition-colors"
                  style={{
                    background: "rgba(255,255,255,0.02)",
                    border: "1px solid rgba(255,255,255,0.06)",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.04)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.02)")}
                >
                  <div className="flex items-center gap-2 mb-1.5">
                    <span
                      className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold"
                      style={{ background: `${C.error}1F`, color: STATUS_TEXT.error }}
                    >
                      Lesson
                    </span>
                    {/* Phase 5 MSY-02: cosine merge candidate flag */}
                    {lesson.merge_candidate_id != null && <MergeCandidateBadge />}
                    <span className="text-[11px]" style={{ color: "var(--color-text-muted)" }}>
                      {timeAgo(lesson.created_at)}
                    </span>
                    {lesson.auto_generated && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.05)", color: "var(--color-text-muted)" }}>
                        Auto
                      </span>
                    )}
                  </div>
                  <div className="text-sm font-medium mb-1" style={{ color: "var(--color-text-primary)" }}>
                    {lesson.title || "(No title)"}
                  </div>
                  <p className="text-xs leading-relaxed line-clamp-3" style={{ color: "var(--color-text-secondary)" }}>
                    {lesson.content}
                  </p>
                  {lesson.tags?.length > 0 && (
                    <div className="flex gap-1 flex-wrap mt-2">
                      {lesson.tags.slice(0, 4).map((tag) => (
                        <span key={tag} className="px-1.5 py-0.5 rounded text-[10px]" style={{ background: "rgba(255,255,255,0.05)", color: "var(--color-text-muted)" }}>
                          {tag}
                        </span>
                      ))}
                    </div>
                  )}
                </motion.div>
              ))}
            </motion.div>
          ) : (
            <motion.div
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              className="flex items-center justify-center h-full"
            >
              <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>
                Select an agent to view lessons
              </p>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}
