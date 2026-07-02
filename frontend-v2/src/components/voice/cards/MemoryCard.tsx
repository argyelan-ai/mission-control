"use client";

import { FileText, ArrowUpRight } from "lucide-react";
import type { MemoryCardData } from "./types";
import { CardShell } from "./CardShell";
import { C } from "@/lib/colors";

/**
 * MemoryCard — Vault note as a small card inside the VoiceDrawer.
 * Click opens an inline preview sheet (stays on the page so the voice
 * call doesn't die from navigation).
 */
export function MemoryCard({
  data,
  title,
  onClose,
  onPreview,
}: {
  data: MemoryCardData;
  title?: string | null;
  onClose: () => void;
  onPreview: () => void;
}) {
  const displayTitle =
    title || data.title || (data.vault_path || "").split("/").pop() || "Memory";

  return (
    <CardShell
      onClose={onClose}
      icon={<FileText size={13} style={{ color: C.accent }} />}
      kind="memory"
      meta={[data.type, data.agent].filter(Boolean).join(" · ")}
    >
      <button
        type="button"
        onClick={onPreview}
        className="flex items-start gap-1.5 group min-w-0 flex-1 text-left cursor-pointer w-full"
      >
        <div className="min-w-0 flex-1">
          <div
            className="text-[11px] font-medium leading-snug truncate group-hover:text-white transition-colors"
            style={{ color: "var(--color-text-primary)" }}
          >
            {displayTitle}
          </div>
          {data.snippet && (
            <div
              className="text-[10px] leading-snug mt-1 line-clamp-2"
              style={{ color: "var(--color-text-muted)" }}
            >
              {data.snippet}
            </div>
          )}
        </div>
        <ArrowUpRight
          size={11}
          className="mt-0.5 opacity-50 group-hover:opacity-100 shrink-0"
          style={{ color: "var(--color-text-secondary)" }}
        />
      </button>
    </CardShell>
  );
}
