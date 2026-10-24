"use client";

/**
 * Bench W1 list (P7, package D) — the bench home.
 *
 * Sections Läuft · Braucht Blick · Fertig (GET /bench/tests, human states
 * only), every row a link to its result page (benchResultHref), header
 * button "Modell testen" into the test sheet (benchTestHref, trigger
 * "list"), filters model · device · harness · class (server-side query),
 * archive toggle and the way to the prompt library. Desktop adds the
 * mockup's centre column: "Wähle links einen Test" + "Laufendes Modell
 * testen". Replaces the legacy ChallengesTab list.
 *
 * Words: state/section/wait/abort/gate words come from `benchShared`
 * (contract), everything else from `benchHome`.
 *
 * Signature is frozen (contract): no props.
 */

import { useMemo, useState } from "react";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import { useLocale, useTranslations } from "next-intl";
import { Archive, BookOpen, ChevronDown, FlaskConical, Server, X } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { Pill } from "@/components/shared/Pill";
import { benchResultHref, benchTestHref } from "@/lib/benchLinks";
import { benchW1Api, benchW1Keys, type TestListQuery } from "../api";
import type { BenchTestList, BenchTestListItem, Harness, Reason, RunClass, Section } from "../types";

const SECTIONS: Section[] = ["running", "needs_look", "done"];
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

function TestRow({ item }: { item: BenchTestListItem }) {
  const t = useTranslations("benchHome");
  const { ts } = useBenchWords();
  const locale = useLocale();
  const status = useRowStatus()(item);
  const models = item.contestants.map((c) => c.display_name).join(" · ");
  const harnesses = [...new Set(item.contestants.map((c) => c.harness).filter((h): h is Harness => !!h))];
  const cloud = item.contestants.some((c) => c.locality === "cloud");
  return (
    // The hairline sits on the wrapper: on the rounded link it would curve up at the ends.
    <div style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
      <Link
        href={benchResultHref(item.id)}
        data-testid="bench-test-row"
        className="block -mx-2 px-2 py-3 rounded-lg transition-colors hover:bg-[var(--color-bg-hover)]"
      >
        <div className="flex items-baseline justify-between gap-3">
          <span className="text-base font-medium truncate" style={{ color: C.textPrimary }}>
            {item.title}
          </span>
          <span className="text-sm tabular-nums shrink-0" style={{ color: C.textMuted }}>
            {day(item.created_at, locale)}
          </span>
        </div>
        <div className="flex items-center gap-2 mt-1 min-w-0">
          {models && (
            <span className="text-sm truncate min-w-0" style={{ color: C.textSecondary }}>
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

  const header = (
    <div className="flex flex-col gap-1">
      <div className="flex items-center justify-between gap-3">
        <h1 className="display text-2xl font-semibold" style={{ color: C.textPrimary }}>
          {t("title")}
        </h1>
        {!isEmpty && base.isSuccess && (
          <Link href={testHref} data-testid="bench-list-test" className={PRIMARY_BTN} style={{ background: C.accent, color: C.onAccent }}>
            <FlaskConical size={16} aria-hidden />
            {t("testModel")}
          </Link>
        )}
      </div>
      {base.isSuccess && total > 0 && (
        <p className="text-sm tabular-nums" style={{ color: C.textSecondary }} data-testid="bench-list-lead">
          {lastDay ? t("lead", { count: total, date: lastDay }) : t("leadNoDate", { count: total })}
        </p>
      )}
    </div>
  );

  if (isEmpty) {
    return (
      <div data-testid="bench-test-list" className="flex flex-col gap-8">
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
    <div data-testid="bench-test-list" className="lg:grid lg:grid-cols-[minmax(320px,400px)_1fr] lg:gap-12">
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
              {SECTIONS.map((section) => {
                const items = data.items.filter((i) => i.section === section);
                const count = Math.max(data.counts[section] ?? 0, items.length);
                if (count === 0) return null;
                return (
                  <section key={section} data-testid={`bench-section-${section}`} aria-labelledby={`bench-section-h-${section}`}>
                    <div className="flex items-baseline justify-between">
                      <h2 id={`bench-section-h-${section}`} className="text-base font-semibold" style={{ color: C.textPrimary }}>
                        {ts(`section.${section}`)}
                      </h2>
                      <span className="text-sm tabular-nums" style={{ color: C.textMuted }} data-testid="bench-section-count">
                        {count}
                      </span>
                    </div>
                    <div className="flex flex-col">
                      {items.map((item) => (
                        <TestRow key={item.id} item={item} />
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
            <Link
              href="/bench?view=library"
              data-testid="bench-list-library"
              className="inline-flex items-center gap-2 h-11 px-3 rounded-md text-sm hover:bg-[var(--color-bg-hover)]"
              style={{ color: C.textSecondary }}
            >
              <BookOpen size={16} aria-hidden />
              {t("library")}
            </Link>
          </div>
        )}
      </div>

      {/* Desktop centre (mockup 06): nothing selected — pick a row, or test
          what runs now. Rows open their own page (/bench/r/<id>). */}
      <aside className="hidden lg:block pt-32" data-testid="bench-pick-hint">
        <div className="sticky top-8 flex flex-col items-start gap-1 max-w-lg">
          <h2 className="text-lg font-semibold" style={{ color: C.textPrimary }}>
            {t("pickHint.title")}
          </h2>
          <p className="text-sm" style={{ color: C.textSecondary }}>
            {t("pickHint.body")}
          </p>
          <Link
            href={testHref}
            className="inline-flex items-center gap-2 h-11 px-3 -ml-3 mt-1 rounded-md text-sm font-medium hover:bg-[var(--color-bg-hover)]"
            style={{ color: C.textPrimary }}
          >
            <FlaskConical size={16} aria-hidden />
            {t("pickHint.action")}
          </Link>
        </div>
      </aside>
    </div>
  );
}
