/**
 * Voice-Display Card-Types — shape stays in sync with backend's
 * VoiceDisplayCard (backend/app/routers/voice.py).
 *
 * Cards arrive via WS /api/v1/vault/voice-display (Redis channel
 * voice:display) and stack in the VoiceDrawer. `kind` discriminates
 * which Card component renders the data; payloads are kind-specific.
 */

export type MemoryCardData = {
  vault_path?: string | null;
  title?: string | null;
  type?: string | null;
  agent?: string | null;
  date?: string | null;
  snippet?: string | null;
};

export type UrlCardData = {
  url: string;
  domain?: string | null;
};

export type FileCardData = {
  vault_path?: string | null;
  title?: string | null;
  type?: string | null;
  agent?: string | null;
  date?: string | null;
};

export type TaskCardData = {
  task_id?: string | null;
  title?: string | null;
  status?: string | null;
  assignee?: string | null;
  priority?: string | null;
};

export type DisplayCardMessage =
  | { kind: "memory"; data: MemoryCardData; title?: string | null; requested_at?: string }
  | { kind: "url"; data: UrlCardData; title?: string | null; requested_at?: string }
  | { kind: "file"; data: FileCardData; title?: string | null; requested_at?: string }
  | { kind: "task"; data: TaskCardData; title?: string | null; requested_at?: string };

// Frontend wraps the message with a stable id for animation keying — the
// backend doesn't generate one (Redis pub/sub is fire-and-forget).
export type DisplayCard = DisplayCardMessage & { id: string };
