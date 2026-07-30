"use client";

import { useTranslations } from "next-intl";

interface VaultEmptyStateProps {
  query?: string;
  scope?: string;
  isError?: boolean;
}

export function VaultEmptyState({ query, scope, isError }: VaultEmptyStateProps) {
  const t = useTranslations("vault");
  return (
    <div
      className="flex flex-col items-center justify-center py-24 px-6 text-center"
    >
      <div
        className="text-5xl mb-6 select-none"
        style={{ filter: "grayscale(1) opacity(0.3)" }}
      >
        {isError ? "⚡" : "◈"}
      </div>
      <div
        className="font-bold text-lg tracking-tight mb-2"
        style={{ color: "var(--color-text-secondary)" }}
      >
        {isError
          ? t("vaultUnreachable")
          : query
          ? t("noNotesMatching", { query })
          : scope
          ? t("noNotesInScope", { scope })
          : t("noNotes")}
      </div>
      <div
        className="text-sm max-w-xs"
        style={{ color: "var(--color-text-muted)" }}
      >
        {isError
          ? t("vaultUnreachableHint")
          : query
          ? t("noNotesMatchingHint")
          : t("noNotesHint")}
      </div>
    </div>
  );
}
