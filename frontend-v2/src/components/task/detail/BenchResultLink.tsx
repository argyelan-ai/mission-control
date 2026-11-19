"use client";

/**
 * A bench agent run is a fleet task, but its result belongs to the
 * benchmark (video, numbers, gate). The task detail leads there instead of
 * leaving the operator at the raw deliverable (27.10.).
 */
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ChevronRight, FlaskConical } from "lucide-react";
import { C } from "@/lib/colors";

export function BenchResultLink({ href, testId, note = false }: { href: string; testId: string; note?: boolean }) {
  const t = useTranslations("tasks");
  return (
    <div className="flex flex-col gap-1">
      {note && (
        <p className="text-sm m-0" style={{ color: C.textSecondary }}>
          {t("benchResultNote")}
        </p>
      )}
      <Link
        href={href}
        data-testid={testId}
        className="self-start inline-flex items-center gap-2 min-h-11 px-4 rounded-full text-sm font-medium no-underline"
        style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.textPrimary }}
      >
        <FlaskConical size={16} aria-hidden />
        {t("benchResult")}
        <ChevronRight size={14} aria-hidden />
      </Link>
    </div>
  );
}
