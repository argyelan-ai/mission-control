"use client";

/**
 * Bench W1 list (P7, package D) — the bench home.
 *
 * Sections Läuft · Neu · Braucht Blick · Fertig (GET /bench/tests, human
 * states only; "Neu" = the list's own: ended tests started < 24 h ago), every row a link to its result page (benchResultHref), header
 * button "Modell testen" into the test sheet (benchTestHref, trigger
 * "list") with the prompt library next to it, filters model · device ·
 * harness · class (server-side query), archive toggle. Replaces the legacy
 * ChallengesTab list.
 *
 * Desktop: master–detail lives on the result page (/bench/r/<id>, a
 * shareable URL): from 1536 px this list is its left pane and the open
 * test's row carries the selection (aria-current). The list never uses
 * viewport columns — a viewport grid here once spilled the list across the
 * result (25.10.). It lays out by its own width (@container): one column in
 * the pane and on the phone; on the wide /bench page (≥ 56 rem) each section
 * becomes a gallery of cards with the test's picture (GET …/poster, loaded
 * when a card comes on screen), because a benchmark is recognised by what it
 * drew, not by its title.
 *
 * Words: state/section/wait/abort/gate words come from `benchShared`
 * (contract), everything else from `benchHome`.
 *
 * Signature is frozen (contract): no props.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import Link from "next/link";
import { usePathname } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Archive, BookOpen, ChevronDown, Clock, FlaskConical, Image as ImageIcon, ImageOff, Loader2, Server, X } from "lucide-react";
import { useAuthBlob } from "@/hooks/useAuthBlob";
import { C, STATUS_TEXT } from "@/lib/colors";
import { Pill } from "@/components/shared/Pill";
import { benchResultHref, benchTestHref } from "@/lib/benchLinks";
import { benchW1Api, benchW1Keys, type TestListQuery } from "../api";
import type { BenchTestList, BenchTestListItem, Harness, Reason, RunClass, Section } from "../types";

type Group = Section | "new";
/** Sections in order; "new" is the list's own (not a server section). */
const GROUPS: Group[] = ["running", "new", "needs_look", "done"];
/** A test started within this window that no longer runs sits on top
 *  under "New" — a fresh result must not hide below old tests that need a
 *  look (27.10.: an agent run ended after an hour, below the fold). */
const FRESH_MS = 24 * 3_600_000;

function isFresh(item: BenchTestListItem, now: number): boolean {
  if (item.section === "running") return false;
  const at = Date.parse(item.created_at);
  return Number.isFinite(at) && now - at < FRESH_MS;
}

const HARNESSES: Harness[] = ["api", "omp", "hermes", "grok-build", "claude-code"];
const RUN_CLASSES: RunClass[] = ["A", "B", "C"];
/** While something runs or waits, the list follows it (the live run itself polls faster). */
const LIST_POLL_MS = 5_000;

type FilterId = "model" | "device" | "harness" | "runClass";
type Filters = Partial<Record<FilterId, string>>;

function day(iso: string | null, locale: string): string | null {
  if (!iso) return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { day: "2-digit", month: "2-digit" }).format(d);
}

function clock(iso: unknown, locale: string): string | null {
  if (typeof iso !== "string") return null;
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return null;
  return new Intl.DateTimeFormat(locale, { hour: "2-digit", minute: "2-digit" }).format(d);
}

function pollWhileActive(data: BenchTestList | undefined): number | false {
  return (data?.counts.running ?? 0) > 0 ? LIST_POLL_MS : false;
}

function useBenchWords() {
  const ts = useTranslations("benchShared");
  const locale = useLocale();
  /** A machine reason in words; null when the catalog does not know it. */
  const reasonText = (r: Reason | null): string | null => {
    if (!r) return null;
    const params: Record<string, string | number> = {};
    for (const [k, v] of Object.entries(r.params ?? {})) {
      if (typeof v === "string" || typeof v === "number") params[k] = v;
    }
    if (ts.has(`wait.${r.code}`)) {
      const time = clock(r.params?.until, locale);
      return ts(`wait.${r.code}`, { ...params, time: time ?? "" });
    }
    if (ts.has(`abort.${r.code}`)) return ts(`abort.${r.code}`);
    if (ts.has(`reason.${r.code}`)) return ts(`reason.${r.code}`, params);
    if (ts.has(`gate.${r.code}`)) return ts(`gate.${r.code}`, params);
    return null;
  };
  return { ts, reasonText };
}

/** Status of one row: a pill (state that wants attention), a quiet word
 *  (done), and an optional line saying why. Never the same fact twice (K3). */
function useRowStatus() {
  const t = useTranslations("benchHome");
  const { ts, reasonText } = useBenchWords();
  return (item: BenchTestListItem): { pill?: { text: string; color: string }; quiet?: string; note?: string | null } => {
    const reason = item.state_reason;
    switch (item.state) {
      case "running": {
        const p = item.progress;
        const text =
          p && typeof p.done === "number" && typeof p.total === "number"
            ? t("progress", { state: ts("state.running"), done: p.done, total: p.total })
            : ts("state.running");
        return { pill: { text, color: C.info } };
      }
      case "waiting":
        return { pill: { text: ts("state.waiting"), color: C.textSecondary }, note: reasonText(reason) };
      case "aborted":
        return { pill: { text: ts("state.aborted"), color: C.textSecondary }, note: reasonText(reason) };
      case "done_missing":
        return {
          pill: { text: ts("state.done_missing"), color: STATUS_TEXT.error },
          // The pill already says the numbers are missing.
          note: reason?.code === "numbers_missing" ? null : reasonText(reason),
        };
      default:
        if (item.gate_status === "amber") {
          return { pill: { text: ts("gate.amber"), color: STATUS_TEXT.warning }, note: reasonText(reason) };
        }
        if (item.gate_status === "red") {
          return { pill: { text: ts("gate.red"), color: STATUS_TEXT.error }, note: reasonText(reason) };
        }
        return { quiet: ts("state.done") };
    }
  };
}

/** The test's picture, in both shapes of the row (30.10.: on the phone and
 *  in the pane beside a result no test showed it): a 16:9 thumbnail beside
 *  the text when the list is narrow, the card's header on the wide page.
 *  Fetched only once the slot is on screen, with the Bearer header like
 *  every file. Never an empty box: until the picture arrives — or when the
 *  test has none (still running, stopped, failed, a page that stayed blank)
 *  — the slot is a quiet frame with a sign; the card adds the word, the
 *  thumbnail leaves it to the row's own state line. */
function Poster({ item }: { item: BenchTestListItem }) {
  const t = useTranslations("benchHome");
  const ref = useRef<HTMLDivElement>(null);
  const [seen, setSeen] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || seen || !item.poster || typeof IntersectionObserver === "undefined") return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) {
          setSeen(true);
          io.disconnect();
        }
      },
      { rootMargin: "200px" },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [item.poster, seen]);
  const { blobUrl, error } = useAuthBlob(seen ? benchW1Api.tests.posterUrl(item.id) : null);
  const live = item.state === "running" || item.state === "waiting";
  const shown = !!blobUrl && !error;
  const state = shown ? "picture" : item.poster && !error ? "loading" : live ? "not-yet" : "none";
  const Sign = state === "loading" ? ImageIcon : item.state === "running" ? Loader2 : item.state === "waiting" ? Clock : ImageOff;
  return (
    <div
      ref={ref}
      data-testid="bench-row-poster"
      data-poster-state={state}
      className="relative flex shrink-0 w-24 aspect-video items-center justify-center overflow-hidden rounded-md border @4xl:w-full @4xl:rounded-none @4xl:border-0 @4xl:border-b"
      style={{ backgroundColor: C.bgDeep, borderColor: C.border }}
    >
      {shown ? (
        // Decorative: the title beside it names the test.
        <img src={blobUrl} alt="" className="absolute inset-0 w-full h-full object-cover object-top" />
      ) : (
        <span className="flex flex-col items-center gap-2 text-sm" style={{ color: C.textMuted }}>
          <Sign
            size={18}
            aria-hidden
            className={state === "loading" ? "opacity-40" : item.state === "running" ? "animate-spin motion-reduce:animate-none" : undefined}
          />
          {state !== "loading" && (
            <span className="hidden @4xl:inline">{t(state === "not-yet" ? "poster.notYet" : "poster.none")}</span>
          )}
        </span>
      )}
    </div>
  );
}

function TestRow({ item, current }: { item: BenchTestListItem; current: boolean }) {
  const t = useTranslations("benchHome");
  const { ts } = useBenchWords();
  const locale = useLocale();
  const status = useRowStatus()(item);
  const models = item.contestants.map((c) => c.display_name).join(" · ");
  const harnesses = [...new Set(item.contestants.map((c) => c.harness).filter((h): h is Harness => !!h))];
  const cloud = item.contestants.some((c) => c.locality === "cloud");
  // Beside a result the open test is brought into the list's view once — it
  // may sit far down a long "Done" section.
  const ref = useRef<HTMLAnchorElement>(null);
  useEffect(() => {
    if (current) ref.current?.scrollIntoView?.({ block: "nearest" });
  }, [current]);
  return (
    // One row, two shapes by the list's width (@container): a hairline row
    // with a picture thumbnail in a pane or on the phone; a card headed by
    // the test's picture on the wide page.
    // The row hairline sits on the wrapper: on the rounded link it would
    // curve up at the ends.
    <div className="border-b @4xl:border-b-0" style={{ borderColor: C.borderSubtle }}>
      <Link
        ref={ref}
        href={benchResultHref(item.id)}
        data-testid="bench-test-row"
        aria-current={current ? "page" : undefined}
        // The open test: accent tint + accent hairline (the selection pattern,
        // no bar, no halo); the others only lighten on hover. Every row has
        // the same box and the same inner padding left and right, so text
        // stays on one line selected or not. On the phone the hover tone may
        // bleed 8 px past the text; in the result page's pane (from 1536 px,
        // where the open test is marked) the box sits ON the list's edges —
        // a pane that scrolls clips what sticks out (25.10.) — and the focus
        // ring goes inside it for the same reason.
        className={`group flex items-start gap-3 -mx-2 px-2 py-3 rounded-lg border transition-colors focus-inset 2xl:mx-0 @4xl:mx-0 @4xl:p-0 @4xl:gap-0 @4xl:h-full @4xl:overflow-hidden @4xl:flex-col @4xl:items-stretch ${
          current
            ? ""
            : "border-transparent @4xl:border-[var(--color-border)] @4xl:bg-[var(--color-bg-surface)] hover:bg-[var(--color-bg-hover)]"
        }`}
        style={current ? { background: C.accentSubtle, borderColor: C.borderAccent } : undefined}
      >
        <Poster item={item} />
        {/* Title and model wrap, never "…": beside a result the column is
            narrow, and a cut model name ("D.", 25.10.) says nothing. The tags
            and the state go to the next line when they do not fit. */}
        <div className="flex-1 min-w-0 @4xl:px-4 @4xl:pt-3 @4xl:pb-4 @4xl:flex @4xl:flex-col">
          <div className="flex items-baseline justify-between gap-3">
            <span
              data-testid="bench-row-title"
              className="text-base font-medium min-w-0 [overflow-wrap:anywhere]"
              style={{ color: C.textPrimary }}
            >
              {item.title}
            </span>
            <span className="text-sm tabular-nums shrink-0" style={{ color: C.textMuted }}>
              {day(item.created_at, locale)}
            </span>
          </div>
          <div className="flex flex-wrap items-center gap-x-2 gap-y-1 mt-1 min-w-0 @4xl:mt-auto @4xl:pt-2">
            {models && (
              <span
                data-testid="bench-row-models"
                className="text-sm min-w-0 [overflow-wrap:anywhere]"
                style={{ color: C.textSecondary }}
              >
                {models}
              </span>
            )}
            {harnesses.map((h) => (
              <span
                key={h}
                className="text-xs px-2 py-1 rounded-sm shrink-0 leading-none"
                style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
              >
                {t(`harnessTag.${h}`)}
              </span>
            ))}
            {cloud && (
              <span
                className="text-xs px-2 py-1 rounded-sm shrink-0 leading-none"
                style={{ color: C.textSecondary, border: `1px solid ${C.border}` }}
              >
                {ts("locality.cloud")}
              </span>
            )}
            <span className="ml-auto shrink-0 pl-2">
              {status.pill ? (
                <Pill color={status.pill.color} casing="sentence" data-testid="bench-row-pill">
                  {status.pill.text}
                </Pill>
              ) : (
                <span className="text-sm" style={{ color: C.textMuted }}>
                  {status.quiet}
                </span>
              )}
            </span>
          </div>
          {status.note && (
            <div className="text-xs mt-1 first-letter:uppercase" style={{ color: C.textMuted }} data-testid="bench-row-note">
              {status.note}
            </div>
          )}
        </div>
      </Link>
    </div>
  );
}

function FilterChip({
  id,
  label,
  value,
  valueLabel,
  options,
  onChange,
}: {
  id: FilterId;
  label: string;
  value: string | undefined;
  valueLabel: string | undefined;
  options: { value: string; label: string }[];
  onChange: (v: string | undefined) => void;
}) {
  const t = useTranslations("benchHome.filter");
  const on = !!value;
  return (
    <div
      data-testid={`bench-filter-${id === "runClass" ? "class" : id}`}
      className="relative inline-flex items-center shrink-0 h-11 rounded-full transition-colors hover:bg-[var(--color-bg-hover)]"
      style={{
        background: on ? C.accentSubtle : "transparent",
        border: `1px solid ${on ? C.borderAccent : C.border}`,
        color: on ? C.textPrimary : C.textSecondary,
      }}
    >
      <span className={`flex items-center gap-1 pl-4 text-sm pointer-events-none whitespace-nowrap ${on ? "pr-1" : "pr-3"}`}>
        {on ? t("active", { label, value: valueLabel ?? value }) : label}
        {!on && <ChevronDown size={14} aria-hidden style={{ color: C.textMuted }} />}
      </span>
      {/* The native picker (wheel on the iPhone) — invisible over the chip. */}
      <select
        aria-label={label}
        value={value ?? ""}
        onChange={(e) => onChange(e.target.value || undefined)}
        className="absolute inset-0 w-full h-full opacity-0 cursor-pointer"
      >
        <option value="">{t("all")}</option>
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
      {on && (
        <button
          type="button"
          onClick={() => onChange(undefined)}
          aria-label={t("clear", { label })}
          className="relative flex items-center justify-center w-9 h-11 rounded-full cursor-pointer"
          style={{ color: C.textSecondary }}
        >
          <X size={14} aria-hidden />
        </button>
      )}
    </div>
  );
}

function SkeletonRows() {
  return (
    <div aria-hidden className="flex flex-col">
      {Array.from({ length: 6 }, (_, i) => (
        <div key={i} className="flex flex-col gap-2 py-3" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
          <span className="h-4 w-3/5 rounded-sm" style={{ background: C.bgSurface }} />
          <span className="h-3 w-2/5 rounded-sm" style={{ background: C.bgSurface }} />
        </div>
      ))}
    </div>
  );
}

const PRIMARY_BTN =
  "inline-flex items-center justify-center gap-2 h-11 px-4 rounded-md text-sm font-semibold whitespace-nowrap shrink-0 hover:brightness-110";

export function TestList() {
  const t = useTranslations("benchHome");
  const { ts } = useBenchWords();
  const locale = useLocale();
  const pathname = usePathname();
  const [filters, setFilters] = useState<Filters>({});
  const [archive, setArchive] = useState(false);

  const baseQuery: TestListQuery = { includeArchived: archive };
  const base = useQuery({
    queryKey: benchW1Keys.tests(baseQuery),
    queryFn: () => benchW1Api.tests.list(baseQuery),
    refetchInterval: (q) => pollWhileActive(q.state.data),
  });

  const activeFilters = (Object.entries(filters) as [FilterId, string | undefined][]).filter(
    (e): e is [FilterId, string] => !!e[1],
  );
  const filterQuery: TestListQuery = {
    ...baseQuery,
    ...(filters.model ? { model: filters.model } : {}),
    ...(filters.device ? { device: filters.device } : {}),
    ...(filters.harness ? { harness: filters.harness as Harness } : {}),
    ...(filters.runClass ? { runClass: filters.runClass as RunClass } : {}),
  };
  const filtered = useQuery({
    queryKey: benchW1Keys.tests(filterQuery),
    queryFn: () => benchW1Api.tests.list(filterQuery),
    enabled: activeFilters.length > 0,
    refetchInterval: (q) => pollWhileActive(q.state.data),
  });
  // Device names for the filter: the contestants' display names (never an IP).
  const contestants = useQuery({
    queryKey: benchW1Keys.contestants(),
    queryFn: () => benchW1Api.contestants(),
    staleTime: 60_000,
    retry: false,
  });

  const shown = activeFilters.length > 0 ? filtered : base;

  const modelOptions = useMemo(() => {
    const names = new Set<string>();
    for (const item of base.data?.items ?? []) for (const c of item.contestants) names.add(c.display_name);
    return [...names].sort().map((n) => ({ value: n, label: n }));
  }, [base.data]);
  const deviceOptions = useMemo(() => {
    const names = new Set<string>();
    for (const c of [...(contestants.data?.local ?? []), ...(contestants.data?.cloud ?? [])]) {
      if (c.device?.name) names.add(c.device.name);
    }
    return [...names].sort().map((n) => ({ value: n, label: n }));
  }, [contestants.data]);
  const harnessOptions = HARNESSES.map((h) => ({ value: h, label: t(`harnessTag.${h}`) }));
  const classOptions = RUN_CLASSES.map((c) => ({ value: c, label: ts(`runClass.${c}`) }));

  const labelOf = (id: FilterId, value: string): string => {
    if (id === "harness") return t(`harnessTag.${value}`);
    if (id === "runClass") return ts(`runClass.${value}`);
    return value;
  };
  const setFilter = (id: FilterId) => (v: string | undefined) => setFilters((f) => ({ ...f, [id]: v }));

  const total = base.data?.total ?? 0;
  const isEmpty = base.isSuccess && total === 0 && activeFilters.length === 0 && !archive;
  const lastDay = day(base.data?.last_at ?? null, locale);
  const testHref = benchTestHref({ trigger: "list" });

  // Narrow (phone, or the column beside a result): two rows —
  // title | Modell testen (the button wraps under the title when the room
  // is too small, never over it), then count | library. Wide: one bar —
  // title … library · Modell testen, the count below. Container queries,
  // not viewport breakpoints: the list must lay out by the room it gets.
  const header = (
    <div
      data-testid="bench-list-header"
      className="flex flex-wrap items-center gap-x-3 gap-y-1"
    >
      <h1 className="display text-2xl font-semibold flex-1 @2xl:order-1" style={{ color: C.textPrimary }}>
        {t("title")}
      </h1>
      {!isEmpty && base.isSuccess && (
        <Link
          href={testHref}
          data-testid="bench-list-test"
          className={`${PRIMARY_BTN} @2xl:order-3`}
          style={{ background: C.accent, color: C.onAccent }}
        >
          <FlaskConical size={16} aria-hidden />
          {t("testModel")}
        </Link>
      )}
      {/* Narrow only: ends the title row. */}
      <span aria-hidden className="basis-full h-0 @2xl:hidden" />
      {base.isSuccess && total > 0 && (
        <p
          className="text-sm tabular-nums flex-1 min-w-0 @2xl:order-4 @2xl:basis-full"
          style={{ color: C.textSecondary }}
          data-testid="bench-list-lead"
        >
          {lastDay ? t("lead", { count: total, date: lastDay }) : t("leadNoDate", { count: total })}
        </p>
      )}
      {base.isSuccess && (
        <Link
          href="/bench?view=library"
          data-testid="bench-list-library"
          className="ml-auto inline-flex items-center gap-2 h-11 px-3 -mr-3 rounded-md text-sm font-medium whitespace-nowrap hover:bg-[var(--color-bg-hover)] @2xl:order-2 @2xl:mr-0 @2xl:border"
          style={{ color: C.textPrimary, borderColor: C.border }}
        >
          <BookOpen size={16} aria-hidden />
          {t("library")}
        </Link>
      )}
    </div>
  );

  if (isEmpty) {
    return (
      <div data-testid="bench-test-list" className="@container flex flex-col gap-8">
        {header}
        <div className="flex flex-col items-start gap-3 max-w-md pt-8">
          <FlaskConical size={32} aria-hidden style={{ color: C.textMuted }} />
          <h2 className="text-lg font-semibold" style={{ color: C.textPrimary }}>
            {t("empty.title")}
          </h2>
          <p className="text-sm" style={{ color: C.textSecondary }}>
            {t("empty.body")} <span className="hidden lg:inline">{t("empty.bodyDesktop")}</span>
          </p>
          <Link href="/runtimes" className={`${PRIMARY_BTN} mt-2`} style={{ background: C.accent, color: C.onAccent }}>
            <Server size={16} aria-hidden />
            {t("empty.toRuntimes")}
          </Link>
        </div>
      </div>
    );
  }

  const data = shown.data;
  const noHit = activeFilters.length > 0 && filtered.isSuccess && (data?.items.length ?? 0) === 0;

  return (
    <div data-testid="bench-test-list" className="@container">
      <div className="flex flex-col gap-4 min-w-0">
        {header}

        {base.isSuccess && total > 0 && (
          <div
            className="flex gap-2 overflow-x-auto -mx-4 px-4 lg:mx-0 lg:px-0 lg:flex-wrap [scrollbar-width:none]"
            role="group"
            aria-label={t("filter.label")}
          >
            <FilterChip id="model" label={t("filter.model")} value={filters.model} valueLabel={filters.model} options={modelOptions} onChange={setFilter("model")} />
            {deviceOptions.length > 0 && (
              <FilterChip id="device" label={t("filter.device")} value={filters.device} valueLabel={filters.device} options={deviceOptions} onChange={setFilter("device")} />
            )}
            <FilterChip
              id="harness"
              label={t("filter.harness")}
              value={filters.harness}
              valueLabel={filters.harness ? labelOf("harness", filters.harness) : undefined}
              options={harnessOptions}
              onChange={setFilter("harness")}
            />
            <FilterChip
              id="runClass"
              label={t("filter.runClass")}
              value={filters.runClass}
              valueLabel={filters.runClass ? labelOf("runClass", filters.runClass) : undefined}
              options={classOptions}
              onChange={setFilter("runClass")}
            />
          </div>
        )}

        {shown.isPending && (activeFilters.length > 0 || base.isPending) ? (
          <div data-testid="bench-list-loading" className="flex flex-col gap-2">
            <span className="text-sm" style={{ color: C.textMuted }} role="status">
              {t("loading")}
            </span>
            <SkeletonRows />
          </div>
        ) : shown.isError ? (
          <div className="flex flex-col items-start gap-2 pt-4" role="alert">
            <p className="text-sm" style={{ color: STATUS_TEXT.error }}>
              {t("loadFailed")}
            </p>
            <button
              type="button"
              onClick={() => shown.refetch()}
              className="inline-flex items-center h-11 px-3 -ml-3 rounded-md text-sm font-medium cursor-pointer hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textPrimary }}
            >
              {t("retry")}
            </button>
          </div>
        ) : noHit ? (
          <div className="flex flex-col items-start gap-1 pt-4">
            <h2 className="text-base font-semibold" style={{ color: C.textPrimary }}>
              {activeFilters.length === 1
                ? t("filter.noHitOne", { value: labelOf(activeFilters[0][0], activeFilters[0][1]) })
                : t("filter.noHitMany")}
            </h2>
            <button
              type="button"
              onClick={() => setFilters({})}
              className="inline-flex items-center gap-2 h-11 px-3 -ml-3 rounded-md text-sm font-medium cursor-pointer hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textPrimary }}
            >
              <X size={16} aria-hidden />
              {t("filter.reset")}
            </button>
          </div>
        ) : (
          data && (
            <div className="flex flex-col gap-6">
              {GROUPS.map((section) => {
                const now = Date.now();
                const items =
                  section === "new"
                    ? data.items.filter((i) => isFresh(i, now))
                    : data.items.filter((i) => i.section === section && !isFresh(i, now));
                // Moved, not copied: a section's server count loses its fresh tests.
                const moved = section === "new" ? 0 : data.items.filter((i) => i.section === section && isFresh(i, now)).length;
                const count = section === "new" ? items.length : Math.max((data.counts[section] ?? 0) - moved, items.length);
                if (count === 0) return null;
                return (
                  <section key={section} data-testid={`bench-section-${section}`} aria-labelledby={`bench-section-h-${section}`}>
                    {/* Count at the row's end in one column; next to the heading
                        once rows flow into columns (the end is far away then). */}
                    <div className="flex items-baseline justify-between @4xl:justify-start @4xl:gap-3">
                      <h2 id={`bench-section-h-${section}`} className="text-base font-semibold" style={{ color: C.textPrimary }}>
                        {section === "new" ? t("sectionNew") : ts(`section.${section}`)}
                      </h2>
                      <span className="text-sm tabular-nums" style={{ color: C.textMuted }} data-testid="bench-section-count">
                        {count}
                      </span>
                    </div>
                    {/* One column of rows in a pane or on the phone; on the wide
                        page a gallery: cards of ≥ 19 rem with the test's picture
                        (benchmarks are visual), newest first. */}
                    <div data-testid="bench-section-rows" className="grid grid-cols-1 @4xl:grid-cols-[repeat(auto-fill,minmax(19rem,1fr))] @4xl:gap-6 @4xl:mt-3">
                      {items.map((item) => (
                        <TestRow key={item.id} item={item} current={pathname === benchResultHref(item.id)} />
                      ))}
                    </div>
                  </section>
                );
              })}
            </div>
          )
        )}

        {base.isSuccess && (
          <div className="flex flex-wrap items-center gap-x-4 -ml-3">
            <button
              type="button"
              onClick={() => setArchive((v) => !v)}
              aria-pressed={archive}
              data-testid="bench-list-archive"
              className="inline-flex items-center gap-2 h-11 px-3 rounded-md text-sm cursor-pointer hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              <Archive size={16} aria-hidden />
              {archive ? t("archiveHide") : t("archiveShow")}
            </button>
          </div>
        )}
      </div>

    </div>
  );
}
