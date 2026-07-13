"use client";

import { C } from "@/lib/colors";
import type { FsRoot } from "@/lib/types";

// Substring-matched against the indexed mime type on the backend
// (`FileIndexEntry.mime.ilike(f"%{type}%")`) — these are the practical
// buckets an operator searches by, not an exhaustive mime taxonomy.
const TYPES = ["image", "video", "audio", "pdf", "markdown", "code"] as const;

export interface FilesSearchFilterState {
  type?: string;
  agent?: string;
  root?: string;
}

interface FilesSearchFiltersProps {
  filters: FilesSearchFilterState;
  onChange: (next: Partial<FilesSearchFilterState>) => void;
  roots: FsRoot[];
  /** Known agent slugs — derived from the current result set so the dropdown
   *  only ever offers agents that actually produced indexed files. */
  agents: string[];
}

function FilterSelect({
  label, value, options, onChange,
}: {
  label: string;
  value: string;
  options: { value: string; label: string }[];
  onChange: (v: string) => void;
}) {
  return (
    <select
      aria-label={label}
      value={value}
      onChange={(e) => onChange(e.target.value)}
      className="text-xs rounded-lg px-2.5 py-1.5 cursor-pointer outline-none"
      style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: value ? C.textPrimary : C.textMuted }}
    >
      <option value="">{label}</option>
      {options.map((o) => (
        <option key={o.value} value={o.value}>{o.label}</option>
      ))}
    </select>
  );
}

/** Dezent filter row shown next to search results — narrows by file type,
 *  the agent that produced the file, and the root it lives in. All three
 *  are optional and independent (backend ANDs whichever are set). */
export function FilesSearchFilters({ filters, onChange, roots, agents }: FilesSearchFiltersProps) {
  return (
    <div className="flex items-center gap-2 flex-wrap mb-3">
      <FilterSelect
        label="Type"
        value={filters.type ?? ""}
        onChange={(v) => onChange({ type: v || undefined })}
        options={TYPES.map((t) => ({ value: t, label: t }))}
      />
      <FilterSelect
        label="Agent"
        value={filters.agent ?? ""}
        onChange={(v) => onChange({ agent: v || undefined })}
        options={agents.map((a) => ({ value: a, label: a }))}
      />
      <FilterSelect
        label="Root"
        value={filters.root ?? ""}
        onChange={(v) => onChange({ root: v || undefined })}
        options={roots.map((r) => ({ value: r.key, label: r.label }))}
      />
    </div>
  );
}
