"use client";

import { ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import { C } from "@/lib/colors";

/** Link chip to a task's pull request ("PR #638 ↗"). Same look as in GitPanel. */
export function PrChip({ url, number }: { url: string; number?: number | null }) {
  const t = useTranslations("tasks");
  return (
    <a
      href={url}
      target="_blank"
      rel="noopener noreferrer"
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-sm text-[10px] font-medium cursor-pointer hover:opacity-80 transition-opacity"
      style={{ background: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
    >
      <ExternalLink size={9} aria-hidden />
      {number ? t("prChipNumber", { number }) : t("prChipOpen")}
    </a>
  );
}
