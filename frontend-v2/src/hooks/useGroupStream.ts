"use client";

/**
 * useGroupStream — Live-Datenhaken eines Gruppenraums (ADR-075).
 *
 * Anders als `useChatStream` (Transkript-EVENTS eines CLI-Agenten) hängt der
 * Gruppenraum an comm_v2-NACHRICHTEN: seq-geordnet, aus der DB, mehrere
 * Absender. Der Merge folgt deshalb dem ThreadPanel-Muster (dedupe über
 * `seq`), die Zustellung dem SSE-Muster der App — mit Polling als Netz,
 * wenn der Strom hängt (dieselbe Vorsicht wie ThreadPanel: lieber doppelt
 * geholt als stumm veraltet).
 *
 * Ehrlichkeits-Regel wie im Sessions-Chat: Ist der Strom weg, behauptet der
 * Haken keinen Zustand — `connected: false` sagt der Statuszeile, dass sie
 * „Status unklar" zeigen muss, statt einen plausiblen Verlauf zu erfinden.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useSSE } from "@/lib/sse";
import {
  EMPTY_GROUP_STREAM_STATE,
  type GroupMessage,
  type GroupStreamState,
} from "@/lib/groupTypes";

/** Netz unter dem SSE-Strom: Delta-Poll, damit ein stiller Ausfall den Raum
 *  nicht einfrieren lässt (Ursache-Klasse „läuft ≠ arbeitet"). */
const POLL_MS = 10_000;
const PAGE_LIMIT = 200;

export function mergeBySeq(existing: GroupMessage[], incoming: GroupMessage[]): GroupMessage[] {
  if (incoming.length === 0) return existing;
  const bySeq = new Map<number, GroupMessage>();
  for (const m of existing) if (!m.pending) bySeq.set(m.seq, m);
  for (const m of incoming) bySeq.set(m.seq, m);
  const merged = [...bySeq.values()].sort((a, b) => a.seq - b.seq);
  // Optimistische Echos, die der Server noch nicht bestätigt hat, bleiben
  // hinten stehen — bis eine echte Nachricht mit gleichem Text ankommt.
  const settledBodies = new Set(merged.map((m) => m.body));
  const stillPending = existing.filter((m) => m.pending && !settledBodies.has(m.body));
  return [...merged, ...stillPending];
}

interface UseGroupStreamResult {
  messages: GroupMessage[];
  state: GroupStreamState;
  connected: boolean;
  loading: boolean;
  error: boolean;
  hasMoreBefore: boolean;
  send: (text: string) => Promise<void>;
  sending: boolean;
  loadOlder: () => void;
  loadingOlder: boolean;
  /** Signal für Panels: Version des Ergebnis-Dokuments, zuletzt gesehen. */
  docVersion: number | null;
  refresh: () => void;
}

export function useGroupStream(groupId: string | null): UseGroupStreamResult {
  const [messages, setMessages] = useState<GroupMessage[]>([]);
  const [state, setState] = useState<GroupStreamState>(EMPTY_GROUP_STREAM_STATE);
  const [connected, setConnected] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [hasMoreBefore, setHasMoreBefore] = useState(false);
  const [sending, setSending] = useState(false);
  const [loadingOlder, setLoadingOlder] = useState(false);

  const latestSeq = useRef(0);
  const oldestSeq = useRef(0);

  const applyIncoming = useCallback((incoming: GroupMessage[]) => {
    if (incoming.length === 0) return;
    setMessages((prev) => {
      const merged = mergeBySeq(prev, incoming);
      const settled = merged.filter((m) => !m.pending);
      if (settled.length > 0) {
        latestSeq.current = Math.max(latestSeq.current, settled[settled.length - 1].seq);
        oldestSeq.current = oldestSeq.current === 0
          ? settled[0].seq
          : Math.min(oldestSeq.current, settled[0].seq);
      }
      return merged;
    });
  }, []);

  // Erstladung — bei jedem Gruppenwechsel von vorn (Reset wie session_changed).
  useEffect(() => {
    latestSeq.current = 0;
    oldestSeq.current = 0;
    setMessages([]);
    setState(EMPTY_GROUP_STREAM_STATE);
    setError(false);
    if (!groupId) return;
    let cancelled = false;
    setLoading(true);
    api.groups
      .messages(groupId, { limit: PAGE_LIMIT })
      .then((res) => {
        if (cancelled) return;
        setConnected(true);
        applyIncoming(res.messages);
        setHasMoreBefore(res.messages.length >= PAGE_LIMIT);
      })
      .catch(() => !cancelled && setError(true))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [groupId, applyIncoming]);

  const pullDelta = useCallback(() => {
    if (!groupId) return;
    api.groups
      .messages(groupId, { sinceSeq: latestSeq.current })
      .then((res) => {
        // Ein geglückter Abruf IST der Beweis, dass die Ansicht aktuell ist —
        // egal ob der SSE-Strom oder das Netz darunter sie geliefert hat.
        // Vorher hing `connected` allein an eingetroffenen Gruppen-Ereignissen;
        // in einer ruhigen Gruppe kam nie eines, und die Statuszeile behauptete
        // „Verbindung verloren", während alles lief (Operator-Befund 21.08.).
        setConnected(true);
        applyIncoming(res.messages);
      })
      .catch(() => setConnected(false));
  }, [groupId, applyIncoming]);

  // SSE: Nachrichten kommen direkt mit; Runden-/Gate-/Doc-Ereignisse pflegen
  // den abgeleiteten Zustand für Statuszeile und Ergebnis-Panel.
  useSSE(groupId ? `/api/v1/groups/${groupId}/stream` : "", {
    enabled: !!groupId,
    onEvent: (event, data) => {
      setConnected(true);
      switch (event) {
        case "group.message_posted": {
          const msg = data.message as GroupMessage | undefined;
          if (msg) applyIncoming([msg]);
          break;
        }
        case "group.round_started":
          setState((s) => ({
            ...s,
            roundNo: (data.round_no as number) ?? s.roundNo,
            maxRounds: (data.max_rounds as number) ?? s.maxRounds,
            pendingSpeakers: (data.pending_speakers as string[]) ?? [],
            activeSpeaker: null,
            gateQuestion: null,
          }));
          break;
        case "group.turn_started":
          setState((s) => ({ ...s, activeSpeaker: (data.speaker as string) ?? null }));
          break;
        case "group.round_completed":
          setState((s) => ({
            ...s,
            pendingSpeakers: [],
            activeSpeaker: null,
            lastRoundCostUsd: (data.cost_usd as number) ?? s.lastRoundCostUsd,
          }));
          break;
        case "group.doc_updated":
          setState((s) => ({ ...s, docVersion: (data.version as number) ?? s.docVersion }));
          break;
        case "group.gate_requested":
          setState((s) => ({ ...s, gateQuestion: (data.question as string) ?? "" }));
          break;
        case "group.status_changed":
          setState((s) => ({
            ...s,
            pendingSpeakers: [],
            activeSpeaker: null,
            gateQuestion: data.status === "waiting_gate" ? s.gateQuestion : null,
          }));
          break;
        default:
          break;
      }
    },
    onError: () => setConnected(false),
  });

  // Polling-Netz. Läuft bewusst auch bei bestehendem SSE-Strom weiter (billig,
  // dedupliziert über seq) — ein still gestorbener Strom sähe sonst aus wie
  // eine ruhige Gruppe.
  useEffect(() => {
    if (!groupId || error) return;
    const id = setInterval(pullDelta, POLL_MS);
    return () => clearInterval(id);
  }, [groupId, error, pullDelta]);

  const send = useCallback(
    async (text: string) => {
      const body = text.trim();
      if (!groupId || !body || sending) return;
      setSending(true);
      // Optimistisches Echo: die eigene Nachricht steht sofort im Raum, klar
      // als "unterwegs" markiert — der Server vergibt die echte seq.
      const echo: GroupMessage = {
        id: `pending-${Date.now()}`,
        thread_id: "",
        seq: Number.MAX_SAFE_INTEGER,
        sender_type: "user",
        sender_id: null,
        message_type: "message",
        body,
        mentions: [],
        created_at: new Date().toISOString(),
        pending: true,
      };
      setMessages((prev) => [...prev, echo]);
      try {
        const posted = await api.groups.postMessage(groupId, body);
        applyIncoming([posted]);
      } catch {
        setMessages((prev) => prev.filter((m) => m.id !== echo.id));
        throw new Error("send_failed");
      } finally {
        setSending(false);
      }
    },
    [groupId, sending, applyIncoming],
  );

  const loadOlder = useCallback(() => {
    if (!groupId || loadingOlder || oldestSeq.current <= 1) return;
    setLoadingOlder(true);
    api.groups
      .messages(groupId, { limit: PAGE_LIMIT })
      .then((res) => {
        applyIncoming(res.messages);
        setHasMoreBefore(false);
      })
      .catch(() => {})
      .finally(() => setLoadingOlder(false));
  }, [groupId, loadingOlder, applyIncoming]);

  const value = useMemo<UseGroupStreamResult>(
    () => ({
      messages,
      state,
      connected,
      loading,
      error,
      hasMoreBefore,
      send,
      sending,
      loadOlder,
      loadingOlder,
      docVersion: state.docVersion,
      refresh: pullDelta,
    }),
    [messages, state, connected, loading, error, hasMoreBefore, send, sending, loadOlder, loadingOlder, pullDelta],
  );

  return value;
}
