"use client";

// The content of the test sheet (P3): rows pre-filled from GET /bench/plan,
// pickers for model / test / opponent inside the same sheet (one level
// deeper, a back arrow — no second dialog), notes before Start, and the
// footer with the time + money summary and the one Start button (K11).
import { useState, type ReactNode } from "react";
import { useRouter } from "next/navigation";
import { useLocale, useTranslations } from "next-intl";
import { keepPreviousData, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowLeft, Check, Clock, Cloud, Cpu, Loader2, Plus, X } from "lucide-react";
import { benchResultHref, type BenchTestParams } from "@/lib/benchLinks";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { benchW1Api, benchW1Keys, parseBenchBlocked, parseCostConfirmation, type PlanQuery } from "../api";
import type {
  BenchPlan,
  BenchTestCreate,
  Blocker,
  Contestant,
  CostConfirmation,
  DeviceState,
  Effort,
  Harness,
  PresetId,
  PresetView,
} from "../types";
import { clockTime, minuteRange, shortDate, usd } from "../live/format";
import { stringParams, useBenchWords } from "../live/words";
import { SheetBody, SheetFooter, SheetHeader, useDesktop } from "./SheetFrame";
import { Chip, ChipGroup, FieldRow, Note, TapRow } from "./parts";
import {
  DEFAULT_CAP_USD,
  blockersFor,
  buildPicks,
  canAddOpponent,
  defaultEffort,
  estimateFor,
  harnessOption,
  moneyFor,
  moneyFromPlan,
  orderedOptions,
  presetChoices,
  samePicks,
  selectionFromPlan,
  toggleEffort,
  toggleHarness,
  type MoneyView,
  type SheetSelection,
} from "./sheetModel";

type View = "main" | "preset" | "model" | "opponent";

const DEVICE_DOT: Record<DeviceState, string> = {
  free: STATUS.online,
  busy: STATUS.warning,
  asleep: STATUS.offline,
  offline: STATUS.offline,
};

export function TestSheet({ params, onClose }: { params: BenchTestParams; onClose: () => void }) {
  const t = useTranslations("benchSheet");
  const w = useBenchWords();
  const locale = useLocale();
  const router = useRouter();
  const qc = useQueryClient();
  const desktop = useDesktop();

  const [preset, setPreset] = useState<PresetId | null>(params.preset);
  const [runtimeId, setRuntimeId] = useState<string | null>(params.runtimeId);
  const [view, setView] = useState<View>("main");

  // "Nochmal" keeps its source; another preset tests the same subject fresh.
  const rerunOf = preset === null || preset === "rerun" ? params.rerunOf : null;
  const planQuery: PlanQuery = { runtimeId, preset, templateId: params.templateId, rerunOf };
  const plan = useQuery({
    queryKey: benchW1Keys.plan(planQuery),
    queryFn: () => benchW1Api.plan(planQuery),
    placeholderData: keepPreviousData,
  });

  // Contestants that came from the picker (an opponent the plan did not list).
  const [extra, setExtra] = useState<Contestant[]>([]);
  const [sel, setSel] = useState<SheetSelection | null>(null);
  const [seenPlan, setSeenPlan] = useState<BenchPlan | undefined>(undefined);
  // Client-side second money question (D4) and the server's 409 question.
  const [capAsked, setCapAsked] = useState(false);
  const [serverAsk, setServerAsk] = useState<CostConfirmation | null>(null);
  const [startError, setStartError] = useState<string | null>(null);
  const [seenPicksKey, setSeenPicksKey] = useState("");

  const data = plan.data;
  // A fresh plan (first load, preset, model) re-fills the rows; an opponent
  // chosen by hand survives a preset change.
  if (data !== seenPlan) {
    setSeenPlan(data);
    if (data) {
      const next = selectionFromPlan(data);
      if (next && !next.opponentId && sel?.opponentId && sel.opponentId !== next.subjectId && next.harnesses.length < 2) {
        next.opponentId = sel.opponentId;
        next.opponentEfforts = sel.opponentEfforts;
      }
      setSel(next);
    }
  }

  const pool: Contestant[] = [...(data?.contestants ?? [])];
  for (const c of extra) if (!pool.some((p) => p.runtime_id === c.runtime_id)) pool.push(c);
  const subject = sel ? pool.find((c) => c.runtime_id === sel.subjectId) : undefined;
  const opponent = sel?.opponentId ? pool.find((c) => c.runtime_id === sel.opponentId) : undefined;
  const picks = sel ? buildPicks(sel) : [];
  const usePlan = Boolean(data && samePicks(picks, data.picks));

  // Any change of the picks withdraws an earlier price answer.
  const picksKey = JSON.stringify(picks);
  if (picksKey !== seenPicksKey) {
    setSeenPicksKey(picksKey);
    setCapAsked(false);
    setServerAsk(null);
    setStartError(null);
  }

  const computed = moneyFor(picks, pool);
  const money: MoneyView = usePlan && data ? moneyFromPlan(data.money, computed.provider) : computed;
  const est = estimateFor(picks, pool);
  const hasA = picks.some((p) => harnessOption(pool.find((c) => c.runtime_id === p.runtime_id), p.harness)?.run_class !== "B");
  const hasB = picks.some((p) => harnessOption(pool.find((c) => c.runtime_id === p.runtime_id), p.harness)?.run_class === "B");
  // The plan's own estimate counts for its own picks; the ladder multiplies
  // the runs, so there the sheet's sum (levels × runs) is the honest one.
  if (usePlan && data && !hasB && !data.preset.multi_effort) est.a = data.estimate;
  const blockers = data
    ? blockersFor(data, picks, pool, usePlan).filter((b) => b.code !== "agent_busy" || hasB)
    : [];
  const blocked = blockers.some((b) => b.blocking);
  const ladder = Boolean(data?.preset.multi_effort);
  const apiPicked = Boolean(sel?.harnesses.includes("api"));
  const ladderShort = ladder && apiPicked && (sel?.efforts.length ?? 0) < 2;

  const create = useMutation({
    mutationFn: (body: BenchTestCreate) => benchW1Api.tests.create(body),
    onSuccess: (test) => {
      void qc.invalidateQueries({ queryKey: benchW1Keys.testsAll() });
      router.push(benchResultHref(test.id));
    },
    onError: (err) => {
      const ask = parseCostConfirmation(err);
      if (ask) {
        setServerAsk(ask);
        return;
      }
      // A blocker the server found after the plan was read (e.g. the
      // engine stopped meanwhile): its words, never the raw answer (K10).
      const blocked = parseBenchBlocked(err);
      if (blocked && blocked.length > 0) {
        setStartError(blocked.map((b) => w.word("blocker", b.code)).join(" · "));
        return;
      }
      const msg = err instanceof Error ? err.message : String(err);
      setStartError(msg.length > 160 ? `${msg.slice(0, 157)}…` : msg);
    },
  });

  const ready = Boolean(data && sel && subject && !plan.isPlaceholderData && picks.length > 0);
  const canStart = ready && !blocked && !ladderShort && !create.isPending;

  function start() {
    if (!data || !sel || !canStart) return;
    setStartError(null);
    const body: BenchTestCreate = {
      preset: data.preset.id,
      prompt_template_id: data.preset.prompt.template_id,
      picks,
      trigger: params.trigger,
    };
    if (params.rerunOf && (data.preset.id === "rerun" || rerunOf)) body.rerun_of = params.rerunOf;
    if (serverAsk) {
      body.cost_confirmed_usd = serverAsk.usd;
      if (serverAsk.step === 2) body.confirm_over_cap = true;
    } else if (money.needsConfirmation && money.usd != null) {
      // D4: the tap on "Start · $X — OK?" is the OK; over the cap ask again.
      if (money.secondConfirmation && !capAsked) {
        setCapAsked(true);
        return;
      }
      body.cost_confirmed_usd = money.usd;
      if (money.secondConfirmation) body.confirm_over_cap = true;
    }
    create.mutate(body);
  }

  function startLabel(): string {
    if (create.isPending) return t("starting");
    if (serverAsk) {
      return serverAsk.step === 2
        ? w.t("cost.confirmAgain", { cap: trimUsd(serverAsk.cap_usd), usd: usd(serverAsk.usd) })
        : w.t("cost.confirm", { usd: usd(serverAsk.usd) });
    }
    if (money.needsConfirmation && money.usd != null) {
      return capAsked
        ? w.t("cost.confirmAgain", { cap: trimUsd(DEFAULT_CAP_USD), usd: usd(money.usd) })
        : w.t("cost.confirm", { usd: usd(money.usd) });
    }
    if (money.kind === "subscription") return t("startSubscription");
    return t("start");
  }

  // ---- words for rows -------------------------------------------------------
  const presetWord = (id: PresetId) => t(`preset.${id}`);
  const where = (c: Contestant) => c.device?.name ?? c.provider ?? "";
  const modelSub = (c: Contestant) => {
    const loc = w.word("locality", c.locality);
    const place = where(c);
    return place ? t("modelSub", { where: place, locality: loc }) : loc;
  };
  const costWord = (c: Contestant) => {
    const m = c.money;
    if (m.cost_kind === "api_price" && m.usd != null) return w.t("cost.api_price", { usd: usd(m.usd) });
    if (m.cost_kind === "subscription") return w.t("cost.subscription");
    if (m.cost_kind === "unknown") return w.t("cost.unknown");
    return w.word("locality", "local");
  };

  function testSub(p: PresetView): string {
    if (sel && !apiPicked) return t("testSub.agent");
    const n = harnessOption(subject, "api")?.n_runs ?? p.n_runs;
    if (p.multi_effort) return t("testSub.ladder", { levels: sel?.efforts.length ?? 0, n });
    return t("testSub.runs", { n: p.n_runs || n });
  }

  // ---- header ----------------------------------------------------------------
  const title =
    view === "preset"
      ? t("changeTest")
      : view === "model"
        ? t("picker.model")
        : view === "opponent"
          ? t("picker.opponent")
          : t("title");

  const header = (
    <div className="flex items-center gap-2 min-h-touch">
      {view !== "main" && (
        <button
          type="button"
          onClick={() => setView("main")}
          aria-label={t("back")}
          className="flex items-center justify-center min-h-touch min-w-touch -ml-3 rounded-md cursor-pointer"
          style={{ color: C.textSecondary }}
        >
          <ArrowLeft size={20} aria-hidden />
        </button>
      )}
      <h2 className="flex-1 min-w-0 text-lg font-semibold" style={{ color: C.textPrimary }}>
        {title}
      </h2>
      <button
        type="button"
        onClick={onClose}
        aria-label={t("close")}
        className="flex items-center justify-center min-h-touch min-w-touch -mr-3 rounded-md cursor-pointer"
        style={{ color: C.textSecondary }}
      >
        <X size={20} aria-hidden />
      </button>
    </div>
  );

  // ---- picker views ------------------------------------------------------------
  if (view !== "main" && data) {
    return (
      <>
        <SheetHeader>{header}</SheetHeader>
        <SheetBody>
          {view === "preset" ? (
            <PresetList
              presets={presetChoices(data, Boolean(params.rerunOf))}
              current={data.preset.id}
              word={presetWord}
              hint={(p) =>
                t(`presetHint.${p.id}`, { n: p.n_runs || harnessOption(subject, "api")?.n_runs || 3 })
              }
              onPick={(id) => {
                setPreset(id);
                setView("main");
              }}
            />
          ) : (
            <ContestantPicker
              exclude={view === "opponent" && sel ? sel.subjectId : null}
              current={view === "model" ? (sel?.subjectId ?? null) : (sel?.opponentId ?? null)}
              sub={(c) =>
                // The group heading already says local/cloud: place + money only.
                [where(c), c.money.cost_kind === "local" ? null : costWord(c)].filter(Boolean).join(" · ")
              }
              onPick={(c) => {
                if (view === "model") {
                  setRuntimeId(c.runtime_id);
                } else if (sel) {
                  setExtra((e) => [...e.filter((x) => x.runtime_id !== c.runtime_id), c]);
                  setSel({ ...sel, opponentId: c.runtime_id, opponentEfforts: [defaultEffort(c)] });
                }
                setView("main");
              }}
            />
          )}
        </SheetBody>
      </>
    );
  }

  // ---- main view ---------------------------------------------------------------
  let body: ReactNode;
  if (plan.isError && !data) {
    body = (
      <div className="py-6">
        <Note tone="error" icon={<AlertTriangle size={16} />} title={t("loadError")} />
        <button
          type="button"
          onClick={() => void plan.refetch()}
          className="mt-3 min-h-touch text-sm font-medium cursor-pointer"
          style={{ color: C.textPrimary }}
        >
          {t("retry")}
        </button>
      </div>
    );
  } else if (!data) {
    body = (
      <div className="flex items-center gap-2 py-8 text-sm" style={{ color: C.textMuted }} role="status">
        <Loader2 size={16} className="animate-spin" aria-hidden />
        {t("loading")}
      </div>
    );
  } else {
    const ompOpt = harnessOption(subject, "omp");
    const sessionNote =
      ompOpt?.effort_note?.code === "omp_session_default"
        ? t("ompSession", { level: w.level((ompOpt.effort_note.params.level as Effort) ?? "default") })
        : null;
    const agentHarness = sel?.harnesses.find((h) => h !== "api");

    body = (
      <>
        {desktop && (
          <div className="pt-1 pb-2">
            <ChipGroup label={t("presets")}>
              {presetChoices(data, Boolean(params.rerunOf)).map((p) => (
                <Chip key={p.id} on={p.id === data.preset.id} onClick={() => setPreset(p.id)}>
                  {presetWord(p.id)}
                </Chip>
              ))}
            </ChipGroup>
          </div>
        )}

        <TapRow
          name={t("row.model")}
          main={subject?.display_name ?? t("picker.model")}
          sub={subject ? modelSub(subject) : undefined}
          onClick={() => setView("model")}
          ariaLabel={t("changeModel")}
          testId="bench-sheet-model"
        />
        <TapRow
          name={t("row.test")}
          main={t("testMain", { preset: presetWord(data.preset.id), prompt: data.preset.prompt.title })}
          sub={testSub(data.preset)}
          onClick={() => setView("preset")}
          ariaLabel={t("changeTest")}
          testId="bench-sheet-test"
        />

        {subject && sel && (
          <FieldRow
            name={t("row.harness")}
            sub={harnessSub(subject, t, w)}
          >
            <ChipGroup label={t("row.harness")}>
              {subject.harnesses.map((o) => (
                <Chip
                  key={o.harness}
                  on={sel.harnesses.includes(o.harness)}
                  disabled={!o.available}
                  onClick={() => setSel(toggleHarness(sel, o.harness))}
                >
                  {w.harness(o.harness)}
                </Chip>
              ))}
            </ChipGroup>
          </FieldRow>
        )}

        {subject && sel && apiPicked && (
          <FieldRow
            name={t("row.effort")}
            sub={
              <EffortSub
                parts={effortSubParts({
                  subject,
                  sel,
                  ladder,
                  agent: agentHarness ? (sessionNote ?? w.harness(agentHarness)) : null,
                  t,
                  apiWord: w.harness("api"),
                  stamp: shortDate(subject.effort.probed_at, locale),
                })}
                failed={subject.effort.status === "failed"}
                failedText={t("effortFailed")}
              />
            }
          >
            <ChipGroup label={t("row.effort")}>
              {orderedOptions(subject, ladder).map((o) => (
                <Chip
                  key={o.level}
                  on={sel.efforts.includes(o.level)}
                  check={ladder}
                  onClick={() => setSel(toggleEffort(sel, o.level, ladder))}
                >
                  {w.option(o, subject.effort.default_observed)}
                </Chip>
              ))}
            </ChipGroup>
          </FieldRow>
        )}

        {subject && sel && !apiPicked && (
          <FieldRow name={t("row.effort")} sub={t("effortDisplayOnly")}>
            <div className="text-base font-medium" style={{ color: C.textPrimary }}>
              {sessionNote ?? w.level("default")}
            </div>
          </FieldRow>
        )}

        {opponent && sel && (
          <FieldRow
            name={t("row.opponent")}
            sub={
              opponent.cloud_build_unknown
                ? [where(opponent), t("cloudBuildUnknown")].filter(Boolean).join(" · ")
                : modelSub(opponent)
            }
            action={
              <button
                type="button"
                onClick={() => setSel({ ...sel, opponentId: null })}
                aria-label={t("removeOpponent")}
                className="flex items-center justify-center min-h-touch min-w-touch -mr-3 rounded-md cursor-pointer shrink-0"
                style={{ color: C.textSecondary }}
              >
                <X size={18} aria-hidden />
              </button>
            }
          >
            <div className="text-base font-medium break-words" style={{ color: C.textPrimary }}>
              {opponent.display_name}
            </div>
          </FieldRow>
        )}

        {subject?.device && (
          <FieldRow name={t("row.device")}>
            <div className="flex items-center gap-2">
              <span
                className="w-2 h-2 rounded-full shrink-0"
                aria-hidden
                style={{ background: DEVICE_DOT[subject.device.state] }}
              />
              <span className="text-base font-medium" style={{ color: C.textPrimary }}>
                {[subject.device.name, subject.device.gpu_mode, t(`deviceState.${subject.device.state}`)]
                  .filter(Boolean)
                  .join(" · ")}
              </span>
            </div>
          </FieldRow>
        )}

        {hasA && hasB && (
          <Note tone="info" icon={<AlertTriangle size={16} />} testId="bench-sheet-classmix">
            {t.rich("classMix", {
              a: w.word("runClass", "A"),
              b: w.word("runClass", "B"),
              strong: (chunks) => (
                <strong className="font-medium" style={{ color: C.textPrimary }}>
                  {chunks}
                </strong>
              ),
            })}
          </Note>
        )}

        {subject && apiPicked && subject.effort.status === "failed" && (
          <Note tone="warn" icon={<AlertTriangle size={16} />}>
            {t("effortFailedNote")}
          </Note>
        )}

        {sel && subject && canAddOpponent(sel) && (
          <button
            type="button"
            onClick={() => setView("opponent")}
            className="mt-3 inline-flex items-center gap-2 min-h-touch text-base font-medium cursor-pointer"
            style={{ color: C.textPrimary }}
          >
            <Plus size={18} aria-hidden />
            {t("addOpponent")}
          </button>
        )}
      </>
    );
  }

  // ---- footer: summary + Start -----------------------------------------------
  const summary = data && sel ? summaryParts({ est, ladder, sel, picks, data, t, hasA }) : [];
  const moneyLine = data && sel ? moneyText(money, t) : null;

  return (
    <>
      <SheetHeader>{header}</SheetHeader>
      <SheetBody>{body}</SheetBody>
      <SheetFooter>
        {/* Blockers sit next to Start, never below the fold: a greyed Start
            always has its reason in view. */}
        {view === "main" &&
          blockers.map((b) => <BlockerNote key={b.code} blocker={b} locale={locale} />)}
        {summary.length > 0 && (
          <p className="flex items-start gap-2 text-sm tabular-nums" style={{ color: C.textSecondary }}>
            <Clock size={15} className="shrink-0 mt-1" aria-hidden style={{ color: C.textMuted }} />
            <Parts parts={summary} />
          </p>
        )}
        {moneyLine && (
          <p className="flex items-start gap-2 text-sm mt-1 tabular-nums" style={{ color: C.textSecondary }}>
            {money.kind === "local" ? (
              <Cpu size={15} className="shrink-0 mt-1" aria-hidden style={{ color: C.textMuted }} />
            ) : (
              <Cloud size={15} className="shrink-0 mt-1" aria-hidden style={{ color: C.textMuted }} />
            )}
            <Parts parts={moneyLine} />
          </p>
        )}
        {ladderShort && (
          <p className="text-sm mt-2" style={{ color: STATUS_TEXT.warning }}>
            {t("ladderMin")}
          </p>
        )}
        {startError && (
          <p role="alert" className="text-sm mt-2 break-words" style={{ color: STATUS_TEXT.error }}>
            {t("startError", { error: startError })}
          </p>
        )}
        <button
          type="button"
          data-testid="bench-sheet-start"
          onClick={start}
          disabled={!canStart}
          aria-live="polite"
          className="mt-3 w-full min-h-14 px-4 rounded-md text-base font-semibold cursor-pointer transition-colors disabled:cursor-not-allowed tabular-nums"
          style={{
            background: canStart ? C.accent : C.bgHover,
            color: canStart ? C.onAccent : C.textDim,
          }}
        >
          {startLabel()}
        </button>
      </SheetFooter>
    </>
  );
}

// ---------------------------------------------------------------------------

type T = ReturnType<typeof useTranslations<"benchSheet">>;

function trimUsd(n: number): string {
  return Number.isInteger(n) ? String(n) : usd(n);
}

function reasonWord(w: ReturnType<typeof useBenchWords>, code: string): string | null {
  for (const group of ["reason", "blocker"]) if (w.has(`${group}.${code}`)) return w.t(`${group}.${code}`);
  return null;
}

function harnessSub(subject: Contestant, t: T, w: ReturnType<typeof useBenchWords>): string | undefined {
  const off = subject.harnesses.filter((o) => !o.available);
  if (off.length)
    return off
      .map((o) => {
        // The server's reason in shared words ("the agent is paused"), else the plain note.
        const why = o.reason ? reasonWord(w, o.reason.code) : null;
        return why
          ? t("harnessUnavailableWhy", { harness: w.harness(o.harness), reason: why })
          : t("harnessUnavailable", { harness: w.harness(o.harness) });
      })
      .join(" · ");
  if (subject.locality === "cloud" && subject.harnesses.length === 1 && subject.harnesses[0].harness === "api") {
    return t("harnessCloudOnly");
  }
  return undefined;
}

function effortSubParts({
  subject,
  sel,
  ladder,
  agent,
  t,
  apiWord,
  stamp,
}: {
  subject: Contestant;
  sel: SheetSelection;
  ladder: boolean;
  agent: string | null;
  t: T;
  apiWord: string;
  stamp: string | null;
}): string[] {
  const parts: string[] = [];
  if (agent) parts.push(t("effortAppliesTo", { api: apiWord, session: agent }));
  if (ladder) parts.push(t("effortCount", { n: sel.efforts.length }));
  const status = subject.effort.status;
  if (status === "only_on_off") parts.push(t("effortOnlyOnOff"));
  if (status === "no_effect") parts.push(t("effortNoEffect"));
  if (status === "unchecked") parts.push(t("effortUnchecked"));
  if (status !== "failed" && status !== "unchecked" && stamp) parts.push(t("effortStamp", { date: stamp }));
  return parts;
}

function EffortSub({ parts, failed, failedText }: { parts: string[]; failed: boolean; failedText: string }) {
  return (
    <>
      {failed && <span style={{ color: STATUS_TEXT.warning }}>{failedText}</span>}
      {failed && parts.length > 0 && " · "}
      {parts.join(" · ")}
    </>
  );
}

function summaryParts({
  est,
  ladder,
  sel,
  picks,
  data,
  t,
  hasA,
}: {
  est: ReturnType<typeof estimateFor>;
  ladder: boolean;
  sel: SheetSelection;
  picks: ReturnType<typeof buildPicks>;
  data: BenchPlan;
  t: T;
  hasA: boolean;
}): string[] {
  const out: string[] = [];
  const a = est.a ? minuteRange(est.a.min_s, est.a.max_s) : null;
  const b = est.b ? minuteRange(est.b.min_s, est.b.max_s) : null;
  if (a && b) out.push(t("estimate.mix", { a, b }));
  else if (a) out.push(t("estimate.about", { range: a }));
  else if (b) out.push(t("estimate.agentSoFar", { range: b }), t("estimate.single"));
  if (ladder && sel.harnesses.includes("api") && sel.efforts.includes("max")) out.push(t("estimate.maxThinks"));
  const aRuntimes = new Set(picks.filter((p) => p.harness === "api").map((p) => p.runtime_id));
  if (aRuntimes.size > 1) out.push(t("estimate.contestants", { n: aRuntimes.size, runs: data.preset.n_runs }));
  if (hasA) out.push(t("estimate.video", { n: data.video_run_index }));
  return out;
}

/** D3/D4 in words: local = "lokal" + a small hypothetical API price;
 *  subscription = no extra cost (+ the comparison); real money stays on
 *  the Start button only (one fact once, K3). */
function moneyText(m: MoneyView, t: T): string[] | null {
  const compare = m.hypotheticalUsd != null ? t("money.compare", { usd: usd(m.hypotheticalUsd) }) : null;
  if (m.kind === "local") return [t("money.local"), compare].filter((x): x is string => Boolean(x));
  if (m.kind === "subscription") {
    const head = m.provider ? t("money.subscription", { provider: m.provider }) : t("money.subscriptionPlain");
    return [head, compare].filter((x): x is string => Boolean(x));
  }
  if (m.kind === "unknown") return [t("money.unknown")];
  return null;
}

/** "a · b · c" where each part keeps its words together: a line breaks
 *  between parts, never inside "Video = Lauf 1". */
function Parts({ parts }: { parts: string[] }) {
  return (
    <span>
      {parts.map((p, i) => (
        <span key={i}>
          {i > 0 && " · "}
          <span className={p.length <= 28 ? "whitespace-nowrap" : undefined}>{p}</span>
        </span>
      ))}
    </span>
  );
}

function BlockerNote({ blocker, locale }: { blocker: Blocker; locale: string }) {
  const t = useTranslations("benchSheet");
  const w = useBenchWords();
  const head = w.word("blocker", blocker.code);
  let text: string;
  if (blocker.blocking) text = t("blockerStops");
  else if (blocker.code === "model_fresh") {
    const time = clockTime(blocker.until, locale);
    text = time ? t("blockerWaits.model_fresh", { time }) : t("blockerWaits.model_fresh_plain");
  } else if (blocker.code === "box_busy" || blocker.code === "agent_busy") {
    text = t(`blockerWaits.${blocker.code}`, stringParams(blocker.params));
  } else text = t("blockerWaits.other");
  return (
    <Note
      tone={blocker.blocking ? "error" : "warn"}
      icon={blocker.blocking ? <AlertTriangle size={16} /> : <Clock size={16} />}
      title={head}
      testId={`bench-sheet-blocker-${blocker.code}`}
      className="mb-3"
    >
      {text}
    </Note>
  );
}

function PresetList({
  presets,
  current,
  word,
  hint,
  onPick,
}: {
  presets: PresetView[];
  current: PresetId;
  word: (id: PresetId) => string;
  hint: (p: PresetView) => string;
  onPick: (id: PresetId) => void;
}) {
  return (
    <ul className="flex flex-col py-2">
      {presets.map((p) => (
        <li key={p.id}>
          <PickRow selected={p.id === current} main={word(p.id)} sub={hint(p)} onClick={() => onPick(p.id)} />
        </li>
      ))}
    </ul>
  );
}

function PickRow({
  selected,
  main,
  sub,
  extra,
  disabled,
  onClick,
}: {
  selected: boolean;
  main: string;
  sub?: string;
  extra?: ReactNode;
  disabled?: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      aria-current={selected ? "true" : undefined}
      className="w-full flex items-center gap-3 min-h-14 py-3 text-left cursor-pointer disabled:cursor-not-allowed"
      style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
    >
      <span className="flex-1 min-w-0">
        <span
          className="block text-base font-medium break-words"
          style={{ color: disabled ? C.textDim : C.textPrimary }}
        >
          {main}
        </span>
        {sub && (
          <span className="block text-sm mt-1" style={{ color: C.textMuted }}>
            {sub}
          </span>
        )}
        {extra}
      </span>
      {selected && <Check size={18} aria-hidden style={{ color: C.accent }} className="shrink-0" />}
    </button>
  );
}

function ContestantPicker({
  exclude,
  current,
  sub,
  onPick,
}: {
  exclude: string | null;
  current: string | null;
  sub: (c: Contestant) => string;
  onPick: (c: Contestant) => void;
}) {
  const t = useTranslations("benchSheet");
  const w = useBenchWords();
  const list = useQuery({ queryKey: benchW1Keys.contestants(), queryFn: () => benchW1Api.contestants() });

  if (list.isError) {
    return (
      <div className="py-6">
        <Note tone="error" icon={<AlertTriangle size={16} />} title={t("picker.error")} />
        <button
          type="button"
          onClick={() => void list.refetch()}
          className="mt-3 min-h-touch text-sm font-medium cursor-pointer"
          style={{ color: C.textPrimary }}
        >
          {t("retry")}
        </button>
      </div>
    );
  }
  if (!list.data) {
    return (
      <div className="flex items-center gap-2 py-8 text-sm" style={{ color: C.textMuted }} role="status">
        <Loader2 size={16} className="animate-spin" aria-hidden />
        {t("picker.loading")}
      </div>
    );
  }
  const groups: [string, Contestant[]][] = [
    [t("picker.local"), list.data.local.filter((c) => c.runtime_id !== exclude)],
    [t("picker.cloud"), list.data.cloud.filter((c) => c.runtime_id !== exclude)],
  ];
  if (groups.every(([, cs]) => cs.length === 0)) {
    return (
      <p className="py-8 text-sm" style={{ color: C.textMuted }}>
        {t("picker.empty")}
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-4 py-2">
      {groups
        .filter(([, cs]) => cs.length > 0)
        .map(([label, cs]) => (
          <section key={label}>
            <h3 className="text-sm pt-2" style={{ color: C.textMuted }}>
              {label}
            </h3>
            <ul>
              {cs.map((c) => {
                const api = harnessOption(c, "api");
                const hard = c.blockers.filter((b) => b.blocking);
                const unavailable = exclude !== null && !(api?.available ?? false);
                return (
                  <li key={c.runtime_id}>
                    <PickRow
                      selected={c.runtime_id === current}
                      main={c.display_name}
                      sub={sub(c)}
                      disabled={unavailable}
                      extra={
                        hard.length > 0 ? (
                          <span className="block text-sm mt-1" style={{ color: STATUS_TEXT.warning }}>
                            {hard.map((b) => w.word("blocker", b.code)).join(" · ")}
                          </span>
                        ) : undefined
                      }
                      onClick={() => onPick(c)}
                    />
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
    </div>
  );
}
