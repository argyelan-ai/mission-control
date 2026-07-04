"use client";

import { motion } from "framer-motion";
import { Pin, Database, Plus } from "lucide-react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import type { BoardMemory, MemoryType } from "@/lib/types";
import { MergeCandidateBadge } from "./MergeCandidateBadge";
import { C } from "@/lib/colors";

const TYPE_STYLE: Record<string, { color: string; bg: string; label: string }> = {
  knowledge:  { color: C.textSecondary, bg: `${C.textSecondary}1F`, label: "Knowledge" },
  reference:  { color: C.warning,       bg: `${C.warning}1F`,       label: "Reference" },
  research:   { color: C.info,          bg: `${C.info}1F`,          label: "Research" },
};

export function SemanticCardGrid({
  entries,
  onOpen,
  onNew,
}: {
  entries: BoardMemory[];
  onOpen: (entry: BoardMemory) => void;
  onNew: () => void;
}) {
  const qc = useQueryClient();

  const pinMutation = useMutation({
    mutationFn: ({ id, pinned }: { id: string; pinned: boolean }) =>
      api.knowledge.update(id, { is_pinned: pinned }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["knowledge"] });
      qc.invalidateQueries({ queryKey: ["knowledge-layer-semantic"] });
    },
  });

  const pinned = entries.filter((e) => e.is_pinned);
  const unpinned = entries.filter((e) => !e.is_pinned);

  if (entries.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center py-20 gap-3">
        <Database size={32} style={{ color: "var(--color-text-muted)" }} />
        <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>No semantic entries.</p>
        <button
          onClick={onNew}
          className="flex items-center gap-2 px-4 py-2 rounded-xl text-xs font-medium cursor-pointer transition-colors"
          style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
        >
          <Plus size={13} /> New entry
        </button>
      </div>
    );
  }

  function renderCard(item: BoardMemory, index: number) {
    const style = TYPE_STYLE[item.memory_type] ?? TYPE_STYLE.knowledge;
    return (
      <motion.div
        key={item.id}
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ delay: index * 0.03, duration: 0.2 }}
        onClick={() => onOpen(item)}
        className="group relative rounded-2xl p-4 cursor-pointer transition-colors"
        style={{
          background: "rgba(255,255,255,0.03)",
          border: "1px solid rgba(255,255,255,0.08)",
        }}
        onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.05)")}
        onMouseLeave={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.03)")}
      >
        {/* Pin button */}
        <button
          onClick={(e) => {
            e.stopPropagation();
            pinMutation.mutate({ id: item.id, pinned: !item.is_pinned });
          }}
          className="absolute top-3 right-3 p-1.5 rounded-lg opacity-0 group-hover:opacity-100 transition-opacity cursor-pointer touch-visible"
          style={{
            background: item.is_pinned ? "rgba(245,158,11,0.12)" : "rgba(255,255,255,0.05)",
            color: item.is_pinned ? C.warning : "var(--color-text-muted)",
          }}
          title={item.is_pinned ? "Unpin" : "Pin"}
        >
          <Pin size={12} />
        </button>

        {/* Header */}
        <div className="flex items-center gap-2 mb-2">
          <span
            className="inline-flex items-center px-2 py-0.5 rounded-full text-[10px] font-semibold"
            style={{ background: style.bg, color: style.color }}
          >
            {style.label}
          </span>
          {/* Phase 5 MSY-02: cosine merge candidate flag */}
          {item.merge_candidate_id != null && <MergeCandidateBadge />}
          <span className="text-[11px]" style={{ color: "var(--color-text-muted)" }}>
            {timeAgo(item.created_at)}
          </span>
        </div>

        {/* Title */}
        <div className="text-sm font-semibold mb-1.5 pr-6" style={{ color: "var(--color-text-primary)" }}>
          {item.title || "(No title)"}
        </div>

        {/* Content preview */}
        <p className="text-xs leading-relaxed line-clamp-3" style={{ color: "var(--color-text-secondary)" }}>
          {item.content}
        </p>

        {/* Tags */}
        {item.tags?.length > 0 && (
          <div className="flex gap-1 flex-wrap mt-3">
            {item.tags.slice(0, 3).map((tag) => (
              <span
                key={tag}
                className="px-1.5 py-0.5 rounded text-[10px]"
                style={{ background: "rgba(255,255,255,0.05)", color: "var(--color-text-muted)" }}
              >
                {tag}
              </span>
            ))}
            {item.tags.length > 3 && (
              <span className="text-[10px] px-1" style={{ color: "var(--color-text-muted)" }}>
                +{item.tags.length - 3}
              </span>
            )}
          </div>
        )}
      </motion.div>
    );
  }

  return (
    <div className="space-y-5">
      {/* Pinned section */}
      {pinned.length > 0 && (
        <div>
          <div className="flex items-center gap-2 mb-3 px-1">
            <Pin size={11} style={{ color: C.warning }} />
            <span className="text-[10px] font-bold uppercase tracking-wider" style={{ color: "var(--color-text-muted)" }}>
              Pinned ({pinned.length})
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3">
            {pinned.map((item, i) => renderCard(item, i))}
          </div>
        </div>
      )}

      {/* All entries */}
      <div className="grid grid-cols-2 gap-3">
        {unpinned.map((item, i) => renderCard(item, pinned.length + i))}
        {/* New entry card */}
        <motion.div
          initial={{ opacity: 0 }}
          animate={{ opacity: 1 }}
          onClick={onNew}
          className="rounded-2xl p-4 cursor-pointer flex flex-col items-center justify-center gap-2 transition-colors min-h-[120px]"
          style={{ border: "1px dashed rgba(255,255,255,0.08)", color: "var(--color-text-muted)" }}
          onMouseEnter={(e) => {
            e.currentTarget.style.borderColor = C.accent;
            e.currentTarget.style.color = C.accent;
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.borderColor = "rgba(255,255,255,0.08)";
            e.currentTarget.style.color = "var(--color-text-muted)";
          }}
        >
          <Plus size={20} />
          <span className="text-sm">New entry</span>
        </motion.div>
      </div>
    </div>
  );
}
