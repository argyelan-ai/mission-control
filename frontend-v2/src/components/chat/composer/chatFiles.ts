import { request } from "@/lib/api";

/** One file of the chat's working folder, relative to it. */
export interface ChatFile {
  path: string;
  name: string;
}

export interface ChatFileList {
  /** The folder as the agent names it (`/workspace/proj`, `~/…`). */
  root: string;
  files: ChatFile[];
  /** The folder was larger than one listing walks. */
  truncated: boolean;
}

/** `GET /agents/{id}/chat/files` — read-only; 404 `no_workspace` when the
 *  chat has no readable folder. */
export function fetchChatFiles(agentId: string, query: string): Promise<ChatFileList> {
  const params = new URLSearchParams({ q: query, limit: "30" });
  return request<ChatFileList>(`/api/v1/agents/${agentId}/chat/files?${params.toString()}`);
}

export type ChatFileStatus = "idle" | "loading" | "ready" | "no_folder" | "error";
