"use client";

import { useEffect, useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  ArchiveRestore,
  ArrowLeft,
  Download,
  ExternalLink,
  Film,
  Loader2,
  Pencil,
  RefreshCw,
  RotateCcw,
  Send,
  Square,
  Trash2,
} from "lucide-react";
import { api, getToken } from "@/lib/api";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
import { Pill } from "@/components/shared/Pill";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { FilePreview } from "@/components/task/FilePreview";
import { benchApi } from "@/verticals/bench_studio/api";
import { BENCH_STATUS_COLOR, ENTRY_STATUS_COLOR } from "./ChallengesTab";
import { DraftDialog } from "./DraftDialog";
import type { BenchChallenge, BenchEntry } from "./types";

function sharedUrl(absPath: string): string {
  return api.files.contentUrl("shared-deliverables", benchApi.sharedSubpath(absPath));
}

async function downloadFile(absPath: string, filename: string) {
  const res = await fetch(sharedUrl(absPath), {
    headers: { Authorization: `Bearer ${getToken()}` },
  });
  if (!res.ok) {
    notify.error("Download fehlgeschlagen");
    return;
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  a.click();
  URL.revokeObjectURL(url);
}

/** Opens the entry's rendered page in a new tab, with a short-lived
 *  view-token instead of the operator's session JWT in the URL (that URL is
 *  meant to be copied/shared/opened on a phone — review finding).
 *
 *  The tab is opened SYNCHRONOUSLY, inside the click handler, before the
 *  `await` — Safari/iOS only allows window.open() to bypass the popup
 *  blocker within the same tick as the user gesture that triggered it. Mint
 *  the token afterwards and redirect the already-open blank tab via
 *  location.href. If the tab couldn't be opened at all (global popup
 *  blocker / browser setting), fall back to a same-tab navigation so the
 *  mobile "Öffnen" flow still works. */
async function openEntryView(challengeId: string, entryId: string) {
  const tab = window.open("", "_blank", "noopener");
  try {
    const { token } = await benchApi.entries.viewToken(challengeId, entryId);
    const url = benchApi.entryViewUrl(challengeId, entryId, token);
    if (tab) {
      tab.location.href = url;
    } else {
      window.location.href = url;
    }
  } catch {
    tab?.close();
    notify.error("Öffnen nicht möglich");
  }
}

/** request() throws `Error("API <status>: <raw body>")` — pull the backend's
 *  `detail` string back out (e.g. the 429 cooldown message with seconds
 *  remaining) so the toast shows the real reason instead of a generic one. */
function apiErrorDetail(err: unknown, fallback: string): string {
  if (err instanceof Error) {
    const match = err.message.match(/^API \d+: ([\s\S]*)$/);
    if (match) {
      try {
        const body = JSON.parse(match[1]);
        if (typeof body?.detail === "string" && body.detail) return body.detail;
      } catch {
        // body wasn't JSON — fall through to fallback
      }
    }
  }
  return fallback;
}

/** Mirrors the backend guard in routers.rerender_entry: needs a recorded
 *  artifact and a settled per-entry status. */
function canRerenderEntry(entry: BenchEntry): boolean {
  return Boolean(entry.artifact_path) &&
    ["generated", "rendered", "failed"].includes(entry.status);
}

function metricsLine(m: BenchEntry["metrics"]): string {
  const parts: string[] = [];
  if (m.duration_ms) parts.push(`${(m.duration_ms / 1000).toFixed(0)} s`);
  if (m.tok_per_s) parts.push(`${m.tok_per_s.toFixed(0)} tok/s`);
  if (m.tokens_out) parts.push(`${m.tokens_out} tok`);
  return parts.join(" · ");
}

export function ChallengeDetail({
  challengeId,
  onBack,
}: {
  challengeId: string;
  onBack: () => void;
}) {
  const qc = useQueryClient();
  const [draftOpen, setDraftOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  // The entry whose per-entry rerender we last kicked off — drives the
  // in-button spinner while the background render+compose run is in flight
  // (tracked via challenge.status polling, since the POST itself returns
  // immediately once the task is scheduled).
  const [rerenderEntryId, setRerenderEntryId] = useState<string | null>(null);

  const { data: challenge } = useQuery({
    queryKey: ["bench-challenge", challengeId],
    queryFn: () => benchApi.challenges.get(challengeId),
    refetchInterval: 5000, // polling — no generic SSE hook for bench
  });

  // Once the run this entry kicked off settles (review/failed/etc.), release
  // the spinner — isRunning below already re-enables the other buttons.
  useEffect(() => {
    if (
      rerenderEntryId &&
      challenge &&
      !["rendering", "composing"].includes(challenge.status)
    ) {
      setRerenderEntryId(null);
    }
  }, [challenge?.status, rerenderEntryId, challenge]);

  function invalidate() {
    qc.invalidateQueries({ queryKey: ["bench-challenge", challengeId] });
    qc.invalidateQueries({ queryKey: ["bench-challenges"] });
  }

  const stopMutation = useMutation({
    mutationFn: () => benchApi.challenges.stop(challengeId),
    onSuccess: () => {
      notify.success("Challenge gestoppt");
      invalidate();
    },
    onError: () => notify.error("Stop nicht möglich"),
  });

  const archiveMutation = useMutation({
    mutationFn: (archived: boolean) =>
      archived
        ? benchApi.challenges.unarchive(challengeId)
        : benchApi.challenges.archive(challengeId),
    onSuccess: (ch) => {
      notify.success(ch.archived_at ? "Challenge archiviert" : "Archivierung aufgehoben");
      invalidate();
    },
    onError: () => notify.error("Archivieren nicht möglich"),
  });

  const deleteMutation = useMutation({
    mutationFn: () => benchApi.challenges.remove(challengeId),
    onSuccess: () => {
      notify.success("Challenge gelöscht");
      qc.invalidateQueries({ queryKey: ["bench-challenges"] });
      onBack();
    },
    onError: () => notify.error("Löschen nicht möglich"),
  });

  const rerenderMutation = useMutation({
    mutationFn: () => benchApi.challenges.rerender(challengeId),
    onSuccess: () => {
      notify.success("Rerender gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenge", challengeId] });
    },
    onError: () => notify.error("Rerender nicht möglich"),
  });

  const recomposeMutation = useMutation({
    mutationFn: () => benchApi.challenges.recompose(challengeId),
    onSuccess: () => {
      notify.success("Video wird neu erstellt");
      invalidate();
    },
    onError: () => notify.error("Recompose nicht möglich"),
  });

  const retryMutation = useMutation({
    mutationFn: (entryId: string) => benchApi.entries.retry(entryId),
    onSuccess: () => {
      notify.success("Retry gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenge", challengeId] });
    },
    onError: () => notify.error("Retry nicht möglich"),
  });

  const entryRerenderMutation = useMutation({
    mutationFn: (entryId: string) => benchApi.entries.rerender(entryId),
    onMutate: (entryId: string) => setRerenderEntryId(entryId),
    onSuccess: () => {
      notify.success("Rerender gestartet");
      qc.invalidateQueries({ queryKey: ["bench-challenge", challengeId] });
    },
    onError: (err) => {
      setRerenderEntryId(null);
      notify.error(apiErrorDetail(err, "Rerender nicht möglich"));
    },
  });

  if (!challenge) return null;
  const canDraft = challenge.status === "review" || challenge.status === "drafted";
  const canRerender = ["review", "drafted", "failed"].includes(challenge.status);
  // Mirrors the backend gates (routers.RUNNING_STATUSES / ARCHIVABLE_STATUSES):
  const isRunning = ["generating", "rendering", "composing"].includes(challenge.status);
  // Grid video specifically: while rendering/composing the DB still points at
  // the previous composed_video_path (recompose overwrites it only on
  // success) — show a spinner instead of that stale video.
  const isComposingVideo = ["rendering", "composing"].includes(challenge.status);
  const canArchive = ["review", "drafted", "published", "failed"].includes(challenge.status);
  // Recompose = branded video rebuild from existing recordings (no re-record).
  // Backend accepts 1 (solo frame) or 2 (side-by-side frame) recorded
  // entries regardless of mode (single-video-branding, 2026-07-13) — the
  // 422 from the backend guard stays the source of truth for edge cases.
  const canRecompose =
    !isRunning && challenge.entries.filter((e) => e.video_path).length >= 1;

  return (
    <div className="flex flex-col gap-5">
      {/* Header */}
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-3 min-w-0">
          <button onClick={onBack} aria-label="Zurück" style={{ color: C.textSecondary }}>
            <ArrowLeft size={18} />
          </button>
          <h2 className="text-lg font-semibold truncate" style={{ color: C.textPrimary }}>
            {challenge.title}
          </h2>
          <Pill color={BENCH_STATUS_COLOR[challenge.status] ?? C.textMuted}>
            {challenge.status}
          </Pill>
          {challenge.archived_at && (
            <Pill color={C.textMuted} variant="outline">
              archiviert
            </Pill>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {isRunning && (
            <button
              onClick={() => stopMutation.mutate()}
              disabled={stopMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm disabled:opacity-40"
              style={{ color: C.error, border: `1px solid ${C.error}55` }}
            >
              <Square size={13} /> Stoppen
            </button>
          )}
          {canArchive && (
            <button
              onClick={() => archiveMutation.mutate(challenge.archived_at !== null)}
              disabled={archiveMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm disabled:opacity-40"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              {challenge.archived_at ? (
                <>
                  <ArchiveRestore size={13} /> Entarchivieren
                </>
              ) : (
                <>
                  <Archive size={13} /> Archivieren
                </>
              )}
            </button>
          )}
          {!isRunning && (
            <button
              onClick={() => setEditOpen(true)}
              aria-label="Challenge bearbeiten"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              <Pencil size={13} /> Bearbeiten
            </button>
          )}
          {canRecompose && (
            <button
              onClick={() => recomposeMutation.mutate()}
              disabled={recomposeMutation.isPending}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm disabled:opacity-40"
              style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
            >
              <Film size={13} /> Video neu erstellen
            </button>
          )}
          {!isRunning && (
            <button
              onClick={() => setDeleteOpen(true)}
              aria-label="Challenge löschen"
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm"
              style={{ color: C.textMuted, border: `1px solid ${C.border}` }}
            >
              <Trash2 size={13} />
            </button>
          )}
          <button
            onClick={() => rerenderMutation.mutate()}
            disabled={!canRerender}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm disabled:opacity-40"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            <RefreshCw size={13} /> Neu rendern
          </button>
          <button
            onClick={() => setDraftOpen(true)}
            disabled={!canDraft}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium disabled:opacity-40"
            style={{ backgroundColor: C.accentSubtle, color: C.accent, border: `1px solid ${C.borderAccent}` }}
          >
            <Send size={13} /> Draft erstellen
          </button>
        </div>
      </div>

      {challenge.error && (
        <div
          className="rounded-lg px-3 py-2 text-sm"
          style={{ color: C.error, border: `1px solid ${C.error}40`, backgroundColor: `${C.error}10` }}
        >
          {challenge.error}
        </div>
      )}

      {/* Composed video (branded solo frame for single/1-survivor, grid for
          side_by_side) — spinner while rendering/composing so the (possibly
          stale) previous video is never shown mid-run. */}
      {(isComposingVideo || challenge.composed_video_path) && (
        <section>
          <h3 className="text-sm font-medium mb-2" style={{ color: C.textSecondary }}>
            {challenge.mode === "side_by_side" ? "Grid-Video" : "Benchmark-Video"}
          </h3>
          <div className="rounded-xl p-3" style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.borderSubtle}` }}>
            {isComposingVideo ? (
              <div
                className="flex flex-col items-center justify-center gap-2 py-10"
                style={{ color: C.textSecondary }}
              >
                <Loader2 size={22} className="animate-spin" style={{ color: C.accent }} />
                <span className="text-sm">
                  {challenge.status === "rendering"
                    ? "Aufnahmen werden gerendert…"
                    : "Video wird zusammengesetzt…"}
                </span>
              </div>
            ) : (
              <FilePreview
                fileUrl={sharedUrl(challenge.composed_video_path!)}
                path={challenge.composed_video_path!}
              />
            )}
          </div>
        </section>
      )}

      {/* Per-model gallery */}
      <section className="grid gap-4 sm:grid-cols-2">
        {challenge.entries.map((entry) => (
          <div
            key={entry.id}
            className="rounded-xl p-3 flex flex-col gap-2"
            style={{ backgroundColor: C.bgSurface, border: `1px solid ${C.borderSubtle}` }}
          >
            <div className="flex items-center justify-between gap-2">
              <span className="text-sm font-medium" style={{ color: C.textPrimary }}>
                {entry.model_label}
              </span>
              <Pill color={ENTRY_STATUS_COLOR[entry.status] ?? C.textMuted}>
                {entry.status}
              </Pill>
            </div>
            {metricsLine(entry.metrics) && (
              <span className="text-xs font-mono" style={{ color: C.textMuted }}>
                {metricsLine(entry.metrics)}
              </span>
            )}
            {entry.video_path ? (
              <FilePreview fileUrl={sharedUrl(entry.video_path)} path={entry.video_path} />
            ) : entry.screenshot_path ? (
              <FilePreview fileUrl={sharedUrl(entry.screenshot_path)} path={entry.screenshot_path} />
            ) : entry.status === "generating" || entry.status === "pending" ? (
              <div
                className="flex items-center justify-center gap-2 py-8 rounded-lg"
                style={{ backgroundColor: C.bgDeep, color: C.textMuted }}
              >
                <Loader2 size={15} className="animate-spin" />
                <span className="text-xs">
                  {entry.status === "generating" ? "Wird generiert…" : "Wartet…"}
                </span>
              </div>
            ) : null}
            {entry.error && (
              <span className="text-xs" style={{ color: C.error }}>{entry.error}</span>
            )}
            <div className="flex items-center gap-2 mt-auto">
              {entry.artifact_path && (
                <>
                  <button
                    onClick={() => openEntryView(challengeId, entry.id)}
                    className="flex items-center gap-1 text-xs"
                    style={{ color: C.accent }}
                  >
                    <ExternalLink size={12} /> Öffnen
                  </button>
                  <button
                    onClick={() =>
                      downloadFile(entry.artifact_path!, `${entry.model_label}-index.html`)
                    }
                    className="flex items-center gap-1 text-xs"
                    style={{ color: C.textSecondary }}
                  >
                    <Download size={12} /> HTML
                  </button>
                </>
              )}
              {entry.status === "failed" && (
                <button
                  onClick={() => retryMutation.mutate(entry.id)}
                  className="flex items-center gap-1 text-xs"
                  style={{ color: C.accent }}
                >
                  <RotateCcw size={12} /> Retry
                </button>
              )}
              {canRerenderEntry(entry) && (() => {
                const isRerenderingThisEntry =
                  (entryRerenderMutation.isPending &&
                    entryRerenderMutation.variables === entry.id) ||
                  (rerenderEntryId === entry.id && isComposingVideo);
                return (
                  <button
                    onClick={() => entryRerenderMutation.mutate(entry.id)}
                    disabled={isRunning || entryRerenderMutation.isPending}
                    aria-label={`${entry.model_label} neu rendern`}
                    className="flex items-center gap-1 text-xs disabled:opacity-40"
                    style={{ color: C.textSecondary }}
                  >
                    {isRerenderingThisEntry ? (
                      <Loader2 size={12} className="animate-spin" style={{ color: C.accent }} />
                    ) : (
                      <RefreshCw size={12} />
                    )}
                    Rerender
                  </button>
                );
              })()}
            </div>
          </div>
        ))}
      </section>

      <DraftDialog challenge={challenge} open={draftOpen} onClose={() => setDraftOpen(false)} />

      {/* Mounted only while open so the form re-seeds from fresh data each time */}
      {editOpen && (
        <EditChallengeDialog
          challenge={challenge}
          open={editOpen}
          onClose={() => setEditOpen(false)}
          onSaved={invalidate}
        />
      )}

      {/* Delete confirm — same pattern as files/DeleteFilesDialog (no window.confirm) */}
      <ResponsiveModal
        open={deleteOpen}
        onClose={() => setDeleteOpen(false)}
        aria-labelledby="delete-challenge-title"
      >
        <div
          className="px-5 pt-4 pb-3 shrink-0"
          style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
        >
          <h2
            id="delete-challenge-title"
            className="text-base font-semibold"
            style={{ color: C.textPrimary }}
          >
            Challenge löschen?
          </h2>
        </div>
        <div className="px-5 py-3">
          <p className="text-sm" style={{ color: C.textSecondary }}>
            „{challenge.title}" wird endgültig gelöscht — inklusive aller Videos und
            Artefakte unter /shared-deliverables. Verknüpfte Fleet-Tasks bleiben
            erhalten (Audit-Trail). Nicht rückgängig machbar.
          </p>
        </div>
        <div
          className="flex items-center justify-end gap-2 px-5 py-3 shrink-0"
          style={{ borderTop: `1px solid ${C.borderSubtle}` }}
        >
          <button
            onClick={() => setDeleteOpen(false)}
            disabled={deleteMutation.isPending}
            className="px-3.5 py-2 rounded-lg text-sm font-medium disabled:opacity-60"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            Abbrechen
          </button>
          <button
            onClick={() => deleteMutation.mutate()}
            disabled={deleteMutation.isPending}
            className="inline-flex items-center gap-2 px-3.5 py-2 rounded-lg text-sm font-semibold disabled:opacity-70"
            style={{ background: C.error, color: C.textPrimary }}
          >
            {deleteMutation.isPending ? (
              <Loader2 size={15} className="animate-spin" />
            ) : (
              <Trash2 size={15} />
            )}
            Löschen
          </button>
        </div>
      </ResponsiveModal>
    </div>
  );
}

/** Edit dialog: challenge title + per-entry model name / chip tag.
 *  Same Leitstand pattern as NewChallengeDialog (ResponsiveModal + dark
 *  inputs). Saves only changed fields; recompose stays a separate,
 *  deliberate action in the header ("Video neu erstellen"). */
function EditChallengeDialog({
  challenge,
  open,
  onClose,
  onSaved,
}: {
  challenge: BenchChallenge;
  open: boolean;
  onClose: () => void;
  onSaved: () => void;
}) {
  const [title, setTitle] = useState(challenge.title);
  const [entryEdits, setEntryEdits] = useState<
    Record<string, { model_label: string; display_tag: string }>
  >(() =>
    Object.fromEntries(
      challenge.entries.map((e) => [
        e.id,
        { model_label: e.model_label, display_tag: e.display_tag ?? "" },
      ])
    )
  );

  const mutation = useMutation({
    mutationFn: async () => {
      if (title.trim() && title.trim() !== challenge.title) {
        await benchApi.challenges.update(challenge.id, { title: title.trim() });
      }
      for (const entry of challenge.entries) {
        const edit = entryEdits[entry.id];
        if (!edit) continue;
        const changed =
          edit.model_label.trim() !== entry.model_label ||
          edit.display_tag.trim() !== (entry.display_tag ?? "");
        if (changed && edit.model_label.trim()) {
          await benchApi.entries.update(entry.id, {
            model_label: edit.model_label.trim(),
            display_tag: edit.display_tag.trim(),
          });
        }
      }
    },
    onSuccess: () => {
      notify.success("Änderungen gespeichert");
      onSaved();
      onClose();
    },
    onError: () => notify.error("Speichern nicht möglich"),
  });

  const inputStyle = {
    backgroundColor: C.bgDeep,
    color: C.textPrimary,
    border: `1px solid ${C.border}`,
  } as const;

  function setEdit(id: string, patch: Partial<{ model_label: string; display_tag: string }>) {
    setEntryEdits((prev) => ({ ...prev, [id]: { ...prev[id], ...patch } }));
  }

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-label="Challenge bearbeiten">
      <div
        className="flex flex-col gap-4 p-5 rounded-xl w-full max-h-[85vh] overflow-y-auto"
        style={{ backgroundColor: C.bgElevated, border: `1px solid ${C.border}` }}
      >
        <h3 className="text-base font-semibold" style={{ color: C.textPrimary }}>
          Challenge bearbeiten
        </h3>

        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          placeholder="Titel"
          aria-label="Titel"
          className="rounded-lg p-2.5 text-sm outline-none"
          style={inputStyle}
        />

        <div className="flex flex-col gap-2">
          <span className="text-sm font-medium" style={{ color: C.textSecondary }}>
            Modelle
          </span>
          {challenge.entries.map((entry, i) => (
            <div key={entry.id} className="flex gap-2 items-center">
              <input
                value={entryEdits[entry.id]?.model_label ?? ""}
                onChange={(e) => setEdit(entry.id, { model_label: e.target.value })}
                placeholder="Modell-Name"
                aria-label={`Modell-Name ${i + 1}`}
                className="rounded-lg p-2 text-sm outline-none flex-1"
                style={inputStyle}
              />
              <input
                value={entryEdits[entry.id]?.display_tag ?? ""}
                onChange={(e) => setEdit(entry.id, { display_tag: e.target.value })}
                placeholder="Tag (leer = Harness-Default)"
                aria-label={`Tag ${i + 1}`}
                className="rounded-lg p-2 text-sm outline-none flex-1"
                style={inputStyle}
              />
            </div>
          ))}
          <span className="text-xs" style={{ color: C.textMuted }}>
            Danach „Video neu erstellen" klicken, um das gebrandete Video mit den
            neuen Namen zu bauen (nutzt die vorhandenen Aufnahmen).
          </span>
        </div>

        <div className="flex justify-end gap-2">
          <button
            onClick={onClose}
            disabled={mutation.isPending}
            className="px-3 py-1.5 rounded-lg text-sm disabled:opacity-60"
            style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
          >
            Abbrechen
          </button>
          <button
            onClick={() => mutation.mutate()}
            disabled={mutation.isPending || !title.trim()}
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
