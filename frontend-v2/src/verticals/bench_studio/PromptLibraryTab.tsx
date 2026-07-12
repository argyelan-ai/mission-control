"use client";

import { useMemo, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Pencil, Play, Plus, Trash2 } from "lucide-react";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { Pill } from "@/components/shared/Pill";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { benchApi } from "@/verticals/bench_studio/api";
import type { BenchChallenge, PromptTemplate } from "./types";

export function PromptLibraryTab({
  onStartChallenge,
}: {
  onStartChallenge: (tpl: PromptTemplate) => void;
}) {
  const qc = useQueryClient();
  const [search, setSearch] = useState("");
  const [activeTag, setActiveTag] = useState<string | null>(null);
  const [editorOpen, setEditorOpen] = useState(false);
  const [editing, setEditing] = useState<PromptTemplate | null>(null);

  const { data: templates } = useQuery({
    queryKey: ["prompt-templates"],
    queryFn: benchApi.promptTemplates.list,
  });
  // Usage history: challenges referencing a template via the frozen-copy FK.
  const { data: challenges } = useQuery({
    queryKey: ["bench-challenges"],
    queryFn: () => benchApi.challenges.list(),
  });

  const removeMutation = useMutation({
    mutationFn: (id: string) => benchApi.promptTemplates.remove(id),
    onSuccess: () => {
      notify.success("Template gelöscht");
      qc.invalidateQueries({ queryKey: ["prompt-templates"] });
    },
    onError: () => notify.error("Löschen fehlgeschlagen"),
  });

  const allTags = useMemo(
    () => Array.from(new Set((templates ?? []).flatMap((t) => t.tags ?? []))).sort(),
    [templates]
  );

  const filtered = (templates ?? []).filter((t) => {
    const q = search.trim().toLowerCase();
    const matchesSearch =
      !q || t.title.toLowerCase().includes(q) || t.body.toLowerCase().includes(q);
    const matchesTag = !activeTag || (t.tags ?? []).includes(activeTag);
    return matchesSearch && matchesTag;
  });


  const usageByTemplate = useMemo(() => {
    const map: Record<string, BenchChallenge[]> = {};
    for (const ch of challenges ?? []) {
      if (ch.prompt_template_id) {
        (map[ch.prompt_template_id] ??= []).push(ch);
      }
    }
    return map;
  }, [challenges]);

  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-center gap-3">
        <input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Suche …"
          className="flex-1 rounded-lg p-2.5 text-sm outline-none"
          style={{
            backgroundColor: C.bgDeep,
            color: C.textPrimary,
            border: `1px solid ${C.border}`,
          }}
        />
        <button
          onClick={() => {
            setEditing(null);
            setEditorOpen(true);
          }}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium shrink-0"
          style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
        >
          <Plus size={14} /> Neues Template
        </button>
      </div>

      {allTags.length > 0 && (
        <div className="flex flex-wrap gap-1.5">
          {allTags.map((tag) => (
            <button
              key={tag}
              aria-label={tag}
              onClick={() => setActiveTag(activeTag === tag ? null : tag)}
              className="inline-flex items-center rounded-full px-2 py-1 text-[10px] font-semibold uppercase tracking-[0.06em] leading-none whitespace-nowrap"
              style={{
                color: activeTag === tag ? C.accent : C.textMuted,
                border: `1px solid ${activeTag === tag ? C.borderAccent : C.borderSubtle}`,
              }}
            >
              # {tag}
            </button>
          ))}
        </div>
      )}

      {filtered.length === 0 && (
        <div
          className="py-12 text-center text-sm rounded-xl"
          style={{ color: C.textSecondary, backgroundColor: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
        >
          Keine Templates gefunden.
        </div>
      )}

      {filtered.map((tpl) => {
        const usage = (usageByTemplate[tpl.id] ?? []).sort(
          (a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        );
        return (
          <div
            key={tpl.id}
            className="rounded-xl px-4 py-3 flex flex-col gap-2"
            style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
          >
            <div className="flex items-center justify-between gap-3">
              <span className="text-sm font-medium truncate" style={{ color: C.textPrimary }}>
                {tpl.title}
              </span>
              <div className="flex items-center gap-3 shrink-0">
                <button
                  onClick={() => onStartChallenge(tpl)}
                  aria-label="Challenge starten"
                  className="flex items-center gap-1 text-xs font-medium"
                  style={{ color: C.accent }}
                >
                  <Play size={12} /> Challenge starten
                </button>
                <button
                  onClick={() => {
                    setEditing(tpl);
                    setEditorOpen(true);
                  }}
                  aria-label="Bearbeiten"
                  style={{ color: C.textSecondary }}
                >
                  <Pencil size={13} />
                </button>
                <button
                  onClick={() => {
                    if (!window.confirm(`Template "${tpl.title}" wirklich löschen?`)) return;
                    removeMutation.mutate(tpl.id);
                  }}
                  aria-label="Löschen"
                  style={{ color: C.textMuted }}
                >
                  <Trash2 size={13} />
                </button>
              </div>
            </div>
            <p className="text-xs line-clamp-2" style={{ color: C.textSecondary }}>
              {tpl.body}
            </p>
            <div className="flex flex-wrap items-center gap-1.5">
              {(tpl.tags ?? []).map((tag) => (
                <Pill key={tag} color={C.textMuted} variant="outline">
                  {tag}
                </Pill>
              ))}
              {usage.length > 0 && (
                <span className="text-xs ml-auto" style={{ color: C.textMuted }}>
                  {usage.length}× verwendet — zuletzt „{usage[0].title}"
                </span>
              )}
            </div>
          </div>
        );
      })}

      <TemplateEditor
        open={editorOpen}
        onClose={() => setEditorOpen(false)}
        editing={editing}
      />
    </div>
  );
}

function TemplateEditor({
  open,
  onClose,
  editing,
}: {
  open: boolean;
  onClose: () => void;
  editing: PromptTemplate | null;
}) {
  const qc = useQueryClient();
  const [title, setTitle] = useState(editing?.title ?? "");
  const [body, setBody] = useState(editing?.body ?? "");
  const [tags, setTags] = useState((editing?.tags ?? []).join(", "));

  // Re-sync when a different template is opened:
  const [lastEditingId, setLastEditingId] = useState<string | null>(null);
  if ((editing?.id ?? null) !== lastEditingId) {
    setLastEditingId(editing?.id ?? null);
    setTitle(editing?.title ?? "");
    setBody(editing?.body ?? "");
    setTags((editing?.tags ?? []).join(", "));
  }

  const mutation = useMutation({
    mutationFn: () => {
      const payload = {
        title,
        body,
        tags: tags.split(",").map((t) => t.trim()).filter(Boolean),
      };
      return editing
        ? benchApi.promptTemplates.update(editing.id, payload)
        : benchApi.promptTemplates.create(payload);
    },
    onSuccess: () => {
      notify.success(editing ? "Template aktualisiert" : "Template erstellt");
      qc.invalidateQueries({ queryKey: ["prompt-templates"] });
      onClose();
    },
    onError: () => notify.error("Speichern fehlgeschlagen"),
  });

  const inputStyle = {
    backgroundColor: C.bgDeep,
    color: C.textPrimary,
    border: `1px solid ${C.border}`,
  } as const;

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label="Template-Editor">
      <div
        className="flex flex-col gap-3 p-5 rounded-xl w-full"
        style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
      >
        <h3 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {editing ? "Template bearbeiten" : "Neues Template"}
        </h3>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Titel"
          className="rounded-lg p-2.5 text-sm outline-none"
          style={inputStyle}
        />
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={6}
          placeholder="Prompt-Text …"
          className="rounded-lg p-3 text-sm resize-none outline-none"
          style={inputStyle}
        />
        <input
          value={tags}
          onChange={(e) => setTags(e.target.value)}
          placeholder="Tags (kommagetrennt)"
          className="rounded-lg p-2.5 text-sm outline-none"
          style={inputStyle}
        />
        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            className="px-3 py-1.5 rounded-lg text-sm"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            Abbrechen
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={!title.trim() || !body.trim() || mutation.isPending}
            className="px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-40"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            Speichern
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}
