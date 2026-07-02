"use client";

import { FileIcon, Send, Eye } from "lucide-react";
import { useMutation } from "@tanstack/react-query";
import type { FileCardData } from "./types";
import { CardShell } from "./CardShell";
import { request } from "@/lib/api";
import { C } from "@/lib/colors";

/**
 * FileCard — Vault file (PDF / image / doc) with "preview" + "send to
 * Telegram" actions.
 *
 * Telegram action calls the same /api/v1/agent/me/telegram endpoint that
 * Jarvis's deliver_to_telegram tool uses — the operator gets a duplicate path
 * to ship the file without saying it out loud.
 */
export function FileCard({
  data,
  title,
  onClose,
  onPreview,
}: {
  data: FileCardData;
  title?: string | null;
  onClose: () => void;
  onPreview: () => void;
}) {
  const displayTitle =
    title || data.title || (data.vault_path || "").split("/").pop() || "Datei";

  const sendToTelegram = useMutation({
    mutationFn: () =>
      request("/api/v1/me/telegram", {
        method: "POST",
        body: JSON.stringify({
          text: `Aus dem Vault: ${displayTitle}`,
          vault_path: data.vault_path,
        }),
      }),
  });

  return (
    <CardShell
      onClose={onClose}
      icon={<FileIcon size={13} style={{ color: C.accent }} />}
      kind="file"
      meta={[data.type, data.agent].filter(Boolean).join(" · ")}
    >
      <div className="flex items-start gap-1.5 min-w-0 flex-1">
        <div className="min-w-0 flex-1">
          <button
            type="button"
            onClick={onPreview}
            className="text-[11px] font-medium leading-snug truncate text-left hover:text-white transition-colors cursor-pointer w-full"
            style={{ color: "var(--color-text-primary)" }}
          >
            {displayTitle}
          </button>
          <div className="flex items-center gap-2 mt-1">
            <button
              type="button"
              onClick={onPreview}
              className="text-[10px] flex items-center gap-0.5 hover:opacity-80 cursor-pointer"
              style={{ color: "var(--color-text-secondary)" }}
            >
              <Eye size={9} /> Vorschau
            </button>
            {data.vault_path && (
              <button
                type="button"
                onClick={() => sendToTelegram.mutate()}
                disabled={sendToTelegram.isPending || sendToTelegram.isSuccess}
                className="text-[10px] flex items-center gap-0.5 transition-opacity disabled:opacity-50 hover:opacity-80 cursor-pointer"
                style={{ color: "var(--color-text-secondary)" }}
              >
                <Send size={9} />
                {sendToTelegram.isSuccess
                  ? "Gesendet"
                  : sendToTelegram.isPending
                  ? "…"
                  : "Telegram"}
              </button>
            )}
          </div>
        </div>
      </div>
    </CardShell>
  );
}
