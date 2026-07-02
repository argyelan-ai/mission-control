"use client";

import { useState, useEffect } from "react";
import AppShell from "@/components/layout/AppShell";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { motion, AnimatePresence } from "framer-motion";
import ReactMarkdown from "react-markdown";
import {
  Search, RefreshCw, Terminal, Package, Server, Plus,
  ChevronDown, Zap, Globe, Code2, Database, GitBranch,
  Cpu, Box, LayoutGrid, FileText, X, Pencil, Eye,
  Save, Check,
} from "lucide-react";
import { api } from "@/lib/api";
import type { MCPServer, Agent } from "@/lib/types";
import { PluginMatrix } from "@/components/shared/PluginMatrix";
import { SkillMatrix } from "@/components/shared/SkillMatrix";
import { PluginsShellTab } from "@/components/plugins/PluginsShellTab";
import { MCPServerMatrix } from "@/components/mcp/MCPServerMatrix";
import { MCPAddServerModal } from "@/components/mcp/MCPAddServerModal";
import { notify } from "@/lib/notify";
import { C } from "@/lib/colors";

const SK = {
  bg: "rgba(255,255,255,0.03)",
  bgHover: "rgba(255,255,255,0.045)",
} as const;

type LocalSkill = { name: string; key?: string; description?: string };
type SkillsListResponse = { skills?: LocalSkill[]; total?: number };

const CATEGORY_META: Record<string, { icon: React.ElementType; color: string }> = {
  "MC Custom":  { icon: Zap,       color: C.accent },
  "Git":        { icon: GitBranch, color: "#F59E0B" },
  "Database":   { icon: Database,  color: C.info },
  "Web":        { icon: Globe,     color: "#00CC88" },
  "Coding":     { icon: Code2,     color: "#F97316" },
  "AI":         { icon: Cpu,       color: C.accent },
  "Content":    { icon: FileText,  color: C.info },
  "System":     { icon: Box,       color: C.textSecondary },
};

function guessCategory(skill: LocalSkill): string {
  const k = (skill.key ?? skill.name).toLowerCase();
  if (k.startsWith("mc-")) return "MC Custom";
  if (k.includes("git") || k.includes("github")) return "Git";
  if (k.includes("database") || k.includes("sql")) return "Database";
  if (k.includes("web") || k.includes("scroll") || k.includes("gsap") || k.includes("3d-web") || k.includes("stunning") || k.includes("shadcn")) return "Web";
  if (k.includes("code") || k.includes("coding")) return "Coding";
  if (k.includes("ai") || k.includes("llm") || k.includes("higgsfield") || k.includes("soul") || k.includes("grok")) return "AI";
  if (k.includes("argyelan") || k.includes("stitch") || k.includes("brand") || k.includes("summarize") || k.includes("nano-pdf")) return "Content";
  return "System";
}

// ── Markdown renderer ──────────────────────────────────────────────────────
function MdContent({ content }: { content: string }) {
  return (
    <ReactMarkdown components={{
      h1: ({ children }) => <h1 className="text-xl font-bold mb-4 mt-2" style={{ color: "var(--color-text-primary)" }}>{children}</h1>,
      h2: ({ children }) => <h2 className="text-base font-semibold mb-3 mt-5 pb-2" style={{ color: "var(--color-text-primary)", borderBottom: `1px solid ${C.borderSubtle}` }}>{children}</h2>,
      h3: ({ children }) => <h3 className="text-sm font-semibold mb-2 mt-4" style={{ color: "var(--color-text-primary)" }}>{children}</h3>,
      p: ({ children }) => <p className="text-sm mb-3 leading-relaxed" style={{ color: "var(--color-text-secondary)" }}>{children}</p>,
      ul: ({ children }) => <ul className="mb-3 pl-4 space-y-1.5">{children}</ul>,
      ol: ({ children }) => <ol className="mb-3 pl-4 space-y-1.5 list-decimal">{children}</ol>,
      li: ({ children }) => <li className="text-sm leading-relaxed list-disc" style={{ color: "var(--color-text-secondary)" }}>{children}</li>,
      code: ({ children, className }) => {
        const isBlock = className?.includes("language-");
        return isBlock ? (
          <code className="block px-4 py-3 rounded-xl text-xs font-mono mb-3 overflow-x-auto whitespace-pre"
            style={{ background: "rgba(255,255,255,0.04)", color: C.accent, border: `1px solid ${C.border}` }}>{children}</code>
        ) : (
          <code className="px-1.5 py-0.5 rounded text-[11px] font-mono"
            style={{ background: "rgba(255,255,255,0.08)", color: C.accent }}>{children}</code>
        );
      },
      pre: ({ children }) => <div className="mb-3">{children}</div>,
      strong: ({ children }) => <strong className="font-semibold" style={{ color: "var(--color-text-primary)" }}>{children}</strong>,
    }}>{content}</ReactMarkdown>
  );
}

// ── Skill Content Modal ────────────────────────────────────────────────────
function SkillContentModal({ skillKey, onClose }: { skillKey: string; onClose: () => void }) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"view" | "edit">("view");
  const [editContent, setEditContent] = useState("");
  const [writeTab, setWriteTab] = useState<"write" | "preview">("write");
  const [dirty, setDirty] = useState(false);
  const [savedToast, setSavedToast] = useState(false);

  const { data, isLoading, error } = useQuery({
    queryKey: ["skill-content", skillKey],
    queryFn: () => api.skills.content(skillKey),
  });

  useEffect(() => { if (data?.content !== undefined) setEditContent(data.content); }, [data?.content]);
  useEffect(() => {
    const h = (e: KeyboardEvent) => { if (e.key === "Escape") onClose(); };
    document.addEventListener("keydown", h);
    return () => document.removeEventListener("keydown", h);
  }, [onClose]);

  const saveMutation = useMutation({
    mutationFn: (content: string) => api.skills.saveContent(skillKey, content),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["skill-content", skillKey] });
      setDirty(false);
      setMode("view");
      setSavedToast(true);
      setTimeout(() => setSavedToast(false), 2500);
    },
  });

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center">
      <div className="absolute inset-0 bg-black/75" onClick={onClose} />
      <motion.div
        initial={{ scale: 0.95, opacity: 0 }} animate={{ scale: 1, opacity: 1 }}
        exit={{ scale: 0.95, opacity: 0 }} transition={{ duration: 0.2, ease: [0.16, 1, 0.3, 1] }}
        className="relative z-10 flex flex-col rounded-2xl overflow-hidden"
        role="dialog"
        aria-modal="true"
        aria-label={`Skill: ${skillKey}`}
        style={{ width: "min(820px, 95vw)", height: "85vh", background: C.bgBase,
          border: `1px solid ${C.border}`, boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)" }}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="absolute inset-x-0 top-0 h-px pointer-events-none"
          style={{ background: "linear-gradient(90deg, transparent, rgba(255,255,255,0.1) 50%, transparent)" }} />

        {/* Header */}
        <div className="flex items-center gap-3 px-5 py-3.5 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
          <div className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0" style={{ background: C.accentSubtle }}>
            <FileText size={13} style={{ color: C.accent }} />
          </div>
          <div className="flex-1 min-w-0">
            <div className="text-sm font-semibold" style={{ color: "var(--color-text-primary)" }}>
              {skillKey} <span className="ml-2 text-[11px] font-normal" style={{ color: "var(--color-text-muted)" }}>SKILL.md</span>
            </div>
            {data?.path && <div className="text-[10px] font-mono truncate" style={{ color: "var(--color-text-muted)" }}>{data.path}</div>}
          </div>
          <div className="flex items-center gap-1.5">
            {mode === "view" && !error && !isLoading && (
              <button onClick={() => { setEditContent(data?.content ?? ""); setDirty(false); setWriteTab("write"); setMode("edit"); }}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
                style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}>
                <Pencil size={11} /> Bearbeiten
              </button>
            )}
            {mode === "edit" && (
              <>
                <button onClick={() => setMode("view")} className="px-3 py-1.5 text-xs rounded-lg cursor-pointer" style={{ color: "var(--color-text-muted)" }}>Abbrechen</button>
                <button onClick={() => saveMutation.mutate(editContent)} disabled={saveMutation.isPending || !dirty}
                  className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
                  style={{ background: dirty ? `linear-gradient(135deg, ${C.accent}, ${C.accentHover})` : "rgba(255,255,255,0.05)",
                    color: dirty ? "white" : "var(--color-text-muted)", cursor: !dirty ? "default" : "pointer" }}>
                  {saveMutation.isPending ? <><RefreshCw size={11} className="animate-spin" /> Speichern...</> : <><Save size={11} /> Speichern</>}
                </button>
              </>
            )}
            <button onClick={onClose} className="p-1.5 rounded-lg cursor-pointer ml-1" style={{ color: "var(--color-text-muted)" }}><X size={15} /></button>
          </div>
        </div>

        {/* Body */}
        <div className="flex-1 overflow-hidden flex flex-col">
          {isLoading ? (
            <div className="flex items-center justify-center h-48"><RefreshCw size={18} className="animate-spin" style={{ color: C.accent }} /></div>
          ) : error ? (
            <div className="flex flex-col items-center justify-center h-48 gap-3">
              <FileText size={22} style={{ color: "var(--color-text-muted)" }} />
              <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>Keine SKILL.md gefunden</p>
              <button onClick={() => { setEditContent("---\nname: " + skillKey + '\ndescription: ""\n---\n\n# ' + skillKey + "\n\n"); setDirty(true); setMode("edit"); }}
                className="flex items-center gap-2 px-4 py-2 rounded-xl text-sm font-medium cursor-pointer"
                style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}>
                <FileText size={13} /> Neue SKILL.md erstellen
              </button>
            </div>
          ) : mode === "view" ? (
            <div className="flex-1 overflow-y-auto px-6 py-5"><MdContent content={data?.content ?? ""} /></div>
          ) : (
            <div className="flex-1 flex flex-col overflow-hidden">
              <div className="flex items-center gap-1 px-5 pt-3 pb-2 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                {(["write", "preview"] as const).map((tab) => (
                  <button key={tab} onClick={() => setWriteTab(tab)}
                    className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs cursor-pointer"
                    style={{ background: writeTab === tab ? "rgba(255,255,255,0.07)" : "transparent",
                      color: writeTab === tab ? "var(--color-text-primary)" : "var(--color-text-muted)" }}>
                    {tab === "write" ? <><Pencil size={11} /> Schreiben</> : <><Eye size={11} /> Vorschau</>}
                  </button>
                ))}
                {dirty && <span className="ml-auto text-[10px] px-2 py-0.5 rounded-full"
                  style={{ background: "rgba(245,158,11,0.1)", color: C.warning, border: "1px solid rgba(245,158,11,0.2)" }}>Ungespeichert</span>}
              </div>
              <div className="flex-1 overflow-hidden">
                {writeTab === "write" ? (
                  <textarea value={editContent} onChange={(e) => { setEditContent(e.target.value); setDirty(true); }}
                    spellCheck={false} className="w-full h-full px-6 py-4 text-sm outline-none resize-none"
                    style={{ background: "transparent", color: "var(--color-text-body)",
                      fontFamily: 'ui-monospace, "Cascadia Code", "Fira Code", monospace', lineHeight: 1.7, fontSize: 13 }}
                    placeholder="Markdown hier eingeben..." />
                ) : (
                  <div className="h-full overflow-y-auto px-6 py-5">
                    {editContent.trim() ? <MdContent content={editContent} /> : <p className="text-sm italic" style={{ color: "var(--color-text-muted)" }}>Noch kein Inhalt.</p>}
                  </div>
                )}
              </div>
            </div>
          )}
        </div>

        <AnimatePresence>
          {savedToast && (
            <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}
              className="absolute bottom-4 left-1/2 -translate-x-1/2 flex items-center gap-2 px-4 py-2 rounded-full text-xs font-medium pointer-events-none"
              style={{ background: "rgba(0,204,136,0.15)", border: "1px solid rgba(0,204,136,0.3)", color: C.online }}>
              <Check size={12} /> Gespeichert
            </motion.div>
          )}
        </AnimatePresence>
      </motion.div>
    </div>
  );
}

// ── Skill Row ──────────────────────────────────────────────────────────────
function SkillRow({ skill, isLast }: { skill: LocalSkill; isLast: boolean }) {
  const [expanded, setExpanded] = useState(false);
  const [showContent, setShowContent] = useState(false);
  const id = skill.key ?? skill.name;

  return (
    <div style={{ borderBottom: isLast ? "none" : `1px solid ${C.borderSubtle}` }}>
      <div className="flex items-center gap-3 px-4 py-3 cursor-pointer transition-colors group"
        onClick={() => skill.description && setExpanded((x) => !x)}
        onMouseEnter={(e) => (e.currentTarget.style.background = SK.bgHover)}
        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
        <div className="w-[3px] self-stretch rounded-full shrink-0" style={{ background: C.online, minHeight: 20 }} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>{skill.name}</span>
            {skill.name !== id && <span className="text-[11px] font-mono" style={{ color: "var(--color-text-muted)" }}>{id}</span>}
          </div>
          {skill.description && !expanded && (
            <p className="text-xs truncate mt-0.5" style={{ color: "var(--color-text-muted)" }}>{skill.description}</p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <button onClick={(e) => { e.stopPropagation(); setShowContent(true); }}
            className="p-1 rounded transition-colors opacity-0 group-hover:opacity-100 cursor-pointer touch-visible"
            style={{ color: "var(--color-text-muted)" }} title="SKILL.md anzeigen">
            <FileText size={12} />
          </button>
          {skill.description && (
            <ChevronDown size={13} className="transition-transform duration-200"
              style={{ color: "var(--color-text-muted)", transform: expanded ? "rotate(180deg)" : "rotate(0deg)" }} />
          )}
        </div>
      </div>

      <AnimatePresence>{showContent && <SkillContentModal skillKey={id} onClose={() => setShowContent(false)} />}</AnimatePresence>

      <AnimatePresence>
        {expanded && (
          <motion.div initial={{ height: 0, opacity: 0 }} animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }} transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }} className="overflow-hidden">
            <div className="px-5 pb-4 pt-1 ml-[7px]" style={{ borderLeft: `2px solid ${C.borderSubtle}` }}>
              <p className="text-xs leading-relaxed" style={{ color: "var(--color-text-secondary)" }}>{skill.description}</p>
            </div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

// ── Category Group ─────────────────────────────────────────────────────────
function CategoryGroup({ category, skills, index }: { category: string; skills: LocalSkill[]; index: number }) {
  const [collapsed, setCollapsed] = useState(false);
  const meta = CATEGORY_META[category] ?? { icon: LayoutGrid, color: C.textSecondary };
  const CatIcon = meta.icon;

  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }}
      transition={{ delay: index * 0.05, duration: 0.2 }}
      className="rounded-2xl overflow-hidden" style={{ background: SK.bg, border: `1px solid ${C.border}` }}>
      <button onClick={() => setCollapsed((x) => !x)}
        className="w-full flex items-center gap-3 px-4 py-3 cursor-pointer transition-colors text-left"
        style={{ borderBottom: collapsed ? "none" : `1px solid ${C.borderSubtle}` }}
        onMouseEnter={(e) => (e.currentTarget.style.background = SK.bgHover)}
        onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}>
        <div className="w-7 h-7 rounded-lg flex items-center justify-center shrink-0" style={{ background: `${meta.color}15` }}>
          <CatIcon size={14} style={{ color: meta.color }} />
        </div>
        <span className="text-sm font-semibold flex-1" style={{ color: "var(--color-text-primary)" }}>{category}</span>
        <span className="text-[11px]" style={{ color: "var(--color-text-muted)" }}>{skills.length} Skills</span>
        <ChevronDown size={13} className="transition-transform duration-200"
          style={{ color: "var(--color-text-muted)", transform: collapsed ? "rotate(-90deg)" : "rotate(0deg)" }} />
      </button>
      <AnimatePresence>
        {!collapsed && (
          <motion.div initial={{ height: 0 }} animate={{ height: "auto" }} exit={{ height: 0 }}
            transition={{ duration: 0.18, ease: [0.16, 1, 0.3, 1] }} className="overflow-hidden">
            {skills.map((skill, i) => <SkillRow key={skill.key ?? skill.name} skill={skill} isLast={i === skills.length - 1} />)}
          </motion.div>
        )}
      </AnimatePresence>
    </motion.div>
  );
}

// ── Page ───────────────────────────────────────────────────────────────────
export default function SkillsPage() {
  const [activeTab, setActiveTab] = useState<"local" | "plugins" | "mcp" | "installer">("local");
  const [search, setSearch] = useState("");
  const [showAddMcpModal, setShowAddMcpModal] = useState(false);

  const qc = useQueryClient();

  const { data: skillsData, isLoading, error: skillsError, refetch, isRefetching } = useQuery<SkillsListResponse>({
    queryKey: ["skills"],
    queryFn: () => api.skills.list() as unknown as Promise<SkillsListResponse>,
    refetchInterval: 60_000,
  });

  const installedSkills: LocalSkill[] = skillsData?.skills ?? [];

  const filtered = installedSkills.filter((s) => {
    if (!search) return true;
    const q = search.toLowerCase();
    return (s.key ?? s.name).toLowerCase().includes(q)
      || s.name.toLowerCase().includes(q)
      || s.description?.toLowerCase().includes(q);
  });

  const grouped: Record<string, LocalSkill[]> = {};
  for (const skill of filtered) {
    const cat = guessCategory(skill);
    if (!grouped[cat]) grouped[cat] = [];
    grouped[cat].push(skill);
  }
  const categoryOrder = Object.keys(grouped).sort((a, b) => {
    if (a === "MC Custom") return -1;
    if (b === "MC Custom") return 1;
    return a.localeCompare(b);
  });

  const { data: mcpServers } = useQuery<MCPServer[]>({
    queryKey: ["mcp-servers"],
    queryFn: () => api.mcpServers.list(),
    staleTime: 30_000,
    enabled: activeTab === "mcp",
  });

  const { data: allAgents } = useQuery<Agent[]>({
    queryKey: ["agents"],
    queryFn: () => api.agents.list(),
    staleTime: 30_000,
    enabled: activeTab === "mcp",
  });

  const deleteMcpMutation = useMutation({
    mutationFn: (name: string) => api.mcpServers.delete(name),
    onSuccess: (data) => {
      qc.invalidateQueries({ queryKey: ["mcp-servers"] });
      qc.invalidateQueries({ queryKey: ["agents"] });
      const affected = data.cleaned_agents.length;
      notify.success(affected > 0 ? `Server entfernt. ${affected} Agent(s) aktualisiert.` : "Server entfernt.");
    },
    onError: (e: Error) => notify.error(e.message ?? "Server konnte nicht entfernt werden"),
  });

  function handleMcpDelete(name: string) {
    if (!confirm(`MCP-Server "${name}" wirklich entfernen?\nAlle Agent-Zuweisungen werden bereinigt.`)) return;
    deleteMcpMutation.mutate(name);
  }

  return (
    <AppShell>
      <div className="max-w-4xl mx-auto">

        {/* ── Header ── */}
        <div className="flex items-start justify-between mb-6 gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight" style={{ color: "var(--color-text-primary)" }}>Skills</h1>
            <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)" }}>
              Skills, CLI Plugins und MCP Servers — lokal aus ~/.mc/
            </p>
          </div>
          {activeTab === "local" && (
            <button onClick={() => refetch()}
              className="p-2 rounded-xl cursor-pointer transition-colors"
              style={{ background: SK.bg, border: `1px solid ${C.border}`, color: "var(--color-text-muted)" }}
              title="Aktualisieren">
              <RefreshCw size={14} className={isRefetching ? "animate-spin" : ""} />
            </button>
          )}
        </div>

        {/* ── Tabs — .tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17) ── */}
        <div className="flex items-center gap-1 mb-5 p-1 rounded-xl tab-strip w-full md:w-fit"
          style={{ background: SK.bg, border: `1px solid ${C.border}` }}>
          {([
            { id: "local",     label: "Skills",      icon: Zap,      count: installedSkills.length },
            { id: "plugins",   label: "CLI Plugins",  icon: Package,  count: 0 },
            { id: "mcp",       label: "MCP Servers",  icon: Server,   count: mcpServers?.length ?? 0 },
            { id: "installer", label: "Installer",    icon: Terminal,  count: 0 },
          ] as const).map(({ id, label, icon: Icon, count }) => (
            <button key={id}
              onClick={() => { setActiveTab(id); setSearch(""); }}
              className="flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-medium cursor-pointer transition-all whitespace-nowrap"
              style={{
                background: activeTab === id ? "rgba(255,255,255,0.07)" : "transparent",
                color: activeTab === id ? "var(--color-text-primary)" : "var(--color-text-muted)",
                boxShadow: activeTab === id ? "0 1px 3px rgba(0,0,0,0.3)" : "none",
              }}>
              <Icon size={13} />
              {label}
              {count > 0 && (
                <span className="text-[10px] font-semibold px-1.5 py-0.5 rounded-full"
                  style={{ background: activeTab === id ? C.accentSubtle : "rgba(255,255,255,0.05)",
                    color: activeTab === id ? C.accent : "var(--color-text-muted)" }}>
                  {count}
                </span>
              )}
            </button>
          ))}
        </div>

        {/* ── Content: Skills ── */}
        {activeTab === "local" && (
          <>
            {/* Search */}
            <div className="relative mb-5" style={{ maxWidth: 400 }}>
              <Search size={13} className="absolute left-3 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: "var(--color-text-muted)" }} />
              <input type="text" value={search} onChange={(e) => setSearch(e.target.value)}
                placeholder="Skill suchen…"
                aria-label="Skill suchen"
                className="w-full pl-9 pr-3 py-2 text-sm rounded-xl outline-none"
                style={{ background: SK.bg, border: `1px solid ${C.border}`, color: "var(--color-text-primary)" }} />
              {search && (
                <button onClick={() => setSearch("")}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-xs cursor-pointer"
                  style={{ color: "var(--color-text-muted)" }}>×</button>
              )}
            </div>

            {/* Error state */}
            {skillsError ? (
              <div className="flex flex-col items-center gap-2 py-8">
                <p className="text-xs" style={{ color: C.error }}>{(skillsError as Error).message}</p>
                <button onClick={() => qc.invalidateQueries({ queryKey: ["skills"] })}
                  className="text-xs underline cursor-pointer" style={{ color: C.accent }}>Erneut versuchen</button>
              </div>
            ) : isLoading ? (
              <div className="flex items-center justify-center py-12">
                <RefreshCw size={18} className="animate-spin" style={{ color: C.accent }} />
              </div>
            ) : filtered.length === 0 ? (
              <div className="flex flex-col items-center justify-center py-16 gap-2">
                <Search size={28} style={{ color: "var(--color-text-muted)" }} />
                <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>
                  {search ? "Keine Skills gefunden." : "Keine Skills in ~/.mc/skills/"}
                </p>
                {search && <button onClick={() => setSearch("")} className="text-xs underline cursor-pointer" style={{ color: C.accent }}>Suche zurücksetzen</button>}
              </div>
            ) : (
              <div className="flex flex-col gap-3">
                {search && <p className="text-xs px-1" style={{ color: "var(--color-text-muted)" }}>{filtered.length} von {installedSkills.length} Skills</p>}
                {categoryOrder.map((cat, i) => (
                  <CategoryGroup key={cat} category={cat} skills={grouped[cat]} index={i} />
                ))}
              </div>
            )}
          </>
        )}

        {/* ── Content: Installer ── */}
        {activeTab === "installer" && <PluginsShellTab />}

        {/* ── Content: Plugins ── */}
        {activeTab === "plugins" && (
          <div className="space-y-8">
            <PluginMatrix />
            <div className="h-px" style={{ background: "rgba(255,255,255,0.06)" }} />
            <SkillMatrix />
          </div>
        )}

        {/* ── Content: MCP ── */}
        {activeTab === "mcp" && (
          <div className="space-y-4">
            <div className="flex items-center justify-between">
              <p className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                {mcpServers?.length ?? 0} MCP-Server installiert · {allAgents?.length ?? 0} Agents
              </p>
              <button onClick={() => setShowAddMcpModal(true)}
                className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium cursor-pointer"
                style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}>
                <Plus size={13} /> MCP-Server hinzufügen
              </button>
            </div>
            <MCPServerMatrix servers={mcpServers ?? []} agents={allAgents ?? []} showDeleteButton onDeleteServer={handleMcpDelete} />
            <AnimatePresence>
              {showAddMcpModal && (
                <MCPAddServerModal onClose={() => setShowAddMcpModal(false)}
                  onSuccess={() => { qc.invalidateQueries({ queryKey: ["mcp-servers"] }); setShowAddMcpModal(false); }} />
              )}
            </AnimatePresence>
          </div>
        )}

      </div>
    </AppShell>
  );
}
