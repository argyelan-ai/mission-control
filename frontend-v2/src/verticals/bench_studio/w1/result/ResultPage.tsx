"use client";

// Bench W1 result page `/bench/r/<id>` (package C, concept P5).
//
// K12 order: context (‹ Benchmark · link · ⋯) → title → state sentence
// (gate light) → verdict → next step (the PRIVATE truncation warning, then
// ONE primary: X-Kit, or "Zahlen nachladen" on a red gate, or "Nochmal") →
// content (video, numbers per class block) → properties (receipt). While the
// test waits or runs the page polls the record (LIVE_POLL_MS) and shows the
// live run (package B) instead of video and numbers.
//
// Phone: one column, content to the edge (K8). Desktop ≥ 1024 px: content
// left, a sticky right column with the actions, the gate checks and the
// receipt; ≥ 1536 px the test list (package D) joins on the left. One DOM
// for both: the two column wrappers are `display: contents` on the phone, so
// their parts interleave by `order`.
//
// Signature is frozen (ResultPageProps).
import { Suspense, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  Archive,
  Check,
  ChevronLeft,
  Clapperboard,
  ExternalLink,
  Film,
  Link2,
  Loader2,
  RefreshCw,
  Send,
  Share,
  Trash2,
} from "lucide-react";
import AppShell from "@/components/layout/AppShell";
import { FilePreview } from "@/components/task/FilePreview";
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { NotFoundState } from "@/components/shared/NotFoundState";
import { OverflowMenu, type OverflowAction } from "@/components/shared/OverflowMenu";
import { api, request } from "@/lib/api";
import { BENCH_LIST_HREF, benchResultHref, benchTestHref } from "@/lib/benchLinks";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
// Legacy client, used ONLY for what W1 has no route for yet: the files API
// subpath of the video, rerender/recompose/archive/delete and the entry page
// view token (CONTRACT §2 — mapped from BenchTestDetail.allowed).
import { benchApi } from "@/verticals/bench_studio/api";
import { LIVE_POLL_MS, benchW1Api, benchW1Keys, parseCostConfirmation } from "../api";
import { XKitSheet } from "../kit/XKitSheet";
import { TestList } from "../list/TestList";
import { LiveRun } from "../live/LiveRun";
import type { BenchTestDetail, ContestantResult, OverlayAction } from "../types";
import { apiErrorText, apiStatus } from "./errors";
import { formatClockTime, formatDay, formatTime, formatTokens, statText } from "./format";
import { GateChecks, GateLine } from "./GateView";
import { isLive, menuIds, openablePages, primaryOf, safeOverlayActions } from "./plan";
import { ReceiptView } from "./ReceiptView";
import { NumbersList, NumbersTable } from "./ResultNumbers";
import { TruncationWarning } from "./TruncationWarning";
import { useTruncationAck } from "./truncationAck";
import { useBenchWords, type BenchWords } from "./words";

export interface ResultPageProps {
  testId: string;
}

export default function ResultPage({ testId }: ResultPageProps) {
  return (
    <AppShell fullHeight mobileHideAppBar>
      <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden pt-safe md:pt-0">
        <ResultView testId={testId} />
      </div>
    </AppShell>
  );
}

function allContestants(test: BenchTestDetail): ContestantResult[] {
  return test.blocks.flatMap((b) => b.contestants);
}

function runsOf(c: ContestantResult | undefined): number {
  if (!c) return 0;
  return c.receipt?.n_runs ?? (c.summary.time.n || c.runs.length);
}

/** The verdict sentence. Single contestant: generated from `verdict`, the
 *  served model id stands here and nowhere else on the page (K3). Several
 *  contestants or classes: no winner, the names stand in the numbers. */
function VerdictLine({ test, words }: { test: BenchTestDetail; words: BenchWords }) {
  const t = words.tr;
  const contestants = allContestants(test);
  let content: React.ReactNode = null;
  if (test.blocks.length > 1) {
    const [a, b] = test.blocks;
    content = t("verdict.twoClasses", {
      a: words.runClass(a.run_class),
      runsA: t("verdict.runs", { n: runsOf(a.contestants[0]) }),
      b: words.runClass(b.run_class),
      runsB: t("verdict.runs", { n: runsOf(b.contestants[0]) }),
    });
  } else if (contestants.length > 1) {
    content = t("verdict.multi", {
      n: contestants.length,
      cls: words.runClass(test.blocks[0].run_class),
      runs: t("verdict.runs", { n: runsOf(contestants[0]) }),
    });
  } else if (test.verdict) {
    const v = test.verdict;
    const c = contestants[0];
    const kind = c?.summary.time_kind ?? (v.run_class === "A" ? "gen_ms" : "active_ms");
    const parts: string[] = [];
    const time = statText(v.time, (x) => formatTime(x, kind));
    const timeLabel = t(kind === "gen_ms" ? "verdict.timeGen" : "verdict.timeActive");
    if (time) {
      const value = time.min && time.max ? words.ts("range", { min: time.min, max: time.max }) : time.median;
      parts.push(t("verdict.time", { value, label: timeLabel }));
    } else {
      parts.push(t("verdict.missing", { field: timeLabel, reason: words.reason(v.time.reason) ?? t("reason.unknown") }));
    }
    const tokens = statText(v.tokens_out, (x) => formatTokens(x, words.locale));
    if (tokens) parts.push(t("verdict.tokens", { value: tokens.median }));
    else
      parts.push(
        t("verdict.missing", {
          field: t("field.tokens_out"),
          reason: words.reason(v.tokens_out.reason) ?? t("reason.unknown"),
        }),
      );
    content = (
      <>
        {t("verdict.head", { model: v.served_model_id, runs: t("verdict.runs", { n: v.n_runs }) })}{" "}
        {parts.join(" · ")}
        {v.page_check === "loads" && (
          <>
            {" · "}
            {t("verdict.pageLoads")}
            <Check size={14} aria-hidden className="inline ml-1 align-baseline" style={{ color: C.online }} />
          </>
        )}
        {v.page_check === "blank" && <>{` · ${t("verdict.pageBlank")}`}</>}
      </>
    );
  }
  if (!content) return null;
  return (
    <p data-testid="result-verdict" className="text-base" style={{ color: C.textPrimary }}>
      {content}
    </p>
  );
}

function StateLine({ test, words }: { test: BenchTestDetail; words: BenchWords }) {
  const state = words.ts(`state.${test.state}`);
  const reason = words.reason(test.state_reason);
  const dot = test.state === "aborted" ? C.textMuted : C.info;
  return (
    <div className="flex items-center gap-2" data-testid="result-state">
      <span aria-hidden className="inline-block w-2 h-2 rounded-full shrink-0" style={{ backgroundColor: dot }} />
      <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>
        {reason ? words.ts("stateWithReason", { state, reason }) : state}
      </span>
    </div>
  );
}

function VideoBlock({ test, words }: { test: BenchTestDetail; words: BenchWords }) {
  const t = words.tr;
  if (!test.video) {
    return (
      <div
        className="w-full min-h-24 rounded-lg flex items-center justify-center px-4 py-6 text-center"
        style={{ backgroundColor: C.bgDeep, border: `1px dashed ${C.borderActive}` }}
      >
        <span className="flex flex-col items-center gap-2 text-sm" style={{ color: C.textMuted }}>
          <Film size={16} aria-hidden />
          {t(test.state === "aborted" ? "video.noneAborted" : "video.none")}
        </span>
      </div>
    );
  }
  const url = api.files.contentUrl("shared-deliverables", benchApi.sharedSubpath(test.video.path));
  const duration = test.video.duration_s;
  return (
    <figure className="flex flex-col gap-2">
      <FilePreview fileUrl={url} path={test.video.path} showDownload={false} />
      <figcaption className="text-xs" style={{ color: C.textMuted }}>
        {duration !== null
          ? t("video.caption", { i: test.video.run_index, d: words.tr("video.seconds", { s: duration }) })
          : t("video.captionShort", { i: test.video.run_index })}
      </figcaption>
    </figure>
  );
}

function AbortedFacts({ test, words }: { test: BenchTestDetail; words: BenchWords }) {
  const t = words.tr;
  const rows: { key: string; label: string; value: string; sub?: string | null }[] = [
    {
      key: "started",
      label: t("aborted.started"),
      value: `${formatDay(test.created_at, words.locale)} ${formatClockTime(test.created_at, words.locale)}`,
    },
    ...allContestants(test).map((c) => ({
      key: c.key,
      label: t("aborted.contestant"),
      value: c.display_name,
      sub: words.harness(c.harness),
    })),
  ];
  return (
    <div className="flex flex-col gap-4">
      <dl className="flex flex-col">
        {rows.map((r) => (
          <div
            key={r.key}
            className="flex items-start justify-between gap-4 py-3"
            style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
          >
            <dt className="text-sm" style={{ color: C.textMuted }}>
              {r.label}
            </dt>
            <dd className="flex flex-col items-end text-right">
              <span className="text-sm tabular-nums" style={{ color: C.textPrimary }}>
                {r.value}
              </span>
              {r.sub && (
                <span className="text-xs" style={{ color: C.textMuted }}>
                  {r.sub}
                </span>
              )}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-sm" style={{ color: C.textMuted }}>
        {t("aborted.tip")}
      </p>
    </div>
  );
}

const PRIMARY_BTN =
  "flex items-center justify-center gap-2 min-h-11 px-4 rounded-md text-sm font-medium cursor-pointer transition-colors disabled:cursor-wait";

export function ResultView({ testId }: ResultPageProps) {
  const words = useBenchWords();
  const t = words.tr;
  const router = useRouter();
  const qc = useQueryClient();
  const key = benchW1Keys.test(testId);
  const [kitOpen, setKitOpen] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [confirmOverlay, setConfirmOverlay] = useState<OverlayAction | null>(null);
  const [truncationAcked, ackTruncation] = useTruncationAck(testId);

  const query = useQuery({
    queryKey: key,
    queryFn: () => benchW1Api.tests.get(testId),
    enabled: Boolean(testId),
    refetchInterval: (q) => {
      const s = q.state.data?.state;
      return s === "waiting" || s === "running" ? LIVE_POLL_MS : false;
    },
  });

  const refresh = () => qc.invalidateQueries({ queryKey: key });

  const harvest = useMutation({
    mutationFn: () => benchW1Api.tests.harvest(testId),
    onSuccess: (data) => {
      qc.setQueryData(key, data);
      qc.invalidateQueries({ queryKey: benchW1Keys.kit(testId) });
      notify.success(t("notify.harvestStarted"));
    },
    onError: (err) => notify.error(apiErrorText(err, t("notify.harvestFailed"))),
  });

  const rerunLarger = useMutation({
    mutationFn: () => benchW1Api.tests.rerun(testId, "larger"),
    onSuccess: (next) => {
      qc.invalidateQueries({ queryKey: benchW1Keys.testsAll() });
      router.push(benchResultHref(next.id));
    },
    onError: (err) => {
      // A paid model: the money must be confirmed in the test sheet (D4).
      if (parseCostConfirmation(err)) {
        notify.info(t("notify.rerunNeedsCost"));
        router.push(benchTestHref({ rerunOf: testId, trigger: "rerun" }));
        return;
      }
      notify.error(apiErrorText(err, t("notify.rerunFailed")));
    },
  });

  const legacy = useMutation({
    mutationFn: async (id: "rerender" | "recompose" | "archive") => {
      if (id === "rerender") await benchApi.challenges.rerender(testId);
      else if (id === "recompose") await benchApi.challenges.recompose(testId);
      else await benchApi.challenges.archive(testId);
      return id;
    },
    onSuccess: (id) => {
      notify.success(t(`notify.${id}`));
      qc.invalidateQueries({ queryKey: benchW1Keys.testsAll() });
      if (id === "archive") router.push(BENCH_LIST_HREF);
      else refresh();
    },
    onError: (err) => notify.error(apiErrorText(err, t("notify.actionFailed"))),
  });

  const remove = useMutation({
    mutationFn: () => benchApi.challenges.remove(testId),
    onSuccess: () => {
      setConfirmDelete(false);
      notify.success(t("notify.deleted"));
      qc.invalidateQueries({ queryKey: benchW1Keys.testsAll() });
      router.push(BENCH_LIST_HREF);
    },
    onError: (err) => notify.error(apiErrorText(err, t("notify.actionFailed"))),
  });

  const overlay = useMutation({
    mutationFn: (a: OverlayAction) => request(a.endpoint, { method: a.method }),
    onSuccess: async (_d, a) => {
      setConfirmOverlay(null);
      notify.success(t("notify.overlayStarted", { label: a.label }));
      await refresh();
    },
    onError: (err, a) => notify.error(apiErrorText(err, t("notify.overlayFailed", { label: a.label }))),
  });

  async function openPage(entryId: string) {
    // Open the tab inside the tap (iOS popup rule), then point it at the
    // short-lived view-token URL — never the session token in a link.
    const tab = window.open("", "_blank");
    try {
      const { token } = await benchApi.entries.viewToken(testId, entryId);
      const url = benchApi.entryViewUrl(testId, entryId, token);
      if (tab) {
        tab.opener = null;
        tab.location.href = url;
      } else window.location.href = url;
    } catch {
      tab?.close();
      notify.error(t("notify.pageFailed"));
    }
  }

  async function copyLink() {
    try {
      await navigator.clipboard.writeText(`${window.location.origin}${benchResultHref(testId)}`);
      notify.success(t("notify.linkCopied"));
    } catch {
      notify.error(t("notify.copyFailed"));
    }
  }

  if (query.isError) {
    const status = apiStatus(query.error);
    const gone = status === 404 || status === 422;
    return (
      <NotFoundState
        title={gone ? t("notFound") : t("loadFailed")}
        hint={gone ? t("notFoundHint") : null}
        backHref={BENCH_LIST_HREF}
        backLabel={t("backToList")}
      />
    );
  }

  const test = query.data;
  if (!test) {
    return (
      <div className="flex items-center gap-2 py-16 justify-center text-sm" style={{ color: C.textMuted }}>
        <Loader2 size={16} aria-hidden className="animate-spin" />
        {t("loading")}
      </div>
    );
  }

  const live = isLive(test);
  const done = test.state === "done" || test.state === "done_missing";
  const contestants = allContestants(test);
  const primary = primaryOf(test);
  const allowed = new Set(test.allowed);
  const showTruncation = Boolean(test.truncation) && !truncationAcked && !live;
  // K3: the name stands in the verdict when there is one contestant with a verdict.
  const nameInVerdict = contestants.length === 1 && test.blocks.length === 1 && Boolean(test.verdict);
  const classWords = test.blocks.map((b) => words.runClass(b.run_class));
  const meta = [
    formatDay(test.created_at, words.locale),
    classWords.length > 1 ? t("metaClasses", { a: classWords[0], b: classWords[1] }) : classWords[0],
  ].filter(Boolean);

  const menu: OverflowAction[] = [];
  for (const id of menuIds(test, primary)) {
    if (id === "kit") menu.push({ id, label: t("menu.kitPreview"), icon: Share, onClick: () => setKitOpen(true) });
    if (id === "harvest")
      menu.push({ id, label: t("actions.harvest"), icon: RefreshCw, onClick: () => harvest.mutate(), loading: harvest.isPending });
    if (id === "rerun_larger")
      menu.push({ id, label: t("actions.rerunLarger"), icon: RefreshCw, onClick: () => rerunLarger.mutate(), loading: rerunLarger.isPending });
    if (id === "recompose" || id === "rerender" || id === "archive")
      menu.push({
        id,
        label: t(`menu.${id}`),
        icon: id === "recompose" ? Clapperboard : id === "rerender" ? Film : Archive,
        onClick: () => legacy.mutate(id),
        loading: legacy.isPending && legacy.variables === id,
      });
    if (id === "delete")
      menu.push({ id, label: t("menu.delete"), icon: Trash2, destructive: true, onClick: () => setConfirmDelete(true) });
  }
  const pages = openablePages(test);
  const pageItems: OverflowAction[] = pages.map((p) => ({
    id: `page-${p.entryId}`,
    label:
      pages.length > 1
        ? t("menu.openPageOf", { name: p.contestant.display_name, i: p.run })
        : t("menu.openPage", { i: p.run }),
    icon: ExternalLink,
    onClick: () => void openPage(p.entryId),
  }));
  const overlayItems: OverflowAction[] = safeOverlayActions(test).map((a) => ({
    id: `overlay-${a.id}`,
    label: a.label,
    icon: Send,
    disabled: a.disabled,
    loading: a.busy || (overlay.isPending && overlay.variables?.id === a.id),
    destructive: a.style === "danger",
    onClick: () => (a.confirm ? setConfirmOverlay(a) : overlay.mutate(a)),
  }));
  // Order: kit/fixes first, then pages, overlay, and archive/delete last.
  const tail = menu.filter((m) => m.id === "archive" || m.id === "delete");
  const head = menu.filter((m) => m.id !== "archive" && m.id !== "delete");
  const menuActions = [...head, ...pageItems, ...overlayItems, ...tail];

  const primaryButton = (() => {
    if (primary === "kit")
      return (
        <button type="button" data-primary="true" onClick={() => setKitOpen(true)} className={`${PRIMARY_BTN} flex-1`} style={{ backgroundColor: C.accent, color: C.onAccent }}>
          <Share size={16} aria-hidden />
          {t("actions.kit")}
        </button>
      );
    if (primary === "harvest")
      return (
        <button type="button" data-primary="true" onClick={() => harvest.mutate()} disabled={harvest.isPending} className={`${PRIMARY_BTN} flex-1`} style={{ backgroundColor: C.accent, color: C.onAccent }}>
          {harvest.isPending ? <Loader2 size={16} aria-hidden className="animate-spin" /> : <RefreshCw size={16} aria-hidden />}
          {t("actions.harvest")}
        </button>
      );
    if (primary === "rerun")
      return (
        <button type="button" data-primary="true" onClick={() => router.push(benchTestHref({ rerunOf: testId, trigger: "rerun" }))} className={`${PRIMARY_BTN} flex-1`} style={{ backgroundColor: C.accent, color: C.onAccent }}>
          <RefreshCw size={16} aria-hidden />
          {t("actions.rerun")}
        </button>
      );
    return null;
  })();
  const secondaryRerun = primary !== "rerun" && allowed.has("rerun") && !live;

  const receipts = contestants.filter((c) => c.receipt);
  const receiptTitle = (c: ContestantResult, block: string) =>
    contestants.length === 1
      ? t("receipt.title")
      : t("receipt.titleFor", { name: test.blocks.length > 1 ? block : c.display_name });

  return (
    <div
      data-testid="bench-result-page"
      data-test-id={testId}
      className="grid grid-cols-1 gap-y-6 pb-8 lg:grid-cols-[minmax(0,1fr)_300px] lg:gap-x-8 2xl:grid-cols-[280px_minmax(0,1fr)_320px]"
    >
      <aside className="hidden 2xl:block min-w-0" data-region="result-list">
        <Suspense fallback={null}>
          <TestList />
        </Suspense>
      </aside>

      {/* Center: head, video/live, numbers */}
      <div className="contents lg:flex lg:flex-col lg:gap-6 min-w-0">
        <header data-region="result-head" className="order-1 lg:order-none flex flex-col gap-3 min-w-0">
          <div className="flex items-center -ml-2 -mr-2">
            <Link
              href={BENCH_LIST_HREF}
              className="flex items-center gap-1 min-h-11 px-2 rounded-md text-sm"
              style={{ color: C.textSecondary }}
            >
              <ChevronLeft size={18} aria-hidden />
              {t("back")}
            </Link>
            <span className="flex-1" />
            <button
              type="button"
              onClick={() => void copyLink()}
              aria-label={t("copyLink")}
              title={t("copyLink")}
              className="flex items-center justify-center w-11 h-11 rounded-md cursor-pointer"
              style={{ color: C.textMuted }}
            >
              <Link2 size={18} aria-hidden />
            </button>
            <OverflowMenu actions={menuActions} label={t("menu.more")} testId="result-more" />
          </div>
          <div className="flex flex-col gap-1">
            <h1 className="display text-2xl font-semibold break-words" style={{ color: C.textPrimary }}>
              {test.title}
            </h1>
            <p className="text-xs" style={{ color: C.textMuted }}>
              {meta.join(" · ")}
            </p>
          </div>
          {done ? (
            <GateLine gate={test.gate} words={words} hideTruncation={Boolean(test.truncation)} />
          ) : (
            <StateLine test={test} words={words} />
          )}
          {done && <VerdictLine test={test} words={words} />}
        </header>

        <section data-region="result-video" className="order-3 lg:order-none min-w-0">
          {live ? (
            <LiveRun test={test} onStopped={(next) => qc.setQueryData(key, next)} />
          ) : (
            <VideoBlock test={test} words={words} />
          )}
        </section>

        {!live && test.state === "aborted" && (
          <section data-region="result-facts" className="order-4 lg:order-none">
            <AbortedFacts test={test} words={words} />
          </section>
        )}

        {done && test.blocks.length > 0 && (
          <section data-region="result-numbers" data-testid="result-numbers" className="order-4 lg:order-none min-w-0">
            <h2 className="label-sys">{t("numbers.label")}</h2>
            <div className="lg:hidden">
              <NumbersList blocks={test.blocks} words={words} showNames={!nameInVerdict} />
            </div>
            <div className="hidden lg:block mt-2">
              <NumbersTable blocks={test.blocks} words={words} showNames={!nameInVerdict} />
            </div>
          </section>
        )}
      </div>

      {/* Side: next step, checks, receipt (sticky on the desktop) */}
      <div className="contents lg:flex lg:flex-col lg:gap-6 lg:sticky lg:top-0 lg:self-start lg:pt-12">
        {(showTruncation || primaryButton || secondaryRerun) && (
          <section data-region="result-actions" className="order-2 lg:order-none flex flex-col gap-3">
            {showTruncation && test.truncation && (
              <TruncationWarning
                truncation={test.truncation}
                contestants={contestants}
                words={words}
                canRerun={allowed.has("rerun_larger")}
                rerunPending={rerunLarger.isPending}
                onRerun={() => rerunLarger.mutate()}
                onContinue={ackTruncation}
              />
            )}
            <div className="flex items-center gap-2">
              {primaryButton}
              {secondaryRerun && (
                <button
                  type="button"
                  onClick={() => router.push(benchTestHref({ rerunOf: testId, trigger: "rerun" }))}
                  className="min-h-11 px-4 rounded-md text-sm cursor-pointer"
                  style={{ color: C.textSecondary }}
                >
                  {t("actions.rerun")}
                </button>
              )}
            </div>
          </section>
        )}

        {done && (
          <div className="hidden lg:block">
            <GateChecks gate={test.gate} words={words} hideTruncation={Boolean(test.truncation)} />
          </div>
        )}

        {done && receipts.length > 0 && (
          <section data-region="result-receipt" className="order-5 lg:order-none" style={{ borderBottom: `1px solid ${C.border}` }}>
            {test.blocks.map((b) =>
              b.contestants
                .filter((c) => c.receipt)
                .map((c) => (
                  <ReceiptView key={c.key} contestant={c} words={words} title={receiptTitle(c, words.runClass(b.run_class))} />
                )),
            )}
          </section>
        )}
      </div>

      <XKitSheet testId={testId} open={kitOpen} onClose={() => setKitOpen(false)} />
      <ConfirmDialog
        open={confirmDelete}
        kicker={t("confirmDelete.kicker")}
        title={t("confirmDelete.title")}
        body={t("confirmDelete.body")}
        confirmLabel={t("confirmDelete.confirm")}
        cancelLabel={t("confirmDelete.cancel")}
        loading={remove.isPending}
        onConfirm={() => remove.mutate()}
        onCancel={() => setConfirmDelete(false)}
      />
      <ConfirmDialog
        open={Boolean(confirmOverlay)}
        kicker={t("confirmOverlay.kicker")}
        title={confirmOverlay?.confirm ?? ""}
        confirmLabel={confirmOverlay?.label}
        cancelLabel={t("confirmDelete.cancel")}
        danger={confirmOverlay?.style === "danger"}
        loading={overlay.isPending}
        onConfirm={() => confirmOverlay && overlay.mutate(confirmOverlay)}
        onCancel={() => setConfirmOverlay(null)}
      />
    </div>
  );
}
