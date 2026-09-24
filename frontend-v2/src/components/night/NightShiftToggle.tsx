"use client";

/**
 * "Run tonight" on the task detail (ROADMAP E2).
 *
 *   ☾ Run tonight                                        [ ●  ]
 *     Queued · omp · GLM local — starts in tonight's window (22:00–06:00)
 *     Pair [ ● omp · GLM local                          ▾ ]
 *
 * On = the task is marked; mc-worker starts it as a head tonight, one after
 * another, local runtime first. Off = unmarked (bookkeeping only, nothing
 * runs). Once started, the head card above takes over and the switch locks.
 *
 * `canMark=false` (someone works on the card — `canMarkTonight`): no switch,
 * unless a mark exists already; then it stays visible with its reason (e.g.
 * "someone else took the task") so the operator can remove it.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Moon } from "lucide-react";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import { C, STATUS_TEXT } from "@/lib/colors";
import { loadRememberedPair, pairKey, pairLabel, type HeadPair, type HeadPairsResponse } from "@/lib/heads";
import { chooseTonightPair, nightErrorKey, nightReasonKey, pairsForTonight, type NightEntry } from "@/lib/nightShift";
import { HeadPairPicker } from "@/components/heads/HeadPairPicker";
import { NightSwitch } from "./NightSwitch";

export const NIGHT_TASK_KEY = (taskId: string) => ["nightShift", "task", taskId] as const;

export function NightShiftToggle({
  taskId,
  disabled = false,
  canMark = true,
}: {
  taskId: string;
  disabled?: boolean;
  canMark?: boolean;
}) {
  const t = useTranslations("nightShift");
  const qc = useQueryClient();
  const markQuery = useQuery({
    queryKey: NIGHT_TASK_KEY(taskId),
    queryFn: () => api.nightShift.getMark(taskId),
    retry: false,
    staleTime: 15_000,
  });
  const configQuery = useQuery({
    queryKey: ["nightShift", "config"],
    queryFn: () => api.nightShift.config(),
    retry: false,
    staleTime: 60_000,
  });
  const mark: NightEntry | null = markQuery.data?.mark ?? null;
  const [picking, setPicking] = useState(false);
  const pairsQuery = useQuery<HeadPairsResponse>({
    queryKey: ["heads", "pairs"],
    queryFn: () => api.heads.pairs(),
    enabled: picking || mark != null,
    retry: false,
    staleTime: 10_000,
  });
  const pairs = pairsQuery.data ? pairsForTonight(pairsQuery.data.pairs) : null;

  const refresh = () => {
    qc.invalidateQueries({ queryKey: ["nightShift"] });
    qc.invalidateQueries({ queryKey: ["tasks"] });
  };
  const markMutation = useMutation({
    mutationFn: (pair: HeadPair) => api.nightShift.mark(taskId, { harness: pair.harness, runtime_slug: pair.runtime_slug }),
    onSuccess: () => {
      notify.success(t("queued"));
      refresh();
    },
    onError: (err) => notify.error(t(nightErrorKey(err))),
  });
  const unmarkMutation = useMutation({
    mutationFn: () => api.nightShift.unmark(taskId),
    onSuccess: () => {
      notify.success(t("unqueued"));
      refresh();
    },
    onError: (err) => notify.error(t(nightErrorKey(err))),
  });
  const busy = markMutation.isPending || unmarkMutation.isPending;

  // The query failed (e.g. heads switched off meanwhile) → show nothing.
  if (markQuery.isError) return null;
  // Nobody may queue a card someone works on; an existing mark stays removable.
  if (!canMark && !mark) return null;

  const started = mark?.state === "started";
  const on = mark != null;

  const toggle = async (next: boolean) => {
    if (!next) {
      unmarkMutation.mutate();
      return;
    }
    setPicking(true);
    const resp = pairsQuery.data ?? (await qc.fetchQuery({ queryKey: ["heads", "pairs"], queryFn: () => api.heads.pairs() }).catch(() => null));
    const pair = chooseTonightPair(resp as HeadPairsResponse | null, loadRememberedPair());
    if (!pair || !pair.startable) {
      notify.error(t("noPair"));
      return;
    }
    markMutation.mutate(pair);
  };

  const selected = mark && pairs ? (pairs.find((p) => pairKey(p) === pairKey(mark)) ?? null) : null;
  const cfg = configQuery.data;
  const pairText = mark ? (selected ? pairLabel(selected) : pairLabel({ harness: mark.harness, runtime_slug: mark.runtime_slug })) : null;

  let line: string;
  let tone: string = C.textMuted;
  if (!mark) {
    line = cfg ? t("hint", { start: cfg.start, end: cfg.end }) : t("hintShort");
  } else if (started) {
    line = t("startedLine");
  } else if (mark.state === "queued") {
    line = t("queuedLine", { pair: pairText ?? "" });
  } else {
    line = `${t(`state.${mark.state}`)} · ${t(nightReasonKey(mark.reason))}`;
    tone = STATUS_TEXT.warning;
  }

  return (
    <section className="rounded-lg px-3 py-1.5" style={{ border: `1px solid ${C.border}` }} data-testid="night-toggle">
      <div className="flex items-center gap-2.5">
        <Moon size={14} aria-hidden style={{ color: on ? C.accent : C.textMuted }} className="shrink-0" />
        <div className="flex-1 min-w-0">
          <div className="text-xs font-medium" style={{ color: C.textPrimary }} id={`night-label-${taskId}`}>
            {t("runTonight")}
          </div>
          <p className="text-[11px] leading-snug" style={{ color: tone }} id={`night-line-${taskId}`} data-testid="night-line">
            {line}
          </p>
          {cfg && !cfg.enabled && on && (
            <p className="text-[11px] leading-snug" style={{ color: STATUS_TEXT.warning }} data-testid="night-off-warning">
              {t("offWarning")}
            </p>
          )}
        </div>
        <NightSwitch
          checked={on}
          onChange={toggle}
          label={t("runTonight")}
          describedBy={`night-line-${taskId}`}
          disabled={disabled || busy || started || markQuery.isLoading}
          testId="night-switch"
        />
      </div>
      {on && !started && canMark && pairs && (
        <div className="pt-1 pb-1.5">
          <HeadPairPicker
            pairs={pairs}
            selected={selected}
            defaultKey={pairsQuery.data?.default_pair ? pairKey(pairsQuery.data.default_pair) : null}
            onSelect={(p) => markMutation.mutate(p)}
            disabled={busy}
            showHints={false}
          />
        </div>
      )}
    </section>
  );
}
