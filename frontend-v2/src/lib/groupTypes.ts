/**
 * Gruppenchat-Typen (ADR-075) — Spiegel der Backend-Serialisierer in
 * `backend/app/routers/groups.py`. Bewusst getrennt von `chatTypes.ts`:
 * jenes beschreibt Transkript-EVENTS eines einzelnen CLI-Agenten, hier geht
 * es um comm_v2-NACHRICHTEN auf einem gemeinsamen Thread (seq-geordnet).
 */

/** Status-Maschine der Gruppe (models/group.py GROUP_STATUSES).
 *  `idle` = keine Runde aktiv → Live-Verhalten (Erwähnte antworten direkt).
 *  Es gibt bewusst KEINEN Modus-Schalter; das Verhalten folgt aus dem Status. */
export type GroupStatus =
  | "draft"
  | "idle"
  | "running"
  | "waiting_gate"
  | "paused"
  | "done"
  | "failed";

export type GroupLifecycle = "one_shot" | "standing";

export type GroupMemberRole = "lead" | "critic" | "member";

export interface GroupMemberInfo {
  id: string;
  name: string;
  slug: string | null;
  emoji: string | null;
  role: GroupMemberRole;
  archived: boolean;
}

/** Zeile in der Sidebar-Liste (`GET /groups`). */
export interface GroupSummary {
  id: string;
  thread_id: string;
  name: string;
  goal: string;
  status: GroupStatus;
  lifecycle: GroupLifecycle;
  member_count: number;
  rounds_completed: number;
  current_round_no: number;
  max_rounds: number;
  created_at: string | null;
  /** Vorschau der letzten Nachricht — optional, damit die UI auch gegen ein
   *  Backend ohne dieses Feld sauber rendert (dann bleibt die Zeile leer). */
  last_message?: { body: string; sender: string; created_at: string | null } | null;
  /** Avatare für den AvatarStack; optional aus demselben Grund. */
  member_avatars?: { id: string; emoji: string | null; name: string }[];
}

/** Detail (`GET /groups/{id}`) — enthält die Mitglieder. */
export interface GroupDetail extends Omit<GroupSummary, "member_count"> {
  lead_agent_id: string | null;
  max_duration_minutes: number | null;
  budget_usd: number | null;
  budget_tokens: number | null;
  result_doc_rel_path: string | null;
  members: GroupMemberInfo[];
}

export interface GroupMessage {
  id: string;
  thread_id: string;
  seq: number;
  sender_type: "user" | "agent" | "system";
  sender_id: string | null;
  message_type: string;
  body: string;
  mentions: string[];
  created_at: string | null;
  /** Nur clientseitig: eine gerade abgeschickte, noch nicht bestätigte
   *  Nachricht (optimistisches Echo) — nie vom Server. */
  pending?: boolean;
}

export interface GroupRoundInfo {
  id: string;
  round_no: number;
  kind: "autonomous" | "live_impulse";
  /** seq des Runden-Briefs — die UI setzt den Runden-Trenner genau davor,
   *  statt den Brief-Text zu parsen. */
  brief_seq: number | null;
  outcome: string | null;
  report: string | null;
  pending_speakers: string[];
  has_doc_snapshot: boolean;
  tokens_used: number | null;
  cost_usd: number | null;
  started_at: string | null;
  finished_at: string | null;
}

export interface GroupDocument {
  rel_path: string;
  content: string;
  /** null = aktueller Datei-Stand; sonst die Runde des Snapshots. */
  version: number | null;
  mtime?: number;
}

export interface EligibleMember {
  id: string;
  name: string;
  slug: string | null;
  emoji: string | null;
}

export interface GroupCreatePayload {
  goal: string;
  member_ids: string[];
  name?: string;
  lead_agent_id?: string;
  lifecycle?: GroupLifecycle;
  max_rounds?: number;
  budget_usd?: number | null;
}

/** SSE-Nutzlast des Gruppen-Kanals (`mc:events:group:{id}`). */
export interface GroupStreamState {
  /** Läuft gerade eine Runde, und auf wen wartet sie? */
  roundNo: number | null;
  maxRounds: number | null;
  pendingSpeakers: string[];
  /** Wer schreibt gerade (Lead-Synthese o.ä.) — null = niemand bekannt. */
  activeSpeaker: string | null;
  /** Offene Frage an Mark (Gate) — null = keine. */
  gateQuestion: string | null;
  /** Kosten der zuletzt abgeschlossenen Runde (USD). */
  lastRoundCostUsd: number | null;
  /** Version des zuletzt aktualisierten Ergebnis-Dokuments. */
  docVersion: number | null;
}

export const EMPTY_GROUP_STREAM_STATE: GroupStreamState = {
  roundNo: null,
  maxRounds: null,
  pendingSpeakers: [],
  activeSpeaker: null,
  gateQuestion: null,
  lastRoundCostUsd: null,
  docVersion: null,
};

/** Welcher Chip eine Gruppenzeile trägt — genau einer, nie mehrere (Design-DNA:
 *  Farbe trägt Bedeutung, nicht Schmuck).
 *
 *  Bewusst eine ART, kein fertiger Text: MC läuft deutsch UND englisch, und
 *  ein Helfer, der Anzeigetexte zurückgibt, wird früher oder später direkt
 *  gerendert — dann steht in der englischen Oberfläche „wartet". Die Zuordnung
 *  Art → i18n-Schlüssel macht die Komponente. */
export type GroupChipKind = "waiting" | "round" | "paused" | "done" | "failed";

export function groupChipKind(group: GroupSummary): GroupChipKind | null {
  switch (group.status) {
    case "waiting_gate":
      return "waiting";
    case "running":
      return "round";
    case "paused":
      return "paused";
    case "done":
      return "done";
    case "failed":
      return "failed";
    default:
      return null; // idle/draft = Live-Betrieb, kein Chip
  }
}

/** Gruppen, die auf Mark warten, gehören nach oben. */
export function sortGroups(groups: GroupSummary[]): GroupSummary[] {
  const rank = (g: GroupSummary) =>
    g.status === "waiting_gate" ? 0 : g.status === "running" ? 1 : g.status === "done" ? 3 : 2;
  return [...groups].sort((a, b) => {
    const d = rank(a) - rank(b);
    if (d !== 0) return d;
    return (b.created_at ?? "").localeCompare(a.created_at ?? "");
  });
}
