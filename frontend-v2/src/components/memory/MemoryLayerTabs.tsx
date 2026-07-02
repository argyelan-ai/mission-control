"use client";

import { motion } from "framer-motion";
import { Database, Clock, GraduationCap, Layers } from "lucide-react";
import { LAYER_COLORS } from "@/components/memory/graphConfig";

export type MemoryLayer = "semantic" | "episodic" | "agent" | "topics";

const LAYER_CONFIG: Record<MemoryLayer, { label: string; icon: typeof Database; color: string; desc: string }> = {
  episodic:  { label: "Episodic",  icon: Clock,          color: LAYER_COLORS.episodic, desc: "Journals, Reviews, Insights" },
  semantic:  { label: "Semantic",  icon: Database,        color: LAYER_COLORS.semantic, desc: "Knowledge, Decisions, Concepts" },
  agent:     { label: "Agent",     icon: GraduationCap,   color: LAYER_COLORS.agent,   desc: "Lessons pro Agent" },
  topics:    { label: "Themen",    icon: Layers,          color: LAYER_COLORS.topics,  desc: "Semantische Themen-Cluster" },
};

const LAYERS: MemoryLayer[] = ["episodic", "semantic", "agent", "topics"];

export function MemoryLayerTabs({
  active,
  onChange,
  counts,
}: {
  active: MemoryLayer;
  onChange: (layer: MemoryLayer) => void;
  counts?: Partial<Record<MemoryLayer, number>>;
}) {
  // tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17)
  return (
    <div className="flex gap-1.5 p-1 rounded-xl mb-5 tab-strip" style={{ background: "rgba(255,255,255,0.03)", border: "1px solid rgba(255,255,255,0.06)" }}>
      {LAYERS.map((layer) => {
        const cfg = LAYER_CONFIG[layer];
        const Icon = cfg.icon;
        const isActive = active === layer;
        const count = counts?.[layer];

        return (
          <button
            key={layer}
            onClick={() => onChange(layer)}
            className="relative flex-1 flex items-center justify-center gap-2 py-2.5 px-3 rounded-lg text-sm font-medium cursor-pointer transition-colors min-h-touch"
            style={{
              color: isActive ? cfg.color : "var(--color-text-muted)",
            }}
          >
            {isActive && (
              <motion.div
                layoutId="memory-layer-tab"
                className="absolute inset-0 rounded-lg"
                style={{
                  background: `${cfg.color}08`,
                  border: `1px solid ${cfg.color}25`,
                }}
                transition={{ type: "spring", stiffness: 400, damping: 30 }}
              />
            )}
            <span className="relative flex items-center gap-2">
              <Icon size={14} />
              {cfg.label}
              {count != null && (
                <span
                  className="px-1.5 py-0.5 rounded-full text-[10px] font-semibold"
                  style={{
                    background: isActive ? `${cfg.color}18` : "rgba(255,255,255,0.06)",
                    color: isActive ? cfg.color : "var(--color-text-muted)",
                  }}
                >
                  {count}
                </span>
              )}
            </span>
          </button>
        );
      })}
    </div>
  );
}
