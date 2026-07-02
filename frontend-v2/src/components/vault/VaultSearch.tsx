"use client";

import { useRef } from "react";
import { C } from "@/lib/colors";

interface VaultSearchProps {
  value: string;
  onChange: (v: string) => void;
}

export function VaultSearch({ value, onChange }: VaultSearchProps) {
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="relative">
      {/* Slash icon */}
      <span
        className="absolute left-4 top-1/2 -translate-y-1/2 font-mono text-sm pointer-events-none select-none"
        style={{ color: "var(--color-text-muted)" }}
      >
        /
      </span>

      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder="what did sparky learn about rate limits?"
        className="w-full pl-9 pr-16 py-3.5 text-[15px] rounded-xl outline-none transition-colors"
        style={{
          background: "rgba(255,255,255,0.03)",
          border: "1px solid rgba(255,255,255,0.08)",
          color: "var(--color-text-primary)",
        }}
        onFocus={(e) => {
          e.currentTarget.style.borderColor = C.accent;
          e.currentTarget.style.boxShadow = "none";
        }}
        onBlur={(e) => {
          e.currentTarget.style.borderColor = "rgba(255,255,255,0.08)";
          e.currentTarget.style.boxShadow = "none";
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            onChange("");
            inputRef.current?.blur();
          }
        }}
        aria-label="Search vault notes"
      />

      {/* Return hint */}
      <span
        className="absolute right-4 top-1/2 -translate-y-1/2 font-mono text-[11px] pointer-events-none"
        style={{ color: "var(--color-text-muted)" }}
      >
        {value ? "ESC" : "⏎"}
      </span>
    </div>
  );
}
