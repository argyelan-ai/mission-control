"use client";

import { motion } from "framer-motion";
import { C } from "@/lib/colors";

type PlannerMode = "direct" | "auto" | "with_planner";

interface PlannerSliderProps {
  value: PlannerMode;
  onChange: (value: PlannerMode) => void;
  accent?: string;
  textMuted?: string;
  textSecondary?: string;
  border?: string;
}

const STOPS: { value: PlannerMode; label: string; hint: string }[] = [
  { value: "direct", label: "Direkt", hint: "Sofort ausfuehren" },
  { value: "auto", label: "Auto", hint: "System entscheidet" },
  { value: "with_planner", label: "Planner", hint: "Erst planen" },
];

export function PlannerSlider({
  value,
  onChange,
  accent = C.accent,
  textMuted = C.textDim,
  textSecondary = C.textSecondary,
  border = C.border,
}: PlannerSliderProps) {
  const activeIndex = STOPS.findIndex((s) => s.value === value);

  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center gap-2">
        <span className="text-[10px] shrink-0" style={{ color: textMuted }}>
          Planung:
        </span>
        <div className="relative flex items-center flex-1 h-6">
          {/* Track line */}
          <div
            className="absolute left-3 right-3 h-px"
            style={{ backgroundColor: border }}
          />

          {/* Active segment fill */}
          <motion.div
            className="absolute h-0.5 rounded-full"
            style={{
              backgroundColor: `${accent}66`,
              left: "12px",
              width: `calc(${(activeIndex / (STOPS.length - 1)) * 100}% - 0px)`,
            }}
            layout
            transition={{ type: "spring", stiffness: 500, damping: 35 }}
          />

          {/* Stop dots + labels */}
          <div className="relative flex justify-between w-full px-1">
            {STOPS.map((stop, i) => {
              const isActive = stop.value === value;
              return (
                <button
                  key={stop.value}
                  type="button"
                  onClick={() => onChange(stop.value)}
                  className="flex flex-col items-center gap-0.5 cursor-pointer group"
                >
                  <motion.div
                    className="w-2.5 h-2.5 rounded-full border transition-colors"
                    style={{
                      backgroundColor: isActive ? accent : "transparent",
                      borderColor: isActive ? accent : `${textMuted}44`,
                      boxShadow: isActive ? `0 0 8px ${accent}40` : "none",
                    }}
                    layout
                    transition={{ type: "spring", stiffness: 500, damping: 35 }}
                  />
                  <span
                    className="text-[9px] font-medium transition-colors"
                    style={{ color: isActive ? accent : textMuted }}
                  >
                    {stop.label}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      </div>

      {/* Active hint */}
      <div className="pl-[52px]">
        <span className="text-[9px]" style={{ color: textSecondary }}>
          {STOPS[activeIndex]?.hint}
        </span>
      </div>
    </div>
  );
}
