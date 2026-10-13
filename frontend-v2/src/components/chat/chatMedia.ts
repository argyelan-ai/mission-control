"use client";

/**
 * Which agent's tool pictures a chat shows (chat package P2).
 *
 * A tool row knows its event, not the agent it belongs to — and the picture
 * URL needs the agent (`/agents/{id}/chat/media/{mediaId}`, scoped so one
 * agent's chat can never fetch another's). ChatView provides the id; rows
 * outside a chat (AgentCard, a head's read-only transcript) get `null` and
 * show no picture instead of guessing an address.
 */
import { createContext, useContext } from "react";
import { api } from "@/lib/api";
import type { ToolImage } from "@/lib/chatTypes";

export const ChatMediaContext = createContext<string | null>(null);

export type MediaUrl = (mediaId: string, variant: "thumb" | "full") => string;

/** URL builder for this chat's pictures, or `null` outside an agent chat. */
export function useChatMediaUrl(): MediaUrl | null {
  const agentId = useContext(ChatMediaContext);
  if (!agentId) return null;
  return (mediaId, variant) => api.chat.mediaUrl(agentId, mediaId, variant);
}

const EXT: Record<ToolImage["mime"], string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/webp": "webp",
  "image/gif": "gif",
};

/** File name for "download": short, stable, no path or agent name. */
export function downloadName(image: ToolImage): string {
  return `mc-image-${image.id.slice(0, 12)}.${EXT[image.mime] ?? "png"}`;
}
