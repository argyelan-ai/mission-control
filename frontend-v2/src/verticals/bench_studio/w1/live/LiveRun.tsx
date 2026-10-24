"use client";

// Bench W1 live run (P4) — package B. Rendered by the result page (package
// C) while the test is waiting or running; C polls the detail every
// LIVE_POLL_MS and passes the fresh record in (frozen signature).
//
// One state line (K12: one line, at most one "·"), then one card per
// contestant: class A ("Modell pur") lists run i/n with time, tokens so far
// and decode tok/s; class B ("Agent baut") runs after class A — never at the
// same time on one box (§5.1) — and shows why it waits, never a tempo.
// Stop sits at the end, quiet, and asks once more before it acts.
import { useEffect, useRef, useState, type ReactNode } from "react";
import { useLocale, useTranslations } from "next-intl";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Check, Clock, Loader2, Square, X } from "lucide-react";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { benchW1Api, benchW1Keys } from "../api";
import type { BenchTestDetail, ContestantResult, Reason, ResultBlock, RunView } from "../types";
import { clockTime, runDuration, stopwatch, tokPerSec, tokenCount } from "./format";
import { stringParams, useBenchWords } from "./words";

export interface LiveRunProps {
  test: BenchTestDetail;
  /** Called after a successful stop with the updated record. */
  onStopped?: (test: BenchTestDetail) => void;
}

const STATE_DOT: Record<BenchTestDetail["state"], string> = {
  waiting: STATUS.warning,
  running: STATUS.busy,
  done: STATUS.online,
  done_missing: STATUS.warning,
  aborted: STATUS.offline,
};

/** A clock that ticks once a second while something runs, so the running
 *  time moves between two polls instead of jumping by 2 s. */
function useNow(active: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return;
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, [active]);
  return now;
}

function findRunning(blocks: ResultBlock[]) {
  for (const block of blocks) {
    for (const c of block.contestants) {
      const run = c.runs.find((r) => r.state === "running");
      if (run) return { block, contestant: c, run };
    }
  }
  return null;
}

/** "time" / "until" params that are ISO timestamps read as a clock time. */
function waitParams(r: Reason, locale: string): Record<string, string | number> {
  const out = stringParams(r.params);
  for (const k of ["time", "until", "not_before"]) {
    const v = out[k];
    if (typeof v === "string" && v.includes("T")) out[k] = clockTime(v, locale) ?? v;
  }
  return out;
}

export function LiveRun({ test, onStopped }: LiveRunProps) {
  const t = useTranslations("benchSheet.live");
  const w = useBenchWords();
  const locale = useLocale();
  const qc = useQueryClient();

  // When did this record arrive? Running clocks add the time since then.
  const [stamp, setStamp] = useState(() => ({ test, at: Date.now() }));
  if (stamp.test !== test) setStamp({ test, at: Date.now() });
  const running = findRunning(test.blocks);
  const now = useNow(Boolean(running));
  const delta = Math.max(0, now - stamp.at);

  const failures = test.blocks.flatMap((b) => b.contestants.flatMap((c) => c.runs)).filter((r) => r.state === "failed").length;
  const hasAgentAfter = test.blocks.some((b) => b.starts_after !== null && b.contestants.some((c) => c.runs.some((r) => r.state === "waiting")));

  // ---- the one state line ------------------------------------------------------
  const stateWord = w.word("state", test.state);
  let detail: string | null = null;
  let hint: string | null = null;
  if (test.state === "running" && running) {
    if (running.block.run_class === "B") detail = t("agentRunning");
    else {
      const n = running.contestant.runs.length;
      const i = running.run.repeat_idx;
      detail = failures > 0 ? t("runOfFailures", { i, n, failed: failures }) : w.t("runOf", { i, n });
    }
    if (failures > 0) hint = t("failuresCount");
    else if (running.block.run_class !== "B" && hasAgentAfter) hint = t("nextAgent");
  } else if (test.state === "waiting") {
    const reason =
      test.state_reason ??
      test.blocks.flatMap((b) => b.contestants.flatMap((c) => c.runs)).find((r) => r.wait)?.wait ??
      null;
    if (reason) {
      detail = w.wait(reason, waitParams(reason, locale));
      if (reason.code === "box_busy") hint = t("busyHint");
    }
  } else if (test.state === "aborted" && test.state_reason) {
    detail = w.word("abort", test.state_reason.code);
  }

  const live = test.state === "waiting" || test.state === "running";
  const multiName = new Set(test.blocks.flatMap((b) => b.contestants.map((c) => c.display_name))).size > 1;

  return (
    <section
      data-testid="bench-live-run"
      data-test-id={test.id}
      data-region="bench-live-run"
      aria-label={t("region")}
      className="flex flex-col"
    >
      <p
        data-testid="bench-live-status"
        role="status"
        className="flex items-center gap-2 text-base tabular-nums"
        style={{ color: C.textPrimary }}
      >
        <span className="w-2 h-2 rounded-full shrink-0" aria-hidden style={{ background: STATE_DOT[test.state] }} />
        <span>{detail ? t("status", { state: stateWord, detail }) : stateWord}</span>
      </p>
      {hint && (
        <p className="text-sm mt-1" style={{ color: C.textMuted }}>
          {hint}
        </p>
      )}

      <div className="flex flex-col mt-4">
        {test.blocks.map((block, bi) => (
          <div key={block.run_class + bi} className={bi > 0 ? "mt-6" : undefined}>
            {bi > 0 && (
              <div className="flex items-center gap-3 mb-3">
                <span className="text-sm shrink-0" style={{ color: C.textSecondary }}>
                  {t("then", { cls: w.word("runClass", block.run_class) })}
                </span>
                <span className="flex-1 h-px" aria-hidden style={{ background: C.border }} />
              </div>
            )}
            <div className="flex flex-col gap-3">
              {block.contestants.map((c) =>
                block.run_class === "B" ? (
                  <AgentCard key={c.key} c={c} delta={delta} aborted={test.state === "aborted"} multiName={multiName} />
                ) : (
                  <ApiCard
                    key={c.key}
                    c={c}
                    delta={delta}
                    aborted={test.state === "aborted"}
                    multiName={multiName}
                    classWord={w.word("runClass", block.run_class)}
                  />
                ),
              )}
            </div>
          </div>
        ))}
      </div>

      {live && <LiveFooter test={test} onStopped={onStopped} qcSet={(r) => qc.setQueryData(benchW1Keys.test(test.id), r)} />}
    </section>
  );
}

// ---------------------------------------------------------------------------

function CardHead({ title, sub, tag }: { title: string; sub?: string | null; tag: string }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <div className="min-w-0">
        <div className="text-base font-medium break-words" style={{ color: C.textPrimary }}>
          {title}
        </div>
        {sub && (
          <div className="text-sm mt-1" style={{ color: C.textMuted }}>
            {sub}
          </div>
        )}
      </div>
      <span
        className="shrink-0 text-xs px-2 py-1 rounded-sm"
        style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
      >
        {tag}
      </span>
    </div>
  );
}

const cardStyle = { background: C.bgSurface, border: `1px solid ${C.border}` };

function ApiCard({
  c,
  delta,
  aborted,
  multiName,
  classWord,
}: {
  c: ContestantResult;
  delta: number;
  aborted: boolean;
  multiName: boolean;
  classWord: string;
}) {
  const w = useBenchWords();
  const harnessLevel = [c.harness ? w.harness(c.harness) : null, w.level(c.effort.requested)].filter(Boolean).join(" · ");
  return (
    <article data-testid="bench-live-card" className="rounded-lg p-4" style={cardStyle}>
      <CardHead title={multiName ? c.display_name : harnessLevel} sub={multiName ? harnessLevel : null} tag={classWord} />
      <ul className="mt-2">
        {c.runs.map((r, i) => (
          <RunLine key={r.entry_id} r={r} delta={delta} aborted={aborted} last={i === c.runs.length - 1} />
        ))}
      </ul>
    </article>
  );
}

function RunLine({ r, delta, aborted, last }: { r: RunView; delta: number; aborted: boolean; last: boolean }) {
  const t = useTranslations("benchSheet.live");
  const w = useBenchWords();
  const locale = useLocale();

  let icon = <Clock size={16} aria-hidden style={{ color: C.textMuted }} />;
  let values: ReactNode;
  let quiet = false;
  if (r.state === "done") {
    icon = <Check size={16} aria-hidden style={{ color: STATUS_TEXT.online }} />;
    values = [runDuration(r.elapsed_ms, locale), r.tokens_out != null ? t("tokens", { n: tokenCount(r.tokens_out, locale) }) : null]
      .filter(Boolean)
      .join(" · ");
  } else if (r.state === "running") {
    icon = <Loader2 size={16} aria-hidden className="animate-spin" style={{ color: C.info }} />;
    const parts = [
      stopwatch((r.elapsed_ms ?? 0) + delta),
      r.tokens_out != null ? t("tokens", { n: tokenCount(r.tokens_out, locale) }) : null,
      r.decode_tok_s != null ? t("decode", { n: tokPerSec(r.decode_tok_s, locale) }) : null,
      r.phase === "recording" || r.phase === "composing" ? t(`phase.${r.phase}`) : null,
    ];
    values = parts.filter(Boolean).join(" · ");
  } else if (r.state === "failed") {
    icon = <X size={16} aria-hidden style={{ color: STATUS_TEXT.error }} />;
    values = r.truncated ? (
      <>
        <span style={{ color: STATUS_TEXT.error }}>{t("truncated")}</span>
        {" · "}
        {t("maxTokensHit")}
      </>
    ) : (
      <span style={{ color: STATUS_TEXT.error }}>
        {r.error ? w.word("reason", r.error.code) : t("failed")}
      </span>
    );
  } else {
    quiet = true;
    values = aborted ? t("notStarted") : r.wait ? w.wait(r.wait, stringParams(r.wait.params)) : t("waits");
    if (aborted) icon = <Square size={14} aria-hidden style={{ color: C.textMuted }} />;
  }

  return (
    <li
      data-testid="bench-live-runline"
      className="flex items-center gap-3 min-h-11 py-2"
      style={last ? undefined : { borderBottom: `1px solid ${C.borderSubtle}` }}
    >
      <span className="w-14 shrink-0 text-sm" style={{ color: C.textMuted }}>
        {t("run", { i: r.repeat_idx })}
      </span>
      <span className="w-5 shrink-0 flex justify-center">{icon}</span>
      <span className="flex-1 min-w-0 text-sm tabular-nums" style={{ color: quiet ? C.textMuted : C.textPrimary }}>
        {values}
      </span>
    </li>
  );
}

function AgentCard({
  c,
  delta,
  aborted,
  multiName,
}: {
  c: ContestantResult;
  delta: number;
  aborted: boolean;
  multiName: boolean;
}) {
  const t = useTranslations("benchSheet.live");
  const w = useBenchWords();
  const locale = useLocale();
  const run = c.runs[0];
  const harness = c.harness ? w.harness(c.harness) : c.display_name;

  const rows: { k: string; v: ReactNode; sub?: string }[] = [];
  if (run) {
    if (run.state === "waiting") {
      const text = aborted ? t("notStarted") : run.wait ? w.wait(run.wait, stringParams(run.wait.params)) : t("waits");
      rows.push({
        k: t("stateRow"),
        v: (
          <span className="inline-flex items-center gap-2">
            <Clock size={15} aria-hidden style={{ color: C.textMuted }} />
            <span>{text}</span>
          </span>
        ),
      });
      if (run.wait?.code === "after_class_a" && !aborted) {
        rows.push({ k: t("whyRow"), v: t("whyQuiet"), sub: t("whyQuietSub") });
      }
    } else if (run.state === "running") {
      rows.push({
        k: t("stateRow"),
        v: (
          <span className="inline-flex items-center gap-2">
            <Loader2 size={15} aria-hidden className="animate-spin" style={{ color: C.info }} />
            <span>{t(`phase.${run.phase}`)}</span>
          </span>
        ),
      });
      rows.push({ k: t("timeRow"), v: stopwatch((run.elapsed_ms ?? 0) + delta) });
    } else {
      rows.push({
        k: t("stateRow"),
        v: run.state === "done" ? t("phase.done") : <span style={{ color: STATUS_TEXT.error }}>{t("failed")}</span>,
      });
      const d = runDuration(run.elapsed_ms, locale);
      if (d) rows.push({ k: t("timeRow"), v: d });
    }
    if (run.tokens_out != null) rows.push({ k: t("tokensRow"), v: tokenCount(run.tokens_out, locale) });
  }

  return (
    <article data-testid="bench-live-card" className="rounded-lg p-4" style={cardStyle}>
      <CardHead title={multiName ? c.display_name : harness} sub={multiName ? harness : null} tag={t("singleRun")} />
      <dl className="mt-2">
        {rows.map((row, i) => (
          <div
            key={row.k}
            className="flex items-start justify-between gap-4 min-h-11 py-2"
            style={i === rows.length - 1 ? undefined : { borderBottom: `1px solid ${C.borderSubtle}` }}
          >
            <dt className="text-sm shrink-0" style={{ color: C.textMuted }}>
              {row.k}
            </dt>
            <dd className="text-sm text-right tabular-nums min-w-0" style={{ color: C.textPrimary }}>
              {row.v}
              {row.sub && (
                <span className="block text-sm mt-1" style={{ color: C.textMuted }}>
                  {row.sub}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>
    </article>
  );
}

function LiveFooter({
  test,
  onStopped,
  qcSet,
}: {
  test: BenchTestDetail;
  onStopped?: (t: BenchTestDetail) => void;
  qcSet: (t: BenchTestDetail) => void;
}) {
  const t = useTranslations("benchSheet.live");
  const [armed, setArmed] = useState(false);
  const timer = useRef<number | null>(null);
  const stop = useMutation({
    mutationFn: () => benchW1Api.tests.stop(test.id),
    onSuccess: (res) => {
      qcSet(res);
      onStopped?.(res);
    },
  });

  useEffect(() => () => {
    if (timer.current) window.clearTimeout(timer.current);
  }, []);

  const canStop = test.allowed.includes("stop");

  function onStop() {
    if (!armed) {
      setArmed(true);
      // A second tap within 4 s confirms; otherwise it calms down again.
      timer.current = window.setTimeout(() => setArmed(false), 4000);
      return;
    }
    if (timer.current) window.clearTimeout(timer.current);
    setArmed(false);
    stop.mutate();
  }

  return (
    <div className="flex flex-col mt-6">
      <div className="flex items-center justify-between gap-3">
        <span className="text-sm" style={{ color: C.textMuted }}>
          {t("videoAfter")}
        </span>
        {canStop && (
          <button
            type="button"
            onClick={onStop}
            disabled={stop.isPending}
            className="shrink-0 inline-flex items-center gap-2 min-h-touch px-2 -mr-2 rounded-md text-sm font-medium cursor-pointer disabled:cursor-not-allowed"
            style={{ color: STATUS_TEXT.error }}
          >
            {stop.isPending ? <Loader2 size={15} className="animate-spin" aria-hidden /> : <Square size={14} aria-hidden />}
            {stop.isPending ? t("stopping") : armed ? t("stopConfirm") : t("stop")}
          </button>
        )}
      </div>
      {stop.isError && (
        <p role="alert" className="text-sm mt-2" style={{ color: STATUS_TEXT.error }}>
          {t("stopError")}
        </p>
      )}
    </div>
  );
}
