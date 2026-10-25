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
// Phone: one column, content to the edge (K8). Desktop ≥ 1024 px: the main
// column (head, private warning, video, numbers; at most 68 rem) and, for a
// finished test only, a sticky right column with the actions, the gate checks
// and the receipt; ≥ 1536 px the test list (package D) joins on the left in
// a fluid 20–28 rem column. One DOM for both: the column wrappers are
// `display: contents` on the phone, so their parts interleave by `order`.
//
// Signature is frozen (ResultPageProps).
import { Suspense, useEffect, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
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
import { ConfirmDialog } from "@/components/shared/ConfirmDialog";
import { NotFoundState } from "@/components/shared/NotFoundState";
import { OverflowMenu, type OverflowAction } from "@/components/shared/OverflowMenu";
import { useAuthBlob } from "@/hooks/useAuthBlob";
import { api, request } from "@/lib/api";
import { BENCH_LIST_HREF, benchResultHref, benchTestHref } from "@/lib/benchLinks";
import { C } from "@/lib/colors";
import { notify } from "@/lib/notify";
// Legacy client, used ONLY for what W1 has no route for yet: the files API
// subpath of the video, rerender/recompose/archive/delete and the entry page
// view token (CONTRACT §2 — mapped from BenchTestDetail.allowed).
import { benchApi } from "@/verticals/bench_studio/api";
import { LIVE_POLL_MS, benchW1Api, benchW1Keys } from "../api";
import { XKitSheet } from "../kit/XKitSheet";
import { TestList } from "../list/TestList";
import { LiveRun } from "../live/LiveRun";
import type { BenchTestDetail, ContestantResult, OverlayAction } from "../types";
import { apiErrorText, apiStatus } from "./errors";
import { formatClockTime, formatDay, formatTime, formatTokens, statText } from "./format";
import { GateChecks, GateLine } from "./GateView";
import { isLive, menuIds, openablePages, primaryOf, safeOverlayActions } from "./plan";
import { ReceiptView } from "./ReceiptView";
import { RunsBreakdown } from "./RunsBreakdown";
import { NumbersList, NumbersTable } from "./ResultNumbers";
import { TruncationWarning } from "./TruncationWarning";
import { useTruncationAck } from "./truncationAck";
import { useRerunLarger } from "./useRerunLarger";
import { useBenchWords, type BenchWords } from "./words";

export interface ResultPageProps {
  testId: string;
}

export default function ResultPage({ testId }: ResultPageProps) {
  return (
    <AppShell fullHeight mobileHideAppBar wide>
      {/* One scroll for the page up to 1535 px; from 1536 px the panes scroll. */}
      <div className="flex-1 min-h-0 overflow-y-auto overflow-x-hidden pt-safe md:pt-0 2xl:overflow-hidden">
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
        data-testid="result-no-video"
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
  // "Lauf 1 · 20 s Aufnahme + 4.5 s Zahlenkarte" — never one number that
  // reads like the run's compute time.
  const card = duration !== null && duration > RECEIPT_CARD_S ? RECEIPT_CARD_S : null;
  const fmt = (v: number) => new Intl.NumberFormat(words.locale, { maximumFractionDigits: 1 }).format(v);
  return (
    <figure className="flex flex-col gap-2">
      <VideoStage url={url} />
      <figcaption className="text-xs" style={{ color: C.textMuted }}>
        {duration !== null && card !== null
          ? t("video.caption", { i: test.video.run_index, rec: fmt(duration - card), card: fmt(card) })
          : t("video.captionShort", { i: test.video.run_index })}
      </figcaption>
    </figure>
  );
}

/** The video is the hero of a finished test: a 16:9 stage that holds its
 *  place while the bytes load (nothing jumps), the recording fills it. The
 *  column around it is sized so the stage never leaves the screen. */
function VideoStage({ url }: { url: string }) {
  const tf = useTranslations("filePreview");
  const { blobUrl, error } = useAuthBlob(url);
  return (
    <div
      data-testid="result-video-stage"
      className="relative w-full aspect-video rounded-lg overflow-hidden flex items-center justify-center"
      style={{ backgroundColor: C.bgDeep, border: `1px solid ${C.border}` }}
    >
      {blobUrl ? (
        <video src={blobUrl} controls preload="metadata" className="absolute inset-0 w-full h-full object-contain" />
      ) : error ? (
        <span className="text-sm" style={{ color: C.textMuted }}>
          {tf("loadFailed")}
        </span>
      ) : (
        <Loader2 size={18} aria-label={tf("loading")} className="animate-spin" style={{ color: C.textMuted }} />
      )}
    </div>
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
            // In the narrow rail a long model name moves under its label
            // whole instead of breaking at a hyphen.
            className="flex flex-wrap items-start justify-between gap-x-4 gap-y-1 py-3"
            style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
          >
            <dt className="text-sm" style={{ color: C.textMuted }}>
              {r.label}
            </dt>
            <dd className="ml-auto flex flex-col items-end text-right min-w-0 [overflow-wrap:anywhere]">
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

/** The receipt card at the end of the video (renderer W1_RECEIPT_CARD_S). */
const RECEIPT_CARD_S = 4.5;

/** Gutters beside the pane hairlines: 24 px, 32 px from 1920 px. */
const PANE_WIDE_PAD_R = "min-[120rem]:pr-8";
const PANE_WIDE_PAD_L = "min-[120rem]:pl-8";
const PANE_WIDE_PAD_X = "min-[120rem]:px-8";
/** The main column from 1024 px: as wide as a 16:9 video can be with the
 *  head above it and the caption still on screen (≈ 20 rem), never under
 *  40 rem. One width for every state, so stepping through the list never
 *  moves the left edge or the head. */
const STAGE_MAX_W = "lg:max-w-[max(40rem,calc((100dvh-20rem)*16/9))]";

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

  // Opening a finished result is what its Eingang item asks for: resolve it
  // here, so a test watched live costs no extra "Erledigt" tap (once per
  // record; the server ignores a test that still runs).
  const finished = query.data && !isLive(query.data) ? query.data.id : null;
  const seenFor = useRef<string | null>(null);
  useEffect(() => {
    if (!finished || seenFor.current === finished) return;
    seenFor.current = finished;
    benchW1Api.tests
      .seen(finished)
      .then((r) => {
        if (r.resolved > 0) void qc.invalidateQueries({ queryKey: ["approvals"] });
      })
      .catch(() => {
        // the Eingang card still resolves itself on tap
      });
  }, [finished, qc]);

  const harvest = useMutation({
    mutationFn: () => benchW1Api.tests.harvest(testId),
    onSuccess: (data) => {
      qc.setQueryData(key, data);
      qc.invalidateQueries({ queryKey: benchW1Keys.kit(testId) });
      notify.success(t("notify.harvestStarted"));
    },
    onError: (err) => notify.error(apiErrorText(err, t("notify.harvestFailed"))),
  });

  // A paid model: the price is confirmed right on the button (D4), the
  // larger budget stays (no detour through the sheet).
  const rerunLarger = useRerunLarger(
    testId,
    (next) => router.push(benchResultHref(next.id)),
    t("notify.rerunFailed"),
  );

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
  // A pending price question keeps the warning in view even after
  // "continue anyway" (the ⋯ re-run puts its question on this button).
  const showTruncation = Boolean(test.truncation) && (!truncationAcked || Boolean(rerunLarger.costAsk)) && !live;
  // Mark's decision 24.10.: the larger budget first, then the kit — until he
  // says "Trotzdem weiter" the re-run is the one primary, the kit a quiet
  // button and the plain "Nochmal" (same budget, same cut) is not offered.
  const truncationFirst = showTruncation && allowed.has("rerun_larger");
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
      menu.push({ id, label: t("actions.rerunLarger"), icon: RefreshCw, onClick: rerunLarger.run, loading: rerunLarger.pending });
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
    if (truncationFirst) {
      if (primary === "kit")
        return (
          <button type="button" onClick={() => setKitOpen(true)} className={`${PRIMARY_BTN} flex-1`} style={{ color: C.textPrimary, border: `1px solid ${C.borderActive}` }}>
            <Share size={16} aria-hidden />
            {t("actions.kit")}
          </button>
        );
      if (primary === "harvest")
        return (
          <button type="button" onClick={() => harvest.mutate()} disabled={harvest.isPending} className={`${PRIMARY_BTN} flex-1`} style={{ color: C.textPrimary, border: `1px solid ${C.borderActive}` }}>
            {harvest.isPending ? <Loader2 size={16} aria-hidden className="animate-spin" /> : <RefreshCw size={16} aria-hidden />}
            {t("actions.harvest")}
          </button>
        );
      return null;
    }
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
  const secondaryRerun = primary !== "rerun" && allowed.has("rerun") && !live && !truncationFirst;

  const receipts = contestants.filter((c) => c.receipt);
  const receiptTitle = (c: ContestantResult, block: string) =>
    contestants.length === 1
      ? t("receipt.title")
      : t("receipt.titleFor", { name: test.blocks.length > 1 ? block : c.display_name });

  // Where the parts sit (25.10., operator on 1920–3440 px screens):
  // · the main column tells what happened: head, the private warning right
  //   under the state line, the video as the hero, the numbers;
  // · the right rail says what to do and what it was (K12 "properties from
  //   1024 px in a right column"): X-Kit / Run again, the checks and the
  //   receipt of a finished test, start and contestant of a stopped one;
  // · from 1536 px the list joins as a third pane. Each pane scrolls on its
  //   own, hairlines part them, all three start on one top line.
  // A live test has no rail: the live run owns the page (K3). The panes keep
  // their scrollbar's room (Windows draws classic bars), so a result that
  // scrolls and one that does not put the column on the same pixels.
  const hasButtons = Boolean(primaryButton || secondaryRerun);
  const checksShown = done && test.gate.status !== "pending";
  const aborted = !live && test.state === "aborted";
  const hasSide = !live && (aborted || hasButtons || checksShown || receipts.length > 0);

  const notice = showTruncation && test.truncation && (
    // The warning's paragraphs keep a reading measure inside the full-width box.
    <section data-region="result-notice" className="order-2 lg:order-none min-w-0 [&_p]:max-w-[72ch]">
      <TruncationWarning
        truncation={test.truncation}
        contestants={contestants}
        words={words}
        canRerun={allowed.has("rerun_larger")}
        rerunPending={rerunLarger.pending}
        costAsk={rerunLarger.costAsk}
        primary={truncationFirst}
        onRerun={rerunLarger.run}
        onContinue={ackTruncation}
      />
    </section>
  );

  const buttons = hasButtons && (
    <section data-region="result-actions" className="order-2 lg:order-none flex items-center gap-2">
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
    </section>
  );

  return (
    <div
      data-testid="bench-result-page"
      data-test-id={testId}
      data-columns={hasSide ? "main-side" : "main"}
      className={`grid grid-cols-1 gap-y-6 pb-8 lg:gap-x-8 2xl:h-full 2xl:pb-0 2xl:gap-x-0 ${
        hasSide
          ? "lg:grid-cols-[minmax(0,1fr)_clamp(18rem,26%,22rem)] 2xl:grid-cols-[clamp(22rem,24%,32rem)_minmax(0,1fr)_clamp(18rem,18%,28rem)]"
          : "2xl:grid-cols-[clamp(22rem,24%,32rem)_minmax(0,1fr)]"
      }`}
    >
      <aside
        className={`hidden 2xl:block min-w-0 2xl:h-full 2xl:overflow-y-auto 2xl:[scrollbar-gutter:stable] scroll-quiet 2xl:pr-6 ${PANE_WIDE_PAD_R} 2xl:pb-8 2xl:border-r`}
        style={{ borderColor: C.border }}
        data-region="result-list"
      >
        <Suspense fallback={null}>
          <TestList />
        </Suspense>
      </aside>

      {/* Main: head, warning, video/live, numbers. From 1024 px one column
          as wide as the video can be without leaving the screen (16:9 under
          the head), every line on its left edge. */}
      <div
        data-region="result-main"
        className={`contents lg:block min-w-0 2xl:h-full 2xl:overflow-y-auto 2xl:[scrollbar-gutter:stable] scroll-quiet 2xl:px-6 ${PANE_WIDE_PAD_X} 2xl:pb-8`}
      >
        <div className={`contents lg:flex lg:flex-col lg:gap-6 lg:w-full ${STAGE_MAX_W} 2xl:mx-auto`}>
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
              <h1 className="display text-2xl font-semibold break-words max-w-[72ch]" style={{ color: C.textPrimary }}>
                {test.title}
              </h1>
              <p className="text-xs" style={{ color: C.textMuted }}>
                {meta.join(" · ")}
              </p>
            </div>
            {/* While it runs the live run owns the one state line (K3). */}
            {done ? (
              <GateLine gate={test.gate} words={words} hideTruncation={Boolean(test.truncation)} />
            ) : (
              !live && <StateLine test={test} words={words} />
            )}
            {done && (
              <div className="max-w-[72ch]">
                <VerdictLine test={test} words={words} />
              </div>
            )}
          </header>

          {notice}

          <section data-region="result-video" className="order-3 lg:order-none min-w-0">
            {live ? (
              <LiveRun test={test} onStopped={(next) => qc.setQueryData(key, next)} />
            ) : (
              <VideoBlock test={test} words={words} />
            )}
          </section>

          {aborted && (
            <section data-region="result-runs" className="order-4 lg:order-none min-w-0">
              <RunsBreakdown test={test} words={words} />
            </section>
          )}

          {done && test.blocks.length > 0 && (
            <section data-region="result-numbers" data-testid="result-numbers" className="order-4 lg:order-none min-w-0">
              <h2 className="label-sys">{t("numbers.label")}</h2>
              <div className="lg:hidden">
                <NumbersList blocks={test.blocks} words={words} showNames={!nameInVerdict} />
              </div>
              {/* Across the column: video and table share one right edge. */}
              <div className="hidden lg:block mt-2">
                <NumbersTable blocks={test.blocks} words={words} showNames={!nameInVerdict} />
              </div>
            </section>
          )}
        </div>
      </div>

      {/* Rail: next step, checks, receipt — or start and contestant of a
          stopped test. Sticky beside the page up to 1535 px, its own pane from
          1536 px; on the phone its parts fall into the one column by order. */}
      {hasSide && (
        <div
          data-region="result-side"
          className={`contents lg:flex lg:flex-col lg:gap-6 lg:sticky lg:top-0 lg:self-start min-w-0 2xl:static 2xl:self-stretch 2xl:h-full 2xl:overflow-y-auto 2xl:[scrollbar-gutter:stable] scroll-quiet 2xl:pl-6 ${PANE_WIDE_PAD_L} 2xl:pb-8 2xl:border-l`}
          style={{ borderColor: C.border }}
        >
          {buttons}

          {checksShown && (
            <div className="hidden lg:block">
              <GateChecks gate={test.gate} words={words} hideTruncation={Boolean(test.truncation)} />
            </div>
          )}

          {aborted && (
            <section data-region="result-facts" className="order-4 lg:order-none">
              <AbortedFacts test={test} words={words} />
            </section>
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
      )}

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
