"use client";

interface VaultEmptyStateProps {
  query?: string;
  scope?: string;
  isError?: boolean;
}

export function VaultEmptyState({ query, scope, isError }: VaultEmptyStateProps) {
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
          ? "vault unreachable"
          : query
          ? `no notes matching "${query}"`
          : `no notes${scope ? ` in ${scope}` : ""}`}
      </div>
      <div
        className="text-sm max-w-xs"
        style={{ color: "var(--color-text-muted)" }}
      >
        {isError
          ? "The vault index may still be building. Check backend logs."
          : query
          ? "Try a broader search term or remove filters."
          : "Notes added by agents will appear here."}
      </div>
    </div>
  );
}
