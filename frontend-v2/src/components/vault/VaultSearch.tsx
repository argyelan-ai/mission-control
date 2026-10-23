"use client";

import { useRef } from "react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

interface VaultSearchProps {
  value: string;
  onChange: (v: string) => void;
}

export function VaultSearch({ value, onChange }: VaultSearchProps) {
  const t = useTranslations("vault");
  const inputRef = useRef<HTMLInputElement>(null);

  return (
    <div className="relative">
      {/* Keyboard hints ("/" to focus, ⏎ / ESC) are desktop-only: a phone has
          no such keys, and their padding cut the placeholder short
          (round-4 user test t4 NICE-1). */}
      <span
        aria-hidden="true"
        className="hidden md:block absolute left-4 top-1/2 -translate-y-1/2 font-mono text-sm pointer-events-none select-none"
        style={{ color: "var(--color-text-muted)" }}
      >
        /
      </span>

      <input
        ref={inputRef}
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={t("searchPlaceholder")}
        className="w-full px-4 md:pl-9 md:pr-16 py-3.5 text-[15px] rounded-xl outline-none transition-colors"
        style={{
          background: "var(--color-bg-deep)",
          border: "1px solid var(--color-border)",
          color: "var(--color-text-primary)",
        }}
        onFocus={(e) => {
          e.currentTarget.style.borderColor = C.accent;
          e.currentTarget.style.boxShadow = "none";
        }}
        onBlur={(e) => {
          e.currentTarget.style.borderColor = "var(--color-border)";
          e.currentTarget.style.boxShadow = "none";
        }}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            onChange("");
            inputRef.current?.blur();
          }
        }}
        aria-label={t("searchVaultNotes")}
      />

      <span
        aria-hidden="true"
        className="hidden md:block absolute right-4 top-1/2 -translate-y-1/2 font-mono text-[11px] pointer-events-none"
        style={{ color: "var(--color-text-muted)" }}
      >
        {value ? "ESC" : "⏎"}
      </span>
    </div>
  );
}
