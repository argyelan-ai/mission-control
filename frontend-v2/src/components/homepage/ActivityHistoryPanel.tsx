"use client";

/**
 * ActivityHistoryPanel
 * Slide-in side panel for full activity history.
 */

import { motion } from "framer-motion";
import { X } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { useAppStore } from "@/lib/store";
import type { ActivityEvent } from "@/lib/types";
import { timeAgo } from "@/lib/utils";
import { C } from "./colors";

export function ActivityHistoryPanel({ onClose }: { onClose: () => void }) {
  const { activeBoardId } = useAppStore();
  const { data: events = [] } = useQuery({
    queryKey: ["activity", "full-history"],
    queryFn: () => api.activity.list({ board_id: activeBoardId ?? undefined, limit: 100 }),
    refetchInterval: 15_000,
  });

  return (
    <motion.div
      initial={{ opacity: 0 }}
      animate={{ opacity: 1 }}
      exit={{ opacity: 0 }}
      transition={{ duration: 0.15 }}
      className="fixed inset-0 z-50 flex items-start justify-end p-4 pt-16"
      onClick={(e) => { if (e.target === e.currentTarget) onClose(); }}
    >
      <div
        className="absolute inset-0"
        style={{ backgroundColor: "rgba(0,0,0,0.5)", backdropFilter: "blur(4px)", WebkitBackdropFilter: "blur(4px)" }}
      />

      <motion.div
        initial={{ opacity: 0, x: 40 }}
        animate={{ opacity: 1, x: 0 }}
        exit={{ opacity: 0, x: 40 }}
        transition={{ duration: 0.25, ease: [0.16, 1, 0.3, 1] }}
        className="relative w-full max-w-md h-[80vh] rounded-2xl overflow-hidden flex flex-col"
        style={{
          background: C.bgElevated,
          border: `1px solid rgba(255,255,255,0.08)`,
          boxShadow: `0 25px 80px rgba(0,0,0,0.6)`,
        }}
      >
        <div className="absolute top-0 left-0 right-0 h-px" style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.12), transparent)" }} />

        <div className="flex items-center justify-between px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.border}` }}>
          <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>Activity History</span>
          <button onClick={onClose} className="cursor-pointer hover:opacity-80 transition-opacity" style={{ color: C.textMuted }}>
            <X size={16} />
          </button>
        </div>

        <div className="flex-1 overflow-y-auto p-4">
          {events.length === 0 ? (
            <div className="flex items-center justify-center h-full">
              <span className="text-sm" style={{ color: C.textMuted }}>No activity</span>
            </div>
          ) : (
            <div className="flex flex-col gap-2">
              {events.map((event: ActivityEvent) => {
                const isError = event.event_type?.includes("fail") || event.event_type?.includes("error");
                const isSuccess = event.event_type?.includes("done") || event.event_type?.includes("complete") || event.event_type?.includes("approved");
                const dotColor = isError ? C.error : isSuccess ? C.online : C.textMuted;

                return (
                  <div
                    key={event.id}
                    className="flex items-start gap-3 px-3 py-2.5 rounded-lg transition-colors"
                    style={{ backgroundColor: "rgba(255,255,255,0.02)" }}
                    onMouseEnter={(e) => (e.currentTarget.style.backgroundColor = "rgba(255,255,255,0.05)")}
                    onMouseLeave={(e) => (e.currentTarget.style.backgroundColor = "rgba(255,255,255,0.02)")}
                  >
                    <span
                      className="w-2 h-2 rounded-full shrink-0 mt-1.5"
                      style={{ backgroundColor: dotColor, boxShadow: isSuccess || isError ? `0 0 6px ${dotColor}44` : "none" }}
                    />
                    <div className="flex-1 min-w-0">
                      <div className="text-[12px] leading-relaxed" style={{ color: C.textPrimary }}>{event.title}</div>
                      <div className="flex items-center gap-2 mt-0.5">
                        <span className="text-[10px]" style={{ color: C.textMuted }}>{timeAgo(event.created_at)}</span>
                        {event.severity && event.severity !== "info" && (
                          <span
                            className="text-[10px] font-medium px-1.5 py-0.5 rounded-full uppercase"
                            style={{
                              color: event.severity === "error" ? C.error : event.severity === "warning" ? C.warning : C.textMuted,
                              backgroundColor: event.severity === "error" ? `${C.error}15` : event.severity === "warning" ? `${C.warning}15` : "transparent",
                            }}
                          >
                            {event.severity}
                          </span>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </motion.div>
    </motion.div>
  );
}
