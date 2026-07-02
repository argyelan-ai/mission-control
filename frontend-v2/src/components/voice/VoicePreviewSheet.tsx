"use client";

/**
 * VoicePreviewSheet — inline preview overlay that opens *over* the
 * VoiceDrawer when the operator clicks a card. Stays on the same page so the
 * LiveKit room + WebSocket survive.
 */

import { useEffect, useMemo } from "react";
import { createPortal } from "react-dom";
import { motion, AnimatePresence } from "framer-motion";
import { useQuery } from "@tanstack/react-query";
import { ExternalLink, X, FileText, FileIcon, CircleDot, Loader2, AlertCircle, PhoneOff } from "lucide-react";
import ReactMarkdown from "react-markdown";
import { api } from "@/lib/api";
import type { DisplayCard } from "./cards/types";
import { useVoiceContext } from "./VoiceWidget";
import { C, LANE } from "@/lib/colors";

const STATUS_COLORS: Record<string, string> = {
  inbox: LANE.inbox,
  in_progress: LANE.in_progress,
  blocked: LANE.blocked,
  review: LANE.review,
  done: LANE.done,
  failed: LANE.failed,
};

export function VoicePreviewSheet({
  card,
  onClose,
}: {
  card: DisplayCard | null;
  onClose: () => void;
}) {
  useEffect(() => {
    if (!card) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [card, onClose]);

  if (typeof document === "undefined") return null;

  return createPortal(
    <AnimatePresence>
      {card && (
        <>
          {/* Backdrop — legitimate overlay, blur kept */}
          <motion.div
            className="fixed inset-0 z-[58]"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.15 }}
            onClick={onClose}
            style={{ background: "rgba(0,0,0,0.55)", backdropFilter: "blur(6px)" }}
          />

          {/* Sheet */}
          <motion.div
            className="fixed z-[59] rounded-2xl overflow-hidden flex flex-col left-1/2 -translate-x-1/2 top-1/2 -translate-y-1/2 w-[calc(100vw-1.5rem)] md:w-[640px]"
            role="dialog"
            aria-modal="true"
            aria-label="Vorschau"
            initial={{ opacity: 0, scale: 0.96, y: 8 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.97, y: 4 }}
            transition={{ type: "spring", stiffness: 340, damping: 30 }}
            style={{
              maxHeight: "min(80vh, 720px)",
              background: C.bgBase,
              border: `1px solid ${C.borderActive}`,
              boxShadow: "0 4px 24px rgba(0,0,0,0.5), 0 1px 2px rgba(0,0,0,0.3)",
            }}
          >
            {/* Top rim */}
            <div
              className="absolute inset-x-0 top-0 h-px pointer-events-none"
              style={{
                background:
                  "linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.10) 50%, transparent 100%)",
              }}
            />

            <SheetContent card={card} onClose={onClose} />
          </motion.div>
        </>
      )}
    </AnimatePresence>,
    document.body,
  );
}

function SheetContent({ card, onClose }: { card: DisplayCard; onClose: () => void }) {
  if (card.kind === "task") return <TaskSheet card={card} onClose={onClose} />;
  // UrlCard bietet kein onPreview an — dieser Zweig ist zur Laufzeit
  // unerreichbar, macht aber das Narrowing auf memory|file explizit.
  if (card.kind === "url") return null;
  return <NoteSheet card={card} onClose={onClose} />;
}

function HangupChip() {
  const { active, endSession } = useVoiceContext();
  if (!active) return null;
  return (
    <button
      type="button"
      onClick={() => endSession()}
      className="flex items-center gap-1 px-2 py-1 rounded text-[10px] hover:opacity-90 cursor-pointer transition-opacity"
      style={{
        background: `linear-gradient(135deg, ${C.error}CC 0%, ${C.error} 100%)`,
        color: C.textPrimary,
        boxShadow: `0 2px 8px ${C.error}40`,
      }}
      title="Anruf beenden (Tokens sparen) — diese Vorschau bleibt offen"
    >
      <PhoneOff size={11} /> Anruf beenden
    </button>
  );
}

function NoteSheet({
  card,
  onClose,
}: {
  card: Extract<DisplayCard, { kind: "memory" | "file" }>;
  onClose: () => void;
}) {
  const path = card.data.vault_path ?? "";
  const enabled = path.length > 0;
  const { data, isLoading, error } = useQuery({
    queryKey: ["vault-note", path],
    queryFn: () => api.vault.get(path),
    enabled,
    staleTime: 30_000,
  });

  const title = useMemo(
    () => card.title || card.data.title || path.split("/").pop() || "Notiz",
    [card, path],
  );

  const openInVaultHref = `/memory?note=${encodeURIComponent(path)}`;
  const Icon = card.kind === "file" ? FileIcon : FileText;

  return (
    <>
      {/* Header */}
      <div className="relative flex items-center justify-between px-5 py-3 border-b border-white/[0.06]">
        <div className="flex items-center gap-2.5 min-w-0">
          <Icon size={14} style={{ color: C.accent }} className="shrink-0" />
          <div className="min-w-0">
            <div
              className="text-[12px] font-medium leading-snug truncate"
              style={{ color: "var(--color-text-primary)" }}
            >
              {title}
            </div>
            <div
              className="text-[10px] mt-0.5 truncate"
              style={{ color: "var(--color-text-muted)" }}
            >
              {[card.data.type, card.data.agent, path].filter(Boolean).join(" · ")}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <HangupChip />
          {path && (
            <a
              href={openInVaultHref}
              target="_blank"
              rel="noopener noreferrer"
              className="flex items-center gap-1 px-2 py-1 rounded text-[10px] hover:bg-white/5 cursor-pointer"
              style={{ color: "var(--color-text-secondary)" }}
              title="Im Vault öffnen (neuer Tab)"
            >
              <ExternalLink size={11} /> Vault
            </a>
          )}
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded hover:bg-white/5 cursor-pointer"
            aria-label="Schliessen"
          >
            <X size={14} style={{ color: "var(--color-text-secondary)" }} />
          </button>
        </div>
      </div>

      {/* Body */}
      <div className="relative flex-1 overflow-y-auto px-6 py-5">
        {!enabled && (
          <div className="text-xs" style={{ color: "var(--color-text-muted)" }}>
            Keine Vault-Pfad-Information.
          </div>
        )}
        {enabled && isLoading && (
          <div className="flex items-center gap-2 text-xs" style={{ color: "var(--color-text-muted)" }}>
            <Loader2 size={13} className="animate-spin" /> Lade Inhalt …
          </div>
        )}
        {enabled && error && (
          <div className="flex items-start gap-2 text-xs" style={{ color: C.error }}>
            <AlertCircle size={13} className="mt-0.5" />
            <div>
              <div className="font-medium">Konnte Notiz nicht laden</div>
              <div className="opacity-80 mt-0.5">{(error as Error).message}</div>
            </div>
          </div>
        )}
        {data && (
          <article
            className="prose prose-invert prose-sm max-w-none"
            style={{ color: "var(--color-text-body)" }}
          >
            <ReactMarkdown>{data.content || "_Leer._"}</ReactMarkdown>
          </article>
        )}
      </div>
    </>
  );
}

function TaskSheet({
  card,
  onClose,
}: {
  card: Extract<DisplayCard, { kind: "task" }>;
  onClose: () => void;
}) {
  const title = card.title || card.data.title || "Task";
  const statusColor = STATUS_COLORS[card.data.status || ""] || C.textMuted;
  const openHref = card.data.task_id ? `/tasks?taskId=${card.data.task_id}` : "/tasks";

  return (
    <>
      <div className="relative flex items-center justify-between px-5 py-3 border-b border-white/[0.06]">
        <div className="flex items-center gap-2.5 min-w-0">
          <CircleDot
            size={14}
            style={{ color: statusColor }}
            className="shrink-0"
          />
          <div className="min-w-0">
            <div
              className="text-[12px] font-medium leading-snug truncate"
              style={{ color: "var(--color-text-primary)" }}
            >
              {title}
            </div>
            <div
              className="text-[10px] mt-0.5"
              style={{ color: "var(--color-text-muted)" }}
            >
              {[card.data.status, card.data.assignee, card.data.priority].filter(Boolean).join(" · ")}
            </div>
          </div>
        </div>
        <div className="flex items-center gap-1 shrink-0">
          <HangupChip />
          <a
            href={openHref}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-center gap-1 px-2 py-1 rounded text-[10px] hover:bg-white/5 cursor-pointer"
            style={{ color: "var(--color-text-secondary)" }}
            title="In Tasks-Board öffnen (neuer Tab)"
          >
            <ExternalLink size={11} /> Tasks
          </a>
          <button
            type="button"
            onClick={onClose}
            className="p-1.5 rounded hover:bg-white/5 cursor-pointer"
            aria-label="Schliessen"
          >
            <X size={14} style={{ color: "var(--color-text-secondary)" }} />
          </button>
        </div>
      </div>
      <div className="px-6 py-6 text-xs" style={{ color: "var(--color-text-body)" }}>
        <div className="grid grid-cols-2 gap-y-2 gap-x-6 max-w-md">
          <span style={{ color: "var(--color-text-muted)" }}>Status</span>
          <span style={{ color: statusColor }}>{card.data.status || "—"}</span>
          <span style={{ color: "var(--color-text-muted)" }}>Zuständig</span>
          <span>{card.data.assignee || "—"}</span>
          <span style={{ color: "var(--color-text-muted)" }}>Priorität</span>
          <span>{card.data.priority || "—"}</span>
          {card.data.task_id && (
            <>
              <span style={{ color: "var(--color-text-muted)" }}>ID</span>
              <span className="font-mono text-[10px] truncate">{card.data.task_id}</span>
            </>
          )}
        </div>
      </div>
    </>
  );
}
