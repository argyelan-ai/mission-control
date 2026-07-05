"use client";

/**
 * CreateLoopDialog — create form for a new loop (ADR-051).
 *
 * Sections: Name/Board, Goal, Backlog source (conditional markdown/project
 * fields), Advanced (collapsed: max rounds, failure gate, human gate, max
 * duration, stop-on-empty).
 */

import { useEffect, useState } from "react";
import { AnimatePresence, motion } from "framer-motion";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ChevronDown, ChevronRight, Loader2, Settings2 } from "lucide-react";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import type { Board, Loop, LoopBacklogSource, LoopCreate } from "@/lib/types";

interface CreateLoopDialogProps {
  open: boolean;
  onClose: () => void;
  onCreated: (loop: Loop) => void;
}

interface FormState {
  name: string;
  boardId: string;
  goal: string;
  backlogSource: LoopBacklogSource;
  backlogMd: string;
  backlogTag: string;
  projectId: string;
  maxRounds: number;
  pauseOnFailedRounds: number;
  humanEveryNRounds: number;
  maxDurationMinutes: string;
  stopOnBacklogEmpty: boolean;
  telegramReports: boolean;
}

function defaultForm(boardId: string): FormState {
  return {
    name: "",
    boardId,
    goal: "",
    backlogSource: "markdown",
    backlogMd: "",
    backlogTag: "",
    projectId: "",
    maxRounds: 10,
    pauseOnFailedRounds: 2,
    humanEveryNRounds: 0,
    maxDurationMinutes: "",
    stopOnBacklogEmpty: true,
    telegramReports: true,
  };
}

export function CreateLoopDialog({ open, onClose, onCreated }: CreateLoopDialogProps) {
  const qc = useQueryClient();
  const activeBoardId = useAppStore((s) => s.activeBoardId);
  const [form, setForm] = useState<FormState>(() => defaultForm(activeBoardId ?? ""));
  const [advancedExpanded, setAdvancedExpanded] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (open) {
      setForm(defaultForm(activeBoardId ?? ""));
      setAdvancedExpanded(false);
      setError(null);
    }
  }, [open, activeBoardId]);

  const { data: boards = [] } = useQuery({
    queryKey: ["boards"],
    queryFn: () => api.boards.list(),
    enabled: open,
  });

  const { data: projects = [] } = useQuery({
    queryKey: ["projects", form.boardId],
    queryFn: () => api.projects.list(form.boardId),
    enabled: open && form.backlogSource === "project" && !!form.boardId,
  });

  const createMutation = useMutation({
    mutationFn: (payload: LoopCreate) => api.loops.create(payload),
    onSuccess: (loop) => {
      qc.invalidateQueries({ queryKey: ["loops"] });
      onCreated(loop);
      onClose();
    },
    onError: (e) => setError(e instanceof Error ? e.message : "Failed to create loop"),
  });

  const handleSubmit = () => {
    setError(null);
    if (!form.name.trim()) return setError("Name is required.");
    if (!form.boardId) return setError("Board is required.");
    if (!form.goal.trim()) return setError("Goal is required.");
    if (form.backlogSource === "markdown" && !form.backlogMd.trim()) {
      return setError("Markdown backlog is required when backlog source is a Markdown list.");
    }
    if (form.backlogSource === "project" && !form.projectId) {
      return setError("Choose a project to pull the backlog from.");
    }
    if (form.backlogSource === "tag" && !form.backlogTag.trim()) {
      return setError("Enter a tag to pull the backlog from.");
    }

    const payload: LoopCreate = {
      board_id: form.boardId,
      name: form.name.trim(),
      goal: form.goal.trim(),
      backlog_source: form.backlogSource,
      ...(form.backlogSource === "markdown" ? { backlog_md: form.backlogMd.trim() } : {}),
      ...(form.backlogSource === "project" ? { project_id: form.projectId } : {}),
      ...(form.backlogSource === "tag" ? { backlog_tag: form.backlogTag.trim() } : {}),
      max_rounds: form.maxRounds,
      pause_on_failed_rounds: form.pauseOnFailedRounds,
      human_every_n_rounds: form.humanEveryNRounds,
      ...(form.maxDurationMinutes.trim() ? { max_duration_minutes: Number(form.maxDurationMinutes) } : {}),
      stop_on_backlog_empty: form.stopOnBacklogEmpty,
      telegram_reports: form.telegramReports,
    };
    createMutation.mutate(payload);
  };

  const inputCls = "w-full rounded-md px-3 py-2 text-sm outline-none";
  const inputStyle = { background: C.bgDeep, border: `1px solid ${C.border}`, color: C.textPrimary };

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="create-loop-title">
      <div className="px-5 pt-4 pb-3 shrink-0" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
        <h2 id="create-loop-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          New loop
        </h2>
        <p className="text-xs mt-0.5" style={{ color: C.textMuted }}>
          Define a goal and a backlog — the loop runs rounds until it's done or needs you.
        </p>
      </div>

      <div className="flex flex-col gap-4 px-5 py-4 overflow-y-auto">
        <Label text="Name *">
          <input
            type="text"
            value={form.name}
            onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
            placeholder="Nightly polish loop"
            className={inputCls}
            style={inputStyle}
          />
        </Label>

        <Label text="Board *">
          <select
            value={form.boardId}
            onChange={(e) => setForm((p) => ({ ...p, boardId: e.target.value, projectId: "" }))}
            className={inputCls}
            style={inputStyle}
          >
            <option value="">— choose a board —</option>
            {boards.map((b: Board) => (
              <option key={b.id} value={b.id}>
                {b.name}
              </option>
            ))}
          </select>
        </Label>

        <Label text="Goal *">
          <textarea
            value={form.goal}
            onChange={(e) => setForm((p) => ({ ...p, goal: e.target.value }))}
            rows={3}
            placeholder="Drive down open bugs on the report engine until the backlog is empty."
            className="w-full resize-none rounded-md px-3 py-2 text-sm outline-none"
            style={inputStyle}
          />
        </Label>

        <Label text="Backlog source *">
          <select
            value={form.backlogSource}
            onChange={(e) => setForm((p) => ({ ...p, backlogSource: e.target.value as LoopBacklogSource }))}
            className={inputCls}
            style={inputStyle}
          >
            <option value="markdown">Markdown list</option>
            <option value="project">Project tasks</option>
            <option value="tag">Tag</option>
            <option value="open_ended">Open-ended</option>
          </select>
        </Label>

        {form.backlogSource === "markdown" && (
          <Label text="Backlog (Markdown) *">
            <textarea
              value={form.backlogMd}
              onChange={(e) => setForm((p) => ({ ...p, backlogMd: e.target.value }))}
              rows={5}
              placeholder={"- Fix flaky test in checkout flow\n- Tighten empty states on /reports\n- Add retry to webhook delivery"}
              className="w-full resize-none rounded-md px-3 py-2 text-sm font-mono outline-none"
              style={inputStyle}
            />
          </Label>
        )}

        {form.backlogSource === "project" && (
          <Label text="Project *">
            <select
              value={form.projectId}
              onChange={(e) => setForm((p) => ({ ...p, projectId: e.target.value }))}
              className={inputCls}
              style={inputStyle}
              disabled={!form.boardId}
            >
              <option value="">— choose a project —</option>
              {projects.map((proj) => (
                <option key={proj.id} value={proj.id}>
                  {proj.name}
                </option>
              ))}
            </select>
          </Label>
        )}

        {form.backlogSource === "tag" && (
          <Label text="Tag *">
            <input
              type="text"
              value={form.backlogTag}
              onChange={(e) => setForm((p) => ({ ...p, backlogTag: e.target.value }))}
              placeholder="polish"
              className={inputCls}
              style={inputStyle}
            />
          </Label>
        )}

        {/* Advanced */}
        <section
          className="flex flex-col gap-2 rounded-lg px-3 py-2.5"
          style={{ border: `1px solid ${C.borderSubtle}`, background: C.bgSurface }}
        >
          <button
            type="button"
            onClick={() => setAdvancedExpanded((v) => !v)}
            className="flex items-center gap-1.5 text-left text-[11px] font-semibold uppercase tracking-wider cursor-pointer"
            style={{ color: C.textSecondary }}
          >
            {advancedExpanded ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
            <Settings2 size={12} />
            Advanced
          </button>
          <AnimatePresence initial={false}>
            {advancedExpanded && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.15 }}
                className="overflow-hidden"
              >
                <div className="flex flex-col gap-3 pt-2">
                  <div className="grid grid-cols-2 gap-3">
                    <Label text="Max rounds">
                      <input
                        type="number"
                        min={1}
                        value={form.maxRounds}
                        onChange={(e) => setForm((p) => ({ ...p, maxRounds: Math.max(1, Number(e.target.value)) }))}
                        className={inputCls}
                        style={inputStyle}
                      />
                    </Label>
                    <Label text="Pause after failed rounds">
                      <input
                        type="number"
                        min={1}
                        value={form.pauseOnFailedRounds}
                        onChange={(e) =>
                          setForm((p) => ({ ...p, pauseOnFailedRounds: Math.max(1, Number(e.target.value)) }))
                        }
                        className={inputCls}
                        style={inputStyle}
                      />
                    </Label>
                    <Label text="Human gate every N rounds (0 = never)">
                      <input
                        type="number"
                        min={0}
                        value={form.humanEveryNRounds}
                        onChange={(e) =>
                          setForm((p) => ({ ...p, humanEveryNRounds: Math.max(0, Number(e.target.value)) }))
                        }
                        className={inputCls}
                        style={inputStyle}
                      />
                    </Label>
                    <Label text="Max duration (minutes, optional)">
                      <input
                        type="number"
                        min={1}
                        value={form.maxDurationMinutes}
                        onChange={(e) => setForm((p) => ({ ...p, maxDurationMinutes: e.target.value }))}
                        placeholder="No limit"
                        className={inputCls}
                        style={inputStyle}
                      />
                    </Label>
                  </div>
                  <label className="flex cursor-pointer items-center gap-2 text-sm" style={{ color: C.textSecondary }}>
                    <input
                      type="checkbox"
                      checked={form.stopOnBacklogEmpty}
                      onChange={(e) => setForm((p) => ({ ...p, stopOnBacklogEmpty: e.target.checked }))}
                      className="h-3.5 w-3.5 cursor-pointer"
                      style={{ accentColor: C.accent }}
                    />
                    Stop when the backlog is empty
                  </label>
                  <label className="flex cursor-pointer items-center gap-2 text-sm" style={{ color: C.textSecondary }}>
                    <input
                      type="checkbox"
                      checked={form.telegramReports}
                      onChange={(e) => setForm((p) => ({ ...p, telegramReports: e.target.checked }))}
                      className="h-3.5 w-3.5 cursor-pointer"
                      style={{ accentColor: C.accent }}
                    />
                    Send a Telegram report after every round
                  </label>
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </section>

        {error && (
          <div
            className="flex items-start gap-2 rounded-md border px-3 py-2 text-xs"
            style={{ borderColor: `${C.error}66`, background: `${C.error}14`, color: C.error }}
          >
            <AlertTriangle size={14} className="mt-0.5 shrink-0" />
            <span>{error}</span>
          </div>
        )}
      </div>

      <div
        className="flex items-center justify-end gap-2 px-5 py-3 shrink-0"
        style={{ borderTop: `1px solid ${C.borderSubtle}`, paddingBottom: "calc(env(safe-area-inset-bottom) + 0.75rem)" }}
      >
        <button
          type="button"
          onClick={onClose}
          disabled={createMutation.isPending}
          className="rounded-md px-3 py-1.5 text-sm cursor-pointer transition"
          style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
        >
          Cancel
        </button>
        <button
          type="button"
          onClick={handleSubmit}
          disabled={createMutation.isPending}
          className="flex items-center gap-1.5 rounded-md px-3.5 py-1.5 text-sm font-medium cursor-pointer transition disabled:opacity-60"
          style={{ background: C.accent, color: C.textPrimary }}
        >
          {createMutation.isPending && <Loader2 size={14} className="animate-spin" />}
          Create loop
        </button>
      </div>
    </ResponsiveModal>
  );
}

function Label({ text, children }: { text: string; children: React.ReactNode }) {
  return (
    <label className="flex flex-col gap-1">
      <span className="text-[10px] uppercase tracking-wide" style={{ color: C.textDim }}>
        {text}
      </span>
      {children}
    </label>
  );
}
