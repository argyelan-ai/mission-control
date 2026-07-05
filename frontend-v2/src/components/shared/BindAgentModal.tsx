"use client";

/**
 * BindAgentModal — Phase 15 T3.3.
 *
 * Lets the operator bind any cli-bridge agent to a runtime directly from the
 * /runtimes page (instead of having to open AgentDetailPage). Re-uses
 * RuntimeSwitchModal for the actual confirm/preview/force flow — this
 * modal is a thin agent-picker on top.
 */

import { useState } from "react";
import Link from "next/link";
import { motion, AnimatePresence } from "framer-motion";
import { useBodyScrollLock } from "@/hooks/useBodyScrollLock";
import { ArrowRight, X, Loader2 } from "lucide-react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Agent, Runtime } from "@/lib/types";
import { RuntimeSwitchModal } from "@/components/shared/RuntimeSwitchModal";
import { RuntimePill } from "@/components/shared/RuntimePill";
import { C } from "@/lib/colors";

interface Props {
  open: boolean;
  onClose: () => void;
  runtime: Runtime;
}

export function BindAgentModal({ open, onClose, runtime }: Props) {
  const [pickedAgent, setPickedAgent] = useState<Agent | null>(null);

  // iOS-safe scroll lock (M4)
  useBodyScrollLock(open);

  const { data: agents = [], isLoading } = useQuery({
    queryKey: ["agents", "all-cli-bridge"],
    queryFn: () => api.agents.list(undefined, true),
    enabled: open,
    select: (rows) => rows.filter((a) => a.agent_runtime === "cli-bridge"),
  });

  return (
    <>
      <AnimatePresence>
        {open && !pickedAgent && (
          <motion.div
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            className="fixed inset-0 z-40 flex items-end sm:items-center justify-center sm:p-4 bg-black/60"
            style={{ paddingBottom: "env(safe-area-inset-bottom)" }}
            onClick={onClose}
          >
            {/* Drag indicator — mobile only */}
            <div className="sm:hidden absolute bottom-[calc(92dvh-0.5rem)] left-1/2 -translate-x-1/2 z-10 w-8 h-1 rounded-full pointer-events-none" style={{ backgroundColor: "rgba(255,255,255,0.18)" }} />

            <motion.div
              initial={{ opacity: 0, y: 32 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: 32 }}
              transition={{ duration: 0.22, ease: [0.16, 1, 0.3, 1] }}
              className="relative w-full mx-2 rounded-t-2xl rounded-b-none sm:mx-0 sm:max-w-md sm:rounded-2xl overflow-hidden max-h-[92dvh] sm:max-h-[88vh] flex flex-col"
              onClick={(e) => e.stopPropagation()}
              style={{
                backgroundColor: "var(--color-bg-elevated)",
                border: "1px solid rgba(255,255,255,0.08)",
                boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
              }}
            >
                <div
                  className="flex items-center justify-between p-5 border-b shrink-0"
                  style={{ borderColor: "rgba(255,255,255,0.06)" }}
                >
                  <div>
                    <h2 className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
                      Bind agent
                    </h2>
                    <div className="text-[11px] mt-0.5" style={{ color: "var(--color-text-muted)" }}>
                      Target runtime: <span className="font-mono">{runtime.display_name}</span>
                    </div>
                  </div>
                  <button
                    onClick={onClose}
                    className="p-1 rounded-md hover:bg-[rgba(255,255,255,0.06)] cursor-pointer"
                  >
                    <X size={14} style={{ color: "var(--color-text-muted)" }} />
                  </button>
                </div>

                <div className="flex-1 overflow-y-auto p-3">
                  {isLoading && (
                    <div className="flex items-center gap-2 p-3 text-[12px] text-[var(--color-text-muted)]">
                      <Loader2 size={12} className="animate-spin" />
                      Loading cli-bridge agents…
                    </div>
                  )}
                  {!isLoading && agents.length === 0 && (
                    <div className="p-4 text-[12px] text-[var(--color-text-muted)] text-center">
                      No cli-bridge agents available.
                    </div>
                  )}
                  <ul className="space-y-1">
                    {agents.map((a) => {
                      const alreadyBound = a.runtime_id === runtime.id;
                      return (
                        <li key={a.id}>
                          <button
                            onClick={() => !alreadyBound && setPickedAgent(a)}
                            disabled={alreadyBound}
                            className="w-full flex items-center justify-between gap-3 p-2.5 rounded-lg text-left transition-colors hover:bg-[rgba(255,255,255,0.04)] disabled:cursor-not-allowed disabled:opacity-50 cursor-pointer"
                            style={{
                              backgroundColor: "rgba(255,255,255,0.02)",
                              border: "1px solid rgba(255,255,255,0.06)",
                            }}
                          >
                            <div className="flex items-center gap-2 min-w-0">
                              <span className="text-base shrink-0">{a.emoji ?? "🤖"}</span>
                              <div className="min-w-0">
                                <div
                                  className="text-[13px] font-medium truncate"
                                  style={{ color: "var(--color-text-primary)" }}
                                >
                                  {a.name}
                                </div>
                                <div className="mt-0.5">
                                  <RuntimePill agent={a} variant="compact" />
                                </div>
                              </div>
                            </div>
                            {alreadyBound ? (
                              <span className="text-[10px] font-mono shrink-0" style={{ color: C.online }}>
                                bound
                              </span>
                            ) : (
                              <ArrowRight size={13} style={{ color: "var(--color-text-muted)" }} />
                            )}
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>

                <div
                  className="px-5 py-3 border-t text-[10px] shrink-0"
                  style={{
                    borderColor: "rgba(255,255,255,0.06)",
                    color: "var(--color-text-muted)",
                    paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)",
                  }}
                >
                  Tip: you can also control runtime switches per agent in the{" "}
                  <Link href="/agents" className="underline hover:text-[var(--color-text-secondary)]">
                    Agents section
                  </Link>
                  .
                </div>
            </motion.div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Phase 15 T3.1 — re-uses the same modal with dry-run + force toggle */}
      {pickedAgent && (
        <RuntimeSwitchModal
          open={true}
          onClose={() => {
            setPickedAgent(null);
            onClose();
          }}
          agent={pickedAgent}
          targetRuntimeId={runtime.id}
          onConfirm={async ({ force_when_in_progress, harness }) => {
            const res = await api.agents.switchRuntime(pickedAgent.id, runtime.id, {
              force_when_in_progress,
              harness,
            });
            return res._switch ?? null;
          }}
        />
      )}
    </>
  );
}
