"use client";

/**
 * ChatChangesList — the Changes panel's default tab "This chat": every file
 * this chat session changed, newest first, from the per-edit diffs already in
 * the transcript (`GET /agents/{id}/chat/changes`, backend `chat_changes.py`).
 *
 * Works for every harness and without git — a chat in a plain folder still
 * shows its work, and nothing about another workspace is ever guessed.
 *
 * Phone first (393 px): one row per file, the file name leads (mono — it is a
 * machine value, K7), its folder and how often/when it changed sit quietly
 * below; +/− on the right in tabular numbers. Tap → the file's combined diff
 * (all its edits, oldest first) in the same `ToolDiffView` the tool cards use,
 * with a back row. The detail view carries "Revert" (P3): one tap opens the
 * confirmation sheet (`RevertSheet`); a file MC already put back says so
 * instead (`reverted`).
 */
import { useState } from "react";
import { useLocale, useTranslations } from "next-intl";
import { Check, ChevronLeft, ChevronRight, FilePen, RotateCcw } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import type { ChatChangeFile, ChatChanges } from "@/lib/chatTypes";
import { ToolDiffView } from "./ToolDiffView";
import { RevertSheet, type RevertTarget } from "./ChangesActions";

function splitPath(path: string): { dir: string; base: string } {
  const i = path.lastIndexOf("/");
  return i < 0 ? { dir: "", base: path } : { dir: path.slice(0, i), base: path.slice(i + 1) };
}

/** +a −d, each only when non-zero (K3). */
function Counts({ file }: { file: Pick<ChatChangeFile, "additions" | "deletions"> }) {
  if (file.additions === 0 && file.deletions === 0) return null;
  return (
    <span className="shrink-0 flex items-center gap-2 font-mono text-xs tabular-nums">
      {file.additions > 0 && <span style={{ color: STATUS_TEXT.online }}>+{file.additions}</span>}
      {file.deletions > 0 && <span style={{ color: STATUS_TEXT.error }}>−{file.deletions}</span>}
    </span>
  );
}

/** "src/app · 2 edits · 3 minutes ago" — the row's quiet second line. */
function useMeta(file: ChatChangeFile, withDir: boolean): string {
  const t = useTranslations("sessions.diff");
  const tCard = useTranslations("sessions.toolCard");
  const locale = useLocale();
  const { dir } = splitPath(file.path);
  const parts: string[] = [];
  if (withDir && dir) parts.push(dir);
  if (file.created) parts.push(tCard("diffCreate"));
  parts.push(t("chatEdits", { count: file.edits }));
  if (file.lastAt) parts.push(timeAgo(file.lastAt, locale));
  return parts.join(" · ");
}

function FileRow({ file, onOpen }: { file: ChatChangeFile; onOpen: () => void }) {
  const { base } = splitPath(file.path);
  const meta = useMeta(file, true);
  return (
    <li>
      <button
        type="button"
        onClick={onOpen}
        data-testid="chat-change-row"
        title={file.path}
        className="w-full flex items-center gap-3 px-3 py-2 min-h-11 text-left cursor-pointer bg-transparent border-0 transition-colors hover:bg-[var(--color-bg-hover)]"
      >
        <span className="min-w-0 flex-1">
          <span className="block font-mono text-sm break-all" style={{ color: C.textPrimary }}>
            {base}
          </span>
          <span className="block text-xs break-words" style={{ color: C.textMuted }}>
            {meta}
          </span>
        </span>
        <Counts file={file} />
        <ChevronRight size={14} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />
      </button>
    </li>
  );
}

function FileDetail({
  file,
  onBack,
  onRevert,
}: {
  file: ChatChangeFile;
  onBack: () => void;
  onRevert?: () => void;
}) {
  const t = useTranslations("sessions.diff");
  const meta = useMeta(file, false);
  return (
    <div data-testid="chat-change-detail" className="flex flex-col gap-2 pb-3">
      <button
        type="button"
        onClick={onBack}
        className="self-start flex items-center gap-1 px-3 min-h-11 text-sm cursor-pointer bg-transparent border-0"
        style={{ color: C.textSecondary }}
      >
        <ChevronLeft size={14} aria-hidden="true" />
        {t("chatBack")}
      </button>
      <div className="flex items-start gap-3 px-3">
        <span className="min-w-0 flex-1">
          <span className="block font-mono text-sm break-all" style={{ color: C.textPrimary }}>
            {file.path}
          </span>
          <span className="block text-xs" style={{ color: C.textMuted }}>
            {meta}
          </span>
        </span>
        <Counts file={file} />
      </div>
      {file.reverted ? (
        <p
          data-testid="chat-change-reverted"
          className="flex items-center gap-2 px-3 text-sm"
          style={{ color: C.textMuted }}
        >
          <Check size={14} aria-hidden="true" />
          {t("act.reverted")}
        </p>
      ) : (
        onRevert && (
          <button
            type="button"
            onClick={onRevert}
            data-testid="chat-change-revert"
            className="self-start flex items-center gap-2 px-3 min-h-11 text-sm cursor-pointer bg-transparent border-0"
            style={{ color: C.textSecondary }}
          >
            <RotateCcw size={14} aria-hidden="true" />
            {t("act.revert")}
          </button>
        )
      )}
      <div className="px-3">
        <ToolDiffView diff={file.diff} multi={false} initialAll />
      </div>
    </div>
  );
}

export function ChatChangesList({ changes, agentId }: { changes: ChatChanges; agentId?: string }) {
  const t = useTranslations("sessions.diff");
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [revert, setRevert] = useState<RevertTarget | null>(null);
  const open = openPath ? changes.files.find((f) => f.path === openPath) : undefined;

  if (open) {
    return (
      <>
        <FileDetail
          file={open}
          onBack={() => setOpenPath(null)}
          onRevert={
            agentId
              ? () => setRevert({ path: open.path, edits: open.edits, created: open.created })
              : undefined
          }
        />
        {agentId && <RevertSheet agentId={agentId} target={revert} onClose={() => setRevert(null)} />}
      </>
    );
  }

  if (changes.files.length === 0) {
    return (
      <div className="flex flex-col items-center justify-center h-full min-h-40 gap-2 px-6 text-center">
        <FilePen size={24} style={{ color: C.textMuted, opacity: 0.4 }} aria-hidden="true" />
        <p className="text-sm" style={{ color: C.textSecondary }}>
          {t("chatEmpty")}
        </p>
        <p className="text-xs max-w-xs" style={{ color: C.textMuted }}>
          {t("chatEmptyHint")}
        </p>
      </div>
    );
  }

  return (
    <div data-region="chat-changes" className="flex flex-col">
      <p
        data-testid="chat-changes-summary"
        className="flex items-center gap-2 px-3 pt-3 pb-1 text-xs"
        style={{ color: C.textMuted }}
      >
        <span>{t("chatFiles", { count: changes.stats.files })}</span>
        <Counts file={changes.stats} />
      </p>
      <ul className="flex flex-col">
        {changes.files.map((file) => (
          <FileRow key={file.path} file={file} onOpen={() => setOpenPath(file.path)} />
        ))}
      </ul>
    </div>
  );
}
