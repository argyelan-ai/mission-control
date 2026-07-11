"use client";

import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Copy, Check } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import type { ModelCatalog } from "@/lib/types";

export const wizardOverlayClass =
  "fixed inset-0 z-50 flex items-end sm:items-center justify-center px-3 sm:px-4";
export const wizardBackdropClass = "absolute inset-0 bg-black/70 backdrop-blur-sm";
export const wizardCardStyle = {
  backgroundColor: C.bgBase,
  border: `1px solid ${C.border}`,
  boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
} as const;
export const wizardLabelClass =
  "text-[11px] mb-1.5 block text-[var(--color-text-muted)] uppercase tracking-wider";
export const wizardInputStyle = {
  border: `1px solid ${C.border}`,
  color: "var(--color-text-primary)",
} as const;
export const wizardInputClass =
  "w-full px-3 py-2.5 text-sm rounded-xl bg-transparent outline-none transition-colors";
export const wizardSelectStyle = { ...wizardInputStyle, backgroundColor: C.bgBase } as const;
export const wizardBtnPrimaryStyle = {
  background: `linear-gradient(135deg, ${C.accentHover}, ${C.accent})`,
} as const;

export function ModelInput({
  value,
  onChange,
  placeholder,
}: {
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const { data: catalog } = useQuery<ModelCatalog>({
    queryKey: ["model-catalog"],
    queryFn: () => api.models.list(),
    staleTime: 120_000,
  });
  const models = catalog?.models ?? [];
  return (
    <div className="relative">
      <input
        type="text"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onFocus={() => setOpen(true)}
        onBlur={() => setTimeout(() => setOpen(false), 200)}
        placeholder={placeholder ?? "anthropic/claude-sonnet-4-20250514"}
        className={`${wizardInputClass} font-mono text-[12px]`}
        style={wizardInputStyle}
      />
      {open && models.length > 0 && (
        <div
          className="absolute z-20 top-full left-0 right-0 mt-1 rounded-xl overflow-hidden max-h-48 overflow-y-auto"
          style={{ backgroundColor: C.bgBase, border: `1px solid ${C.border}` }}
        >
          {models
            .filter((m) => m.available)
            .filter((m) => !value || m.id.toLowerCase().includes(value.toLowerCase()))
            .slice(0, 12)
            .map((m) => (
              <button
                key={m.id}
                type="button"
                onMouseDown={(e) => {
                  e.preventDefault();
                  onChange(m.id);
                  setOpen(false);
                }}
                className="block w-full text-left px-3 py-2 text-[11px] font-mono cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.06)]"
                style={{ color: "var(--color-text-secondary)" }}
              >
                {m.id}
              </button>
            ))}
        </div>
      )}
    </div>
  );
}

export function TokenDisplay({ token }: { token: string }) {
  const [copied, setCopied] = useState(false);
  async function copy() {
    await navigator.clipboard.writeText(token);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }
  return (
    <div
      className="rounded-xl p-3 text-[11px] font-mono break-all flex items-start gap-2"
      style={{
        backgroundColor: "rgba(255,255,255,0.03)",
        color: "var(--color-text-muted)",
        border: `1px solid ${C.borderSubtle}`,
      }}
    >
      <span className="flex-1">{token}</span>
      <button
        onClick={copy}
        className="shrink-0 p-1 rounded-md cursor-pointer transition-colors hover:bg-[rgba(255,255,255,0.06)]"
        title="Token kopieren"
      >
        {copied ? <Check size={12} className="text-[var(--color-online)]" /> : <Copy size={12} />}
      </button>
    </div>
  );
}
