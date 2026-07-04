"use client";

import { useEffect, useState } from "react";
import { useMutation, useQueries, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArchiveRestore,
  ExternalLink,
  Link2,
  Loader2,
  RefreshCw,
  Search,
  Trash2,
  Unlink,
  X,
} from "lucide-react";
import { SlideOverPanel } from "@/components/shared/SlideOverPanel";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import type { Board, Project, Repo } from "@/lib/types";

/** Pull the human-readable detail out of `API 409: {"detail":"..."}` errors. */
function extractApiError(err: unknown): string {
  const msg = err instanceof Error ? err.message : String(err);
  const jsonStart = msg.indexOf("{");
  if (jsonStart >= 0) {
    try {
      const parsed = JSON.parse(msg.slice(jsonStart));
      if (typeof parsed.detail === "string") return parsed.detail;
    } catch {
      /* fall through to raw message */
    }
  }
  return msg;
}

// ── Delete confirm ────────────────────────────────────────────────────────────

function DeleteRepoDialog({
  repo,
  open,
  onClose,
  onDeleted,
}: {
  repo: Repo;
  open: boolean;
  onClose: () => void;
  onDeleted: () => void;
}) {
  const queryClient = useQueryClient();
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  const deleteMutation = useMutation({
    mutationFn: () => api.repos.remove(repo.id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["repos"] });
      notify.success(`${repo.full_name} entfernt`);
      onDeleted();
    },
    // 409 = Projekte noch verknüpft — Backend-Text zeigen statt still zu scheitern
    onError: (err) => setErrorMsg(extractApiError(err)),
  });

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="delete-repo-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2 id="delete-repo-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {repo.full_name} entfernen?
        </h2>
      </div>

      <div className="px-5 py-3">
        <p className="text-xs" style={{ color: C.textSecondary }}>
          Entfernt nur die MC-Registry — GitHub wird nicht angetastet.
        </p>
        {errorMsg && (
          <div
            className="mt-3 text-xs px-3 py-2 rounded-lg"
            style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
          >
            {errorMsg}
          </div>
        )}
      </div>

      <div
        className="flex items-center justify-end gap-2 px-5 py-3 shrink-0"
        style={{ borderTop: `1px solid ${C.borderSubtle}` }}
      >
        <button
          onClick={onClose}
          disabled={deleteMutation.isPending}
          className="px-3.5 py-2 rounded-lg text-sm font-medium transition-colors cursor-pointer disabled:opacity-60 disabled:cursor-not-allowed"
          style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
        >
          Abbrechen
        </button>
        <button
          onClick={() => { setErrorMsg(null); deleteMutation.mutate(); }}
          disabled={deleteMutation.isPending}
          className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-sm font-semibold transition-opacity cursor-pointer disabled:opacity-70 disabled:cursor-not-allowed"
          style={{ background: C.error, color: "#FFFFFF" }}
        >
          {deleteMutation.isPending ? <Loader2 size={15} className="animate-spin" /> : <Trash2 size={15} />}
          Löschen
        </button>
      </div>
    </ResponsiveModal>
  );
}

// ── Project link picker ───────────────────────────────────────────────────────

function LinkProjectPicker({ repo, onClose }: { repo: Repo; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [search, setSearch] = useState("");

  const { data: boards } = useQuery<Board[]>({ queryKey: ["boards"], queryFn: api.boards.list });

  const projectQueries = useQueries({
    queries: (boards ?? []).map((b) => ({
      queryKey: ["board-projects", b.id],
      queryFn: () => api.projects.list(b.id),
      staleTime: 15_000,
    })),
  });

  const linkedIds = new Set(repo.linked_projects.map((p) => p.id));
  const boardNameOf = new Map((boards ?? []).map((b) => [b.id, b.name]));
  const allProjects: Project[] = projectQueries.flatMap((q) => q.data ?? []);
  const candidates = allProjects
    .filter((p) => !linkedIds.has(p.id))
    .filter((p) => p.name.toLowerCase().includes(search.toLowerCase()));

  const linkMutation = useMutation({
    mutationFn: (projectId: string) => api.repos.linkProject(repo.id, projectId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ["repo", repo.id] });
      queryClient.invalidateQueries({ queryKey: ["repos"] });
    },
    onError: () => notify.error("Verknüpfen fehlgeschlagen"),
  });

  const isLoading = projectQueries.some((q) => q.isLoading);

  return (
    <div
      className="mt-2 rounded-lg overflow-hidden"
      style={{ background: C.bgDeep, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-center gap-2 px-3 py-2" style={{ borderBottom: `1px solid ${C.border}` }}>
        <Search size={12} style={{ color: C.textMuted }} />
        <input
          autoFocus
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Projekt suchen..."
          aria-label="Projekt suchen"
          className="flex-1 bg-transparent text-xs outline-none"
          style={{ color: C.textPrimary }}
        />
        <button onClick={onClose} aria-label="Schliessen" className="cursor-pointer">
          <X size={12} style={{ color: C.textMuted }} />
        </button>
      </div>
      <div className="max-h-[180px] overflow-y-auto">
        {isLoading && (
          <div className="flex items-center gap-2 px-3 py-3 text-xs" style={{ color: C.textMuted }}>
            <Loader2 size={11} className="animate-spin" /> Projekte laden...
          </div>
        )}
        {!isLoading && candidates.length === 0 && (
          <div className="px-3 py-3 text-xs" style={{ color: C.textMuted }}>
            Kein Projekt gefunden
          </div>
        )}
        {candidates.map((p) => (
          <button
            key={p.id}
            onClick={() => linkMutation.mutate(p.id)}
            disabled={linkMutation.isPending}
            className="flex items-center justify-between w-full px-3 py-2 text-left cursor-pointer transition-colors hover:bg-white/5 disabled:opacity-50"
          >
            <span className="text-xs truncate" style={{ color: C.textPrimary }}>{p.name}</span>
            <span className="text-[10px] shrink-0 ml-2" style={{ color: C.textMuted }}>
              {boardNameOf.get(p.board_id) ?? ""}
            </span>
          </button>
        ))}
      </div>
    </div>
  );
}

// ── Detail Panel ───────────────────────────────────────────────────────────────

export function RepoDetailPanel({
  repoId,
  open,
  onClose,
}: {
  repoId: string | null;
  open: boolean;
  onClose: () => void;
}) {
  const queryClient = useQueryClient();
  const [description, setDescription] = useState("");
  const [rulesMd, setRulesMd] = useState("");
  const [pickerOpen, setPickerOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [savedMsg, setSavedMsg] = useState(false);

  const { data: repo } = useQuery<Repo>({
    queryKey: ["repo", repoId],
    queryFn: () => api.repos.get(repoId!),
    enabled: !!repoId,
  });

  // Reset local form state whenever a different repo is opened
  useEffect(() => {
    if (repo) {
      setDescription(repo.description ?? "");
      setRulesMd(repo.rules_md ?? "");
      setSavedMsg(false);
    }
  }, [repo?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ["repo", repoId] });
    queryClient.invalidateQueries({ queryKey: ["repos"] });
  };

  const saveMutation = useMutation({
    mutationFn: () =>
      api.repos.update(repo!.id, { description: description.trim() || null, rules_md: rulesMd.trim() || null }),
    onSuccess: () => {
      invalidate();
      setSavedMsg(true);
      setTimeout(() => setSavedMsg(false), 2500);
    },
    onError: () => notify.error("Speichern fehlgeschlagen"),
  });

  const syncMutation = useMutation({
    mutationFn: () => api.repos.sync(repo!.id),
    onSuccess: () => { invalidate(); notify.success("Metadaten synchronisiert"); },
    onError: () => notify.error("Sync fehlgeschlagen"),
  });

  const archiveMutation = useMutation({
    mutationFn: () => api.repos.update(repo!.id, { is_active: !repo!.is_active }),
    onSuccess: () => invalidate(),
    onError: () => notify.error("Aktion fehlgeschlagen"),
  });

  const unlinkMutation = useMutation({
    mutationFn: (projectId: string) => api.repos.unlinkProject(repo!.id, projectId),
    onSuccess: () => invalidate(),
    onError: () => notify.error("Entkoppeln fehlgeschlagen"),
  });

  const isDirty = !!repo && (description !== (repo.description ?? "") || rulesMd !== (repo.rules_md ?? ""));

  return (
    <>
      <SlideOverPanel open={open} onClose={onClose} title={repo?.full_name ?? "Repo"} desktopWidth="480px">
        {!repo ? (
          <div className="flex items-center gap-2 p-5" style={{ color: C.textMuted }}>
            <Loader2 size={13} className="animate-spin" />
            <span className="text-xs">Lade Repo...</span>
          </div>
        ) : (
          <div className="p-5 flex flex-col gap-5">
            {/* Metadata */}
            <div>
              <div className="flex items-center gap-2 flex-wrap mb-2">
                <a
                  href={repo.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="inline-flex items-center gap-1 text-xs cursor-pointer"
                  style={{ color: C.accent }}
                >
                  <ExternalLink size={11} />
                  Auf GitHub öffnen
                </a>
                <button
                  onClick={() => syncMutation.mutate()}
                  disabled={syncMutation.isPending}
                  className="inline-flex items-center gap-1 text-xs px-2 py-1 rounded-md cursor-pointer transition-all disabled:opacity-50 ml-auto"
                  style={{ background: C.borderSubtle, border: `1px solid ${C.border}`, color: C.textMuted }}
                >
                  {syncMutation.isPending
                    ? <Loader2 size={11} className="animate-spin" />
                    : <RefreshCw size={11} />}
                  Sync
                </button>
              </div>
              <div className="grid grid-cols-2 gap-x-3 gap-y-1.5 text-xs" style={{ color: C.textMuted }}>
                <span>Sichtbarkeit</span>
                <span style={{ color: C.textSecondary }}>{repo.visibility}</span>
                <span>Branch</span>
                <span className="font-mono" style={{ color: C.textSecondary }}>{repo.default_branch}</span>
                <span>Quelle</span>
                <span style={{ color: C.textSecondary }}>{repo.source === "imported" ? "Importiert" : "MC"}</span>
                <span>Erstellt</span>
                <span style={{ color: C.textSecondary }}>{timeAgo(repo.created_at)}</span>
                <span>Zuletzt synced</span>
                <span style={{ color: C.textSecondary }}>
                  {repo.last_synced_at ? timeAgo(repo.last_synced_at) : "nie"}
                </span>
              </div>
            </div>

            {/* Description */}
            <div className="flex flex-col gap-1">
              <label htmlFor="repo-description" className="text-xs" style={{ color: C.textMuted }}>
                Beschreibung
              </label>
              <textarea
                id="repo-description"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                rows={2}
                className="text-sm px-3 py-2 rounded-lg outline-none resize-none"
                style={{ background: C.border, border: `1px solid ${C.borderSubtle}`, color: C.textPrimary }}
              />
            </div>

            {/* Rules editor */}
            <div className="flex flex-col gap-1">
              <label htmlFor="repo-rules" className="text-xs font-medium" style={{ color: C.textSecondary }}>
                Arbeitsregeln (Markdown)
              </label>
              <p className="text-xs mb-1" style={{ color: C.textDim }}>
                Diese Regeln werden jedem Agenten-Dispatch in diesem Repo mitgegeben.
              </p>
              <textarea
                id="repo-rules"
                value={rulesMd}
                onChange={(e) => setRulesMd(e.target.value)}
                rows={12}
                placeholder={"# Arbeitsregeln\n\n- Branch-Konvention...\n- Commit-Style...\n- Review-Vorgaben..."}
                className="text-xs font-mono px-3 py-2 rounded-lg outline-none resize-y leading-relaxed"
                style={{ background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textPrimary }}
              />
              <div className="flex items-center gap-2 mt-1">
                <button
                  onClick={() => saveMutation.mutate()}
                  disabled={!isDirty || saveMutation.isPending}
                  className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed transition-all"
                  style={{ background: C.accentSubtle, border: `1px solid ${C.borderAccent}`, color: C.accent }}
                >
                  {saveMutation.isPending && <Loader2 size={11} className="animate-spin" />}
                  Speichern
                </button>
                {savedMsg && (
                  <span className="text-xs" style={{ color: C.online }}>Gespeichert</span>
                )}
              </div>
            </div>

            {/* Linked projects */}
            <div className="flex flex-col gap-2">
              <div className="flex items-center justify-between">
                <span className="text-xs font-medium" style={{ color: C.textSecondary }}>
                  Verknüpfte Projekte
                </span>
                <button
                  onClick={() => setPickerOpen((v) => !v)}
                  className="inline-flex items-center gap-1 text-xs cursor-pointer"
                  style={{ color: C.accent }}
                >
                  <Link2 size={11} />
                  Verknüpfen
                </button>
              </div>
              {repo.linked_projects.length === 0 && !pickerOpen && (
                <span className="text-xs" style={{ color: C.textDim }}>Keine Projekte verknüpft</span>
              )}
              <div className="flex flex-col gap-1.5">
                {repo.linked_projects.map((p) => (
                  <div
                    key={p.id}
                    className="flex items-center justify-between px-3 py-1.5 rounded-lg"
                    style={{ background: C.borderSubtle, border: `1px solid ${C.borderSubtle}` }}
                  >
                    <div className="min-w-0">
                      <span className="text-xs truncate" style={{ color: C.textPrimary }}>{p.name}</span>
                      <span className="text-[10px] ml-2" style={{ color: C.textMuted }}>{p.status}</span>
                    </div>
                    <button
                      onClick={() => unlinkMutation.mutate(p.id)}
                      disabled={unlinkMutation.isPending}
                      title="Entkoppeln"
                      aria-label={`${p.name} entkoppeln`}
                      className="shrink-0 cursor-pointer disabled:opacity-50"
                      style={{ color: C.textMuted }}
                    >
                      <Unlink size={12} />
                    </button>
                  </div>
                ))}
              </div>
              {pickerOpen && <LinkProjectPicker repo={repo} onClose={() => setPickerOpen(false)} />}
            </div>

            {/* Danger zone */}
            <div
              className="flex items-center gap-2 pt-3"
              style={{ borderTop: `1px solid ${C.borderSubtle}` }}
            >
              <button
                onClick={() => archiveMutation.mutate()}
                disabled={archiveMutation.isPending}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer disabled:opacity-50 transition-all"
                style={{ background: C.borderSubtle, border: `1px solid ${C.border}`, color: C.textMuted }}
              >
                {archiveMutation.isPending
                  ? <Loader2 size={11} className="animate-spin" />
                  : repo.is_active ? <Archive size={11} /> : <ArchiveRestore size={11} />}
                {repo.is_active ? "Archivieren" : "Reaktivieren"}
              </button>
              <button
                onClick={() => setDeleteOpen(true)}
                className="flex items-center gap-1.5 text-xs px-3 py-1.5 rounded-lg cursor-pointer transition-all ml-auto"
                style={{ background: `${C.error}14`, border: `1px solid ${C.error}33`, color: STATUS_TEXT.error }}
              >
                <Trash2 size={11} />
                Löschen
              </button>
            </div>
          </div>
        )}
      </SlideOverPanel>

      {repo && (
        <DeleteRepoDialog
          repo={repo}
          open={deleteOpen}
          onClose={() => setDeleteOpen(false)}
          onDeleted={() => { setDeleteOpen(false); onClose(); }}
        />
      )}
    </>
  );
}
