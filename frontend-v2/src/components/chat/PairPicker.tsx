"use client";

/**
 * PairPicker — moves a chat to another pair: harness × runtime × model
 * (ADR-105, S3 item D).
 *
 * What it offers comes from the server, never from a local list:
 * - which harness may run which runtime: `GET /runtimes/compat-matrix`
 *   (harness_compat) — an incompatible pair is not listed at all;
 * - which harnesses can hold a chat at all: `CHAT_HARNESSES`, the mirror of
 *   the backend's `chat_record.RESUME_STYLE` (the switch API answers 400
 *   `harness_cannot_hold_chats` for any other);
 * - whether a local model is loaded: the runtime row's `serving_since`
 *   (set by the runtime watcher). A model that is not loaded needs a switch
 *   of its box, which takes the box from every chat on it — the row says how
 *   many.
 *
 * A running turn is the server's call too (F1): the switch is sent as it
 * is; a 409 `turn_running` opens the two-button sheet with exactly the
 * `options` the server named. "Switch after this answer" only appears when
 * the server offers `after_turn` (today it does not — plan §6).
 *
 * An agent's own (default) chat IS the agent's process: the server moves it
 * only to another model of the same runtime (409
 * `default_chat_follows_agent` otherwise). The picker says so and links to
 * the agent's runtime switch instead of listing pairs that would be refused.
 */
import { useCallback, useMemo, useReducer, useState } from "react";
import Link from "next/link";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Check, ChevronRight, X } from "lucide-react";

import {
  api,
  switchRefusal,
  type RecordChat,
  type SwitchPair,
  type SwitchRefusal,
  type SwitchRequest,
} from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { useRuntimeRows } from "@/lib/runtimeRows";
import type { CompatMatrix, Runtime } from "@/lib/types";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { RECORD_CHATS_QUERY_KEY, recordSwitchesQueryKey } from "@/hooks/useChatSwitch";

/** Harnesses that can hold a chat of their own — backend
 *  `chat_record.RESUME_STYLE` (claude: cli, omp: acp). */
export const CHAT_HARNESSES: readonly string[] = ["claude", "omp"];

/** The modes the busy sheet has words for. A mode the server offers one day
 *  without words here is not shown (never a raw key on screen). */
export const BUSY_MODES = ["stop_and_switch", "after_turn"] as const;
export type BusyMode = (typeof BUSY_MODES)[number];

// ── Labels ──────────────────────────────────────────────────────────────

export type PairLookups = {
  harnessLabel: (key: string | null | undefined) => string | null;
  runtime: (id: string | null | undefined) => Pick<Runtime, "id" | "display_name" | "model_identifier"> | null;
};

export function makeLookups(matrix: CompatMatrix | undefined, rows: Runtime[] | undefined): PairLookups {
  const labels = new Map((matrix?.harnesses ?? []).map((h) => [h.key as string, h.label]));
  const byId = new Map((rows ?? []).map((r) => [r.id, r]));
  return {
    harnessLabel: (k) => (k ? labels.get(k) ?? k : null),
    runtime: (id) => (id ? byId.get(id) ?? null : null),
  };
}

/** "Claude Code · claude-opus-4-1": the harness, then the model (or the
 *  runtime's name when no model is known). Unknown parts are left out (K3). */
export function pairLabel(pair: SwitchPair | null | undefined, lookups: PairLookups): string {
  if (!pair) return "";
  const rt = lookups.runtime(pair.runtimeId);
  const model = pair.model || rt?.model_identifier || rt?.display_name || null;
  return [lookups.harnessLabel(pair.harness), model].filter(Boolean).join(" · ");
}

export const COMPAT_MATRIX_QUERY_KEY = ["compat-matrix"] as const;

/** Labels from the same queries the runtimes page uses (needs a query client). */
export function usePairLookups(): PairLookups {
  const { data: matrix } = useQuery({
    queryKey: COMPAT_MATRIX_QUERY_KEY,
    queryFn: () => api.runtimes.compatMatrix(),
    staleTime: 5 * 60_000,
  });
  const { data: rows } = useRuntimeRows();
  return useMemo(() => makeLookups(matrix, rows?.runtimes), [matrix, rows]);
}

// ── The options ─────────────────────────────────────────────────────────

export type PairOption = {
  key: string;
  harness: string;
  runtimeId: string;
  /** What the row is called: the model the runtime serves, else its name. */
  title: string;
  /** The box (local) — null for a cloud runtime. */
  box: string | null;
  local: boolean;
  /** Local only: the model is up on its box right now. */
  loaded: boolean;
  /** Local and not loaded: chats on that box that lose their model. */
  affects: number;
  current: boolean;
  model: string | null;
};

function hostIds(rt: Pick<Runtime, "host" | "member_hosts">): string[] {
  return [rt.host?.id, ...(rt.member_hosts ?? []).map((m) => m.host_id)].filter((x): x is string => !!x);
}

/**
 * Every pair the chat may move to, per harness. Incompatible pairs and
 * harnesses that cannot hold a chat are not listed (the server would refuse
 * them). `affects` counts the other chats whose runtime is serving on the
 * box a not-loaded model would take over.
 */
export function buildPairOptions(input: {
  matrix: Pick<CompatMatrix, "harnesses" | "runtimes"> | undefined;
  rows: Runtime[] | undefined;
  chat: Pick<RecordChat, "id" | "harness" | "model" | "runtimeId">;
  chats: Pick<RecordChat, "id" | "runtimeId" | "archived">[];
}): Map<string, PairOption[]> {
  const out = new Map<string, PairOption[]>();
  const { matrix, rows, chat, chats } = input;
  if (!matrix || !rows) return out;
  const bySlug = new Map(rows.map((r) => [r.slug ?? r.id, r]));
  const serving = rows.filter((r) => r.enabled !== false && !!r.serving_since);
  for (const h of matrix.harnesses) {
    if (!CHAT_HARNESSES.includes(h.key)) continue;
    const list: PairOption[] = [];
    for (const m of matrix.runtimes) {
      if (!m.compatible_harnesses.includes(h.key)) continue;
      const rt = bySlug.get(m.slug);
      if (!rt || rt.enabled === false) continue;
      const local = (rt.locality ?? m.locality) === "local";
      const loaded = local && !!rt.serving_since;
      let affects = 0;
      if (local && !loaded) {
        const boxes = new Set(hostIds(rt));
        const displaced = new Set(
          serving.filter((s) => s.id !== rt.id && hostIds(s).some((id) => boxes.has(id))).map((s) => s.id),
        );
        affects = chats.filter((c) => c.id !== chat.id && !c.archived && !!c.runtimeId && displaced.has(c.runtimeId)).length;
      }
      list.push({
        key: `${h.key}:${rt.id}`,
        harness: h.key,
        runtimeId: rt.id,
        title: rt.model_identifier || rt.display_name,
        box: local ? rt.host?.display_name ?? null : null,
        local,
        loaded,
        affects,
        current: chat.harness === h.key && chat.runtimeId === rt.id,
        model: rt.model_identifier ?? null,
      });
    }
    if (list.length) out.set(h.key, list);
  }
  return out;
}

// ── The switch action (picker and the card's "switch back") ────────────

export type SwitchTarget = { kind: "pair"; body: SwitchRequest } | { kind: "back"; mode?: BusyMode };

export type SwitchActionState =
  | { phase: "idle" }
  | { phase: "sending"; target: SwitchTarget }
  | { phase: "busy"; target: SwitchTarget; options: BusyMode[] }
  | { phase: "refused"; target: SwitchTarget; code: string; message: string | null };

export type SwitchActionEvent =
  | { type: "send"; target: SwitchTarget }
  | { type: "refused"; refusal: SwitchRefusal }
  | { type: "done" }
  | { type: "cancel" };

/** The picker's state machine: send → done, or → busy (409 turn_running
 *  with options the UI knows), or → refused (anything else). */
export function switchActionReducer(state: SwitchActionState, ev: SwitchActionEvent): SwitchActionState {
  switch (ev.type) {
    case "send":
      return { phase: "sending", target: ev.target };
    case "refused": {
      if (state.phase !== "sending") return state;
      const { refusal } = ev;
      // A request that already carried a mode is never asked about again.
      const alreadyChose = state.target.kind === "pair" ? !!state.target.body.mode : !!state.target.mode;
      if (refusal.code === "turn_running" && !alreadyChose) {
        const options = BUSY_MODES.filter((m) => refusal.options.includes(m));
        if (options.length) return { phase: "busy", target: state.target, options };
      }
      return { phase: "refused", target: state.target, code: refusal.code, message: refusal.message };
    }
    case "done":
    case "cancel":
      return { phase: "idle" };
  }
}

function withMode(target: SwitchTarget, mode: BusyMode): SwitchTarget {
  return target.kind === "pair" ? { kind: "pair", body: { ...target.body, mode } } : { kind: "back", mode };
}

export function useSwitchAction(chatId: string | null, onDone?: () => void) {
  const [state, dispatch] = useReducer(switchActionReducer, { phase: "idle" } as SwitchActionState);
  const qc = useQueryClient();
  const run = useCallback(
    async (target: SwitchTarget, mode?: BusyMode) => {
      if (!chatId) return;
      const t = mode ? withMode(target, mode) : target;
      dispatch({ type: "send", target: t });
      try {
        if (t.kind === "pair") await api.chatRecord.switchChat(chatId, t.body);
        else await api.chatRecord.switchBack(chatId, t.mode);
        dispatch({ type: "done" });
        onDone?.();
      } catch (err) {
        const refusal = switchRefusal(err) ?? { status: 0, code: "network", options: [], message: null };
        dispatch({ type: "refused", refusal });
      } finally {
        qc.invalidateQueries({ queryKey: recordSwitchesQueryKey(chatId) });
        qc.invalidateQueries({ queryKey: RECORD_CHATS_QUERY_KEY });
      }
    },
    [chatId, qc, onDone],
  );
  return {
    state,
    send: (target: SwitchTarget) => run(target),
    choose: (mode: BusyMode) => {
      if (state.phase !== "busy") return;
      void run(state.target, mode);
    },
    cancel: () => dispatch({ type: "cancel" }),
  };
}

// ── The busy sheet ──────────────────────────────────────────────────────

export function BusySheet({
  options,
  onChoose,
  onCancel,
}: {
  options: BusyMode[] | null;
  onChoose: (mode: BusyMode) => void;
  onCancel: () => void;
}) {
  const t = useTranslations("cockpit.switch");
  return (
    <ResponsiveModal open={!!options} onClose={onCancel} aria-labelledby="switch-busy-title">
      <div data-testid="switch-busy-sheet" className="px-4 pt-3 pb-4 flex flex-col gap-3">
        <h2 id="switch-busy-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
          {t("busyTitle")}
        </h2>
        <p className="text-sm leading-normal" style={{ color: C.textSecondary }}>
          {t("busyBody")}
        </p>
        <div className="flex flex-col gap-2 pt-1">
          {(options ?? []).map((mode, i) => (
            <button
              key={mode}
              type="button"
              data-testid={`switch-busy-${mode}`}
              onClick={() => onChoose(mode)}
              className="min-h-touch rounded-md text-sm font-medium cursor-pointer"
              style={
                i === 0
                  ? { background: C.accent, color: C.onAccent }
                  : { color: C.textSecondary, border: `1px solid ${C.borderActive}` }
              }
            >
              {t(mode === "stop_and_switch" ? "busyStop" : "busyAfter")}
            </button>
          ))}
          <button
            type="button"
            data-testid="switch-busy-cancel"
            onClick={onCancel}
            className="min-h-touch rounded-md text-sm cursor-pointer"
            style={{ color: C.textMuted }}
          >
            {t("cancel")}
          </button>
        </div>
      </div>
    </ResponsiveModal>
  );
}

/** A refusal in the UI language: a known code has a sentence, the server's
 *  own message (German from harness_compat) follows only for the pair rule. */
export function useRefusalText() {
  const t = useTranslations("cockpit.switch");
  return (code: string): string => {
    const key = `refused.${code}`;
    return t.has(key) ? t(key) : t("refused.generic");
  };
}

// ── The picker ──────────────────────────────────────────────────────────

export function PairPicker({
  open,
  onClose,
  chat,
  chats,
  agentId,
}: {
  open: boolean;
  onClose: () => void;
  chat: RecordChat;
  chats: RecordChat[];
  agentId: string | null;
}) {
  const t = useTranslations("cockpit.switch");
  const refusalText = useRefusalText();
  const lookups = usePairLookups();
  const { data: matrix } = useQuery({
    queryKey: COMPAT_MATRIX_QUERY_KEY,
    queryFn: () => api.runtimes.compatMatrix(),
    staleTime: 5 * 60_000,
    enabled: open,
  });
  const { data: rows } = useRuntimeRows(open);
  const options = useMemo(
    () => buildPairOptions({ matrix, rows: rows?.runtimes, chat, chats }),
    [matrix, rows, chat, chats],
  );
  const harnesses = [...options.keys()];
  const [picked, setPicked] = useState<string | null>(null);
  const harness = picked && options.has(picked) ? picked : chat.harness && options.has(chat.harness) ? chat.harness : harnesses[0] ?? null;
  const list = harness ? options.get(harness) ?? [] : [];
  const ready = list.filter((o) => !o.local || o.loaded);
  const needsBox = list.filter((o) => o.local && !o.loaded);
  const action = useSwitchAction(chat.id, onClose);
  const sending = action.state.phase === "sending";
  const current = pairLabel({ harness: chat.harness, runtimeId: chat.runtimeId ?? null, model: chat.model }, lookups);

  const row = (o: PairOption) => {
    const sub = o.local
      ? // The group heading already says the box must switch (K3): the row
        // adds only what it costs.
        [o.box, o.loaded ? t("loaded") : o.affects > 0 ? t("affects", { count: o.affects }) : null]
          .filter(Boolean)
          .join(" · ")
      : null;
    return (
      <button
        key={o.key}
        type="button"
        role="radio"
        aria-checked={o.current}
        data-testid="pair-option"
        data-pair={o.key}
        data-loaded={o.local ? String(o.loaded) : undefined}
        disabled={o.current || sending}
        onClick={() =>
          action.send({ kind: "pair", body: { harness: o.harness, runtimeId: o.runtimeId, model: o.model } })
        }
        className="flex items-center gap-3 min-h-touch px-2 py-2 rounded-lg text-left cursor-pointer disabled:cursor-default hover:bg-[var(--color-bg-hover)] disabled:hover:bg-transparent"
      >
        <span className="flex-1 min-w-0">
          <span className="block truncate text-sm" style={{ color: o.current ? C.textPrimary : C.textSecondary }}>
            {o.title}
          </span>
          {sub && (
            <span className="block truncate text-xs" style={{ color: C.textMuted }}>
              {sub}
            </span>
          )}
        </span>
        {o.current && <Check size={15} style={{ color: C.accent }} aria-hidden="true" />}
      </button>
    );
  };

  return (
    <>
      <ResponsiveModal open={open && action.state.phase !== "busy"} onClose={onClose} aria-labelledby="pair-picker-title">
        <div data-testid="pair-picker" className="flex flex-col min-h-0 px-4 pt-3 pb-4">
          <div className="flex items-center justify-between">
            <h2 id="pair-picker-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
              {t("pickerTitle")}
            </h2>
            <button
              type="button"
              onClick={onClose}
              aria-label={t("close")}
              className="flex items-center justify-center w-11 h-11 -mr-2 rounded-md cursor-pointer"
              style={{ color: C.textMuted }}
            >
              <X size={17} aria-hidden="true" />
            </button>
          </div>
          {current && (
            <p data-testid="pair-current" className="text-sm truncate" style={{ color: C.textMuted }}>
              {t("current", { pair: current })}
            </p>
          )}

          {chat.agentDefault ? (
            <div data-testid="pair-follows-agent" className="pt-4 flex flex-col gap-1">
              <p className="text-sm leading-normal" style={{ color: C.textSecondary }}>
                {t("followsAgent")}
              </p>
              {agentId && (
                <Link
                  href={`/agents/${encodeURIComponent(agentId)}`}
                  onClick={onClose}
                  className="inline-flex min-h-11 items-center gap-1 text-sm font-medium"
                  style={{ color: C.accent }}
                >
                  {t("agentRuntimeLink")}
                  <ChevronRight size={14} aria-hidden="true" />
                </Link>
              )}
            </div>
          ) : (
            <>
              {harnesses.length > 1 && (
                <div
                  role="radiogroup"
                  aria-label={t("harness")}
                  className="flex items-center rounded-md overflow-hidden mt-4"
                  style={{ border: `1px solid ${C.border}` }}
                >
                  {harnesses.map((h, i) => (
                    <button
                      key={h}
                      type="button"
                      role="radio"
                      aria-checked={h === harness}
                      data-testid={`pair-harness-${h}`}
                      onClick={() => setPicked(h)}
                      className="flex-1 min-h-touch text-sm font-medium cursor-pointer"
                      style={{
                        background: h === harness ? C.accentSubtle : "transparent",
                        color: h === harness ? C.accent : C.textSecondary,
                        borderLeft: i > 0 ? `1px solid ${C.border}` : undefined,
                      }}
                    >
                      {lookups.harnessLabel(h)}
                    </button>
                  ))}
                </div>
              )}
              <div className="overflow-y-auto min-h-0" role="radiogroup" aria-label={t("pickerTitle")}>
                {ready.length > 0 && (
                  <>
                    <div className="label-sys px-2 pt-4 pb-1" style={{ color: C.textMuted }}>
                      {t("groupReady")}
                    </div>
                    {ready.map(row)}
                  </>
                )}
                {needsBox.length > 0 && (
                  <>
                    <div className="label-sys px-2 pt-4 pb-1" style={{ color: C.textMuted }}>
                      {t("groupNeedsBox")}
                    </div>
                    {needsBox.map(row)}
                  </>
                )}
                {list.length === 0 && (
                  <p className="pt-4 text-sm" style={{ color: C.textMuted }}>
                    {t("noPairs")}
                  </p>
                )}
              </div>
            </>
          )}

          {action.state.phase === "refused" && (
            <p data-testid="pair-refused" role="alert" className="pt-3 text-sm leading-normal" style={{ color: STATUS_TEXT.error }}>
              {refusalText(action.state.code)}
            </p>
          )}
        </div>
      </ResponsiveModal>
      <BusySheet
        options={action.state.phase === "busy" ? action.state.options : null}
        onChoose={action.choose}
        onCancel={action.cancel}
      />
    </>
  );
}
