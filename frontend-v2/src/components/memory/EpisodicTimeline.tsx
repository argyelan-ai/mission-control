"use client";

import { useMemo } from "react";
import { motion } from "framer-motion";
import { Clock } from "lucide-react";
import { timeAgo } from "@/lib/utils";
import type { BoardMemory, MemoryType } from "@/lib/types";
import { MergeCandidateBadge } from "./MergeCandidateBadge";
import { C } from "@/lib/colors";

const TYPE_COLORS: Record<string, string> = {
  journal:       C.online,         // #2B9A4A
  weekly_review: C.textSecondary,  // #A1A1A1
  insight:       C.online,         // #2B9A4A
  task_log:      C.info,           // #2E6FD8
};

const TYPE_LABELS: Record<string, string> = {
  journal: "Journal",
  weekly_review: "Weekly Review",
  insight: "Insight",
  task_log: "Task Log",
};

function formatDay(dateStr: string): string {
  const d = new Date(dateStr);
  const today = new Date();
  const yesterday = new Date();
  yesterday.setDate(yesterday.getDate() - 1);

  if (d.toDateString() === today.toDateString()) return "Heute";
  if (d.toDateString() === yesterday.toDateString()) return "Gestern";

  return d.toLocaleDateString("de-CH", { weekday: "short", day: "numeric", month: "short", year: "numeric" });
}

function groupByDay(items: BoardMemory[]): Map<string, BoardMemory[]> {
  const groups = new Map<string, BoardMemory[]>();
  for (const item of items) {
    const key = new Date(item.created_at).toDateString();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key)!.push(item);
  }
  return groups;
}

export function EpisodicTimeline({
  entries,
  onOpen,
}: {
  entries: BoardMemory[];
  onOpen: (entry: BoardMemory) => void;
}) {
  const grouped = useMemo(() => groupByDay(entries), [entries]);

  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-3">
        <Clock size={32} style={{ color: "var(--color-text-muted)" }} />
        <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>Keine episodischen Eintraege.</p>
      </div>
    );
  }

  return (
    <div className="space-y-6">
      {Array.from(grouped.entries()).map(([dayKey, items], gi) => (
        <div key={dayKey}>
          {/* Day header */}
          <div className="flex items-center gap-3 mb-3">
            <div
              className="text-xs font-semibold uppercase tracking-wider"
              style={{ color: "var(--color-text-muted)" }}
            >
              {formatDay(items[0].created_at)}
            </div>
            <div className="flex-1 h-px" style={{ background: "rgba(255,255,255,0.06)" }} />
            <span className="text-[10px] tabular-nums" style={{ color: "var(--color-text-muted)" }}>
              {items.length} {items.length === 1 ? "Eintrag" : "Eintraege"}
            </span>
          </div>

          {/* Timeline items */}
          <div className="relative pl-7">
            <div
              className="absolute left-[9px] top-0 bottom-0 w-0.5 rounded-full"
              style={{ background: "linear-gradient(to bottom, rgba(0,204,136,0.5), rgba(0,204,136,0.05))" }}
            />
            {items.map((item, i) => {
              const color = TYPE_COLORS[item.memory_type] ?? C.textSecondary;
              return (
                <motion.div
                  key={item.id}
                  initial={{ opacity: 0, x: -8 }}
                  animate={{ opacity: 1, x: 0 }}
                  transition={{ delay: gi * 0.05 + i * 0.03, duration: 0.25 }}
                  className="relative mb-4 last:mb-0"
                >
                  {/* Dot */}
                  <div
                    className="absolute -left-7 top-4 w-2.5 h-2.5 rounded-full border-2"
                    style={{ background: color, borderColor: "#0A0A0A" }}
                  />

                  {/* Card */}
                  <div
                    onClick={() => onOpen(item)}
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
                        style={{ background: `${color}18`, color }}
                      >
                        {TYPE_LABELS[item.memory_type] ?? item.memory_type}
                      </span>
                      {/* Phase 5 MSY-02: cosine merge candidate flag */}
                      {item.merge_candidate_id != null && <MergeCandidateBadge />}
                      <span className="text-[11px]" style={{ color: "var(--color-text-muted)" }}>
                        {timeAgo(item.created_at)}
                      </span>
                      {item.auto_generated && (
                        <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.05)", color: "var(--color-text-muted)" }}>
                          Auto
                        </span>
                      )}
                      {item.source && item.source !== "user" && item.source !== "system" && (
                        <span className="text-[10px]" style={{ color: "var(--color-text-muted)" }}>
                          von {item.source}
                        </span>
                      )}
                    </div>
                    <div className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                      {item.title || item.content.slice(0, 100)}
                    </div>
                    {item.title && (
                      <p className="text-xs mt-1 line-clamp-2 leading-relaxed" style={{ color: "var(--color-text-secondary)" }}>
                        {item.content}
                      </p>
                    )}
                  </div>
                </motion.div>
              );
            })}
          </div>
        </div>
      ))}
    </div>
  );
}
