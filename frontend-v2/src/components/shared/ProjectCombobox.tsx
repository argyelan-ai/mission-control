"use client";

import { useState, useRef, useEffect } from "react";
import { motion, AnimatePresence } from "framer-motion";
import { Search, Plus, ChevronDown, X } from "lucide-react";
import type { Project } from "@/lib/types";
import { C, LANE } from "@/lib/colors";

interface ProjectComboboxProps {
  projects: Project[];
  value: string | null;
  onChange: (projectId: string | null) => void;
  onCreateProject: (name: string, projectType: string) => Promise<Project>;
  accent?: string;
  textPrimary?: string;
  textMuted?: string;
  textSecondary?: string;
  border?: string;
  deep?: string;
}

const STATUS_DOT: Record<string, { color: string; label: string }> = {
  active: { color: LANE.in_progress, label: "active" }, // C.info — active work
  draft:  { color: C.textDim,        label: "draft" },  // neutral, dim
  planning: { color: C.info,         label: "planning" },
  paused: { color: C.warning,        label: "paused" },
  done:   { color: C.online,         label: "done" },
  archived: { color: C.textDim,      label: "archived" },
};

const PROJECT_TYPES = [
  { value: "feature", label: "Feature" },
  { value: "website", label: "Website" },
  { value: "content", label: "Content" },
  { value: "research", label: "Research" },
  { value: "automation", label: "Automation" },
  { value: "design", label: "Design" },
  { value: "free", label: "Free-form" },
];

const STATUS_ORDER = ["active", "draft", "planning", "paused", "done", "archived"];

export function ProjectCombobox({
  projects,
  value,
  onChange,
  onCreateProject,
  accent = C.accent,
  textPrimary = C.textPrimary,
  textMuted = C.textDim,
  textSecondary = C.textSecondary,
  border = C.border,
  deep = C.bgDeep,
}: ProjectComboboxProps) {
  const [open, setOpen] = useState(false);
  const [search, setSearch] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newType, setNewType] = useState("feature");
  const [creating, setCreating] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const selected = projects.find((p) => p.id === value);

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) {
        setOpen(false);
        setShowCreate(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  const sorted = [...projects]
    .filter((p) => p.status !== "archived")
    .sort((a, b) => STATUS_ORDER.indexOf(a.status) - STATUS_ORDER.indexOf(b.status));

  const filtered = sorted.filter((p) =>
    p.name.toLowerCase().includes(search.toLowerCase())
  );

  const handleCreate = async () => {
    if (!newName.trim() || creating) return;
    setCreating(true);
    try {
      const project = await onCreateProject(newName.trim(), newType);
      onChange(project.id);
      setOpen(false);
      setShowCreate(false);
      setNewName("");
      setNewType("feature");
    } finally {
      setCreating(false);
    }
  };

  return (
    <div ref={ref} className="relative">
      <button
        type="button"
        onClick={() => setOpen(!open)}
        className="flex items-center gap-2 w-full text-left px-3 py-2 rounded-xl text-[12px] transition-all cursor-pointer"
        style={{
          backgroundColor: deep,
          border: `1px solid ${value ? `${accent}66` : border}`,
          color: value ? textPrimary : textMuted,
        }}
      >
        <span className="flex-1 truncate">
          {selected ? selected.name : "Project (optional)"}
        </span>
        {value ? (
          <X
            size={12}
            className="shrink-0 opacity-50 hover:opacity-100 transition-opacity"
            onClick={(e) => {
              e.stopPropagation();
              onChange(null);
            }}
          />
        ) : (
          <ChevronDown size={12} className="shrink-0 opacity-50" />
        )}
      </button>

      <AnimatePresence>
        {open && (
          <motion.div
            initial={{ opacity: 0, y: -4 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.12 }}
            className="absolute z-50 mt-1 w-full rounded-xl overflow-hidden"
            style={{
              backgroundColor: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            <div className="flex items-center gap-2 px-3 py-2" style={{ borderBottom: `1px solid ${border}` }}>
              <Search size={12} style={{ color: textMuted }} />
              <input
                ref={inputRef}
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="Search projects..."
                className="flex-1 bg-transparent text-[11px] outline-none"
                style={{ color: textPrimary }}
              />
            </div>

            <div className="max-h-[200px] overflow-y-auto">
              {filtered.map((p) => {
                const dot = STATUS_DOT[p.status] ?? STATUS_DOT.draft;
                return (
                  <button
                    key={p.id}
                    type="button"
                    onClick={() => {
                      onChange(p.id);
                      setOpen(false);
                      setSearch("");
                    }}
                    className="flex items-center gap-2 w-full px-3 py-2 text-left transition-colors hover:bg-white/5 cursor-pointer"
                  >
                    <span className="flex-1 text-[11px] truncate" style={{ color: textPrimary }}>
                      {p.name}
                    </span>
                    <span className="flex items-center gap-1.5 text-[9px]" style={{ color: textSecondary }}>
                      {dot.label}
                      <span
                        className="w-1.5 h-1.5 rounded-full"
                        style={{ backgroundColor: dot.color }}
                      />
                    </span>
                  </button>
                );
              })}
              {filtered.length === 0 && (
                <div className="px-3 py-3 text-[11px] text-center" style={{ color: textMuted }}>
                  No project found
                </div>
              )}
            </div>

            <div style={{ borderTop: `1px solid ${border}` }}>
              {!showCreate ? (
                <button
                  type="button"
                  onClick={() => setShowCreate(true)}
                  className="flex items-center gap-2 w-full px-3 py-2.5 text-[11px] font-medium transition-colors hover:bg-white/5 cursor-pointer"
                  style={{ color: accent }}
                >
                  <Plus size={12} />
                  Create new project
                </button>
              ) : (
                <div className="p-3 flex flex-col gap-2">
                  <input
                    autoFocus
                    value={newName}
                    onChange={(e) => setNewName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") handleCreate();
                      if (e.key === "Escape") setShowCreate(false);
                    }}
                    placeholder="Project name"
                    className="w-full text-[11px] px-2.5 py-1.5 rounded-lg bg-transparent outline-none"
                    style={{ border: `1px solid ${border}`, color: textPrimary }}
                  />
                  <select
                    value={newType}
                    onChange={(e) => setNewType(e.target.value)}
                    className="w-full text-[11px] px-2.5 py-1.5 rounded-lg outline-none cursor-pointer"
                    style={{ backgroundColor: deep, border: `1px solid ${border}`, color: textSecondary }}
                  >
                    {PROJECT_TYPES.map((t) => (
                      <option key={t.value} value={t.value}>{t.label}</option>
                    ))}
                  </select>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => setShowCreate(false)}
                      className="flex-1 text-[10px] py-1 rounded-lg cursor-pointer"
                      style={{ color: textMuted, border: `1px solid ${border}` }}
                    >
                      Cancel
                    </button>
                    <button
                      type="button"
                      onClick={handleCreate}
                      disabled={!newName.trim() || creating}
                      className="flex-1 text-[10px] py-1 rounded-lg font-medium cursor-pointer disabled:opacity-30"
                      style={{ backgroundColor: `${accent}22`, color: accent, border: `1px solid ${accent}66` }}
                    >
                      {creating ? "..." : "Create"}
                    </button>
                  </div>
                </div>
              )}
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
