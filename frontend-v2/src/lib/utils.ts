import { type ClassValue, clsx } from "clsx";
import { twMerge } from "tailwind-merge";
import { formatDistanceToNow } from "date-fns";

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

export function contextPercent(tokens: number, max: number): number {
  if (!max) return 0;
  return Math.round((tokens / max) * 100);
}

export function contextColor(pct: number): string {
  if (pct >= 90) return "var(--color-status-error)";
  if (pct >= 70) return "var(--color-status-warning)";
  return "var(--color-status-online)";
}

export function agentStatusColor(status: string): string {
  switch (status) {
    case "online": return "status-dot-online";
    case "busy": return "status-dot-busy";
    case "error": return "status-dot-error";
    case "restarting": return "status-dot-restarting";
    case "offline": return "status-dot-offline";
    default: return "status-dot-offline";
  }
}

export function priorityColor(priority: string): string {
  switch (priority) {
    case "critical": return "text-[var(--color-status-error)]";
    case "high": return "text-[var(--color-status-warning)]";
    case "medium": return "text-[var(--color-text-secondary)]";
    default: return "text-[var(--color-text-muted)]";
  }
}

export function severityColor(severity: string): string {
  switch (severity) {
    case "critical":
    case "error": return "var(--color-status-error)";
    case "warning": return "var(--color-status-warning)";
    default: return "var(--color-status-info)";
  }
}

export function timeAgo(date: string | null): string {
  if (!date) return "never";
  try {
    return formatDistanceToNow(new Date(date), { addSuffix: true });
  } catch {
    return "unknown";
  }
}

export function slugify(str: string): string {
  return str
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "-")
    .replace(/^-|-$/g, "");
}

export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ["KB", "MB", "GB"];
  let value = bytes / 1024;
  let i = 0;
  while (value >= 1024 && i < units.length - 1) {
    value /= 1024;
    i++;
  }
  return `${value.toFixed(value >= 10 ? 0 : 1)} ${units[i]}`;
}

// ── Reference Files (ADR-053) ────────────────────────────────────────────────
// Shared `accept` attribute for reference-file upload inputs — mirrors the
// backend's allowed-MIME allowlist (routers/references.py).
export const REFERENCE_FILE_ACCEPT =
  ".png,.jpg,.jpeg,.webp,.gif,.svg,.pdf,.txt,.md,.csv,.json,.zip,.xlsx,.docx,.html";
