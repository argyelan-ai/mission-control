"use client";

// Numbers per class block (concept §5.1/§5.3): class A and class B never in
// one table — each block has its own divider, class B has no speed row at
// all. Median + range from the n runs; a missing HEADLINE number (time,
// tokens out) reads "nicht gemessen: Grund" (K15), every other empty value
// is left out (K3). Phone: a calm property list (K12). Desktop ≥ 1024 px:
// one table per block.
import { Check } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import type { ContestantResult, ResultBlock, Stat } from "../types";
import { formatSeconds, formatTime, formatTokens, statText } from "./format";
import type { BenchWords } from "./words";

interface Cell {
  value: string;
  sub: string | null;
  missing?: boolean;
  /** A passed check (page loads) carries a drawn check mark. */
  check?: boolean;
}

interface Row {
  key: string;
  label: string;
  cell: Cell;
}

function statCell(
  stat: Stat | null,
  fmt: (v: number) => string,
  words: BenchWords,
  headline: boolean,
  compact = false,
): Cell | null {
  const parts = statText(stat, fmt);
  if (!parts) {
    if (!headline || !stat) return null;
    const why = words.reason(stat.reason) ?? words.tr("reason.unknown");
    return { value: words.ts("reason.notMeasured", { reason: why }), sub: null, missing: true };
  }
  const range = parts.min && parts.max ? words.ts("range", { min: parts.min, max: parts.max }) : null;
  if (compact) return { value: parts.median, sub: range };
  const sub =
    parts.n > 1
      ? range
        ? `${words.ts("medianOf", { n: parts.n })} · ${range}`
        : words.ts("medianOf", { n: parts.n })
      : null;
  return { value: parts.median, sub };
}

/** All rows of one contestant, in the fixed order of the mockup.
 *  `compact` (desktop table): range only, short speed note, no page row
 *  (the verdict says it), the cost as one word. */
export function contestantRows(c: ContestantResult, words: BenchWords, compact = false): Row[] {
  const s = c.summary;
  const loc = words.locale;
  const rows: Row[] = [];
  const timeLabel = s.time_kind === "gen_ms" ? words.tr("numbers.timeGen") : words.tr("numbers.timeActive");
  const time = statCell(s.time, (v) => formatTime(v, s.time_kind), words, true, compact);
  if (time) {
    if (s.time_kind === "active_ms" && s.wait_ms !== null) {
      const wait = s.wait_ms > 0 ? words.tr("numbers.waited", { d: formatTime(s.wait_ms, "active_ms") }) : words.tr("numbers.noWait");
      time.sub = time.sub ? `${time.sub} · ${wait}` : wait;
    }
    rows.push({ key: "time", label: timeLabel, cell: time });
  }
  const tokens = statCell(s.tokens_out, (v) => formatTokens(v, loc), words, true, compact);
  if (tokens) {
    const reasoning = statText(s.tokens_reasoning, (v) => formatTokens(v, loc));
    if (!compact && !tokens.missing && reasoning) {
      const think = words.tr("numbers.thinkingPart", { v: reasoning.median });
      tokens.sub = tokens.sub ? `${tokens.sub} · ${think}` : think;
    }
    rows.push({ key: "tokens", label: words.tr("numbers.tokensOut"), cell: tokens });
  }
  if (s.tokens_cache_read !== null) {
    rows.push({
      key: "cache",
      label: words.tr("numbers.cacheRead"),
      cell: { value: formatTokens(s.tokens_cache_read, loc), sub: null },
    });
  }
  // Speed: class A only, and only when measured (not a headline: omitted, K3).
  const decode = statText(s.decode_tok_s, (v) => String(Math.round(v)));
  if (decode) {
    const ttft = statText(s.ttft_ms, (v) => formatSeconds(v, loc));
    rows.push({
      key: "speed",
      label: words.tr("numbers.speed"),
      cell: {
        value: words.tr("numbers.speedValue", { v: decode.median }),
        sub: ttft && !compact ? words.tr("numbers.speedSub", { ttft: ttft.median }) : words.tr("numbers.speedSubShort"),
      },
    });
  }
  const money = words.money(s.cost);
  const short = compact && s.cost.cost_kind === "local";
  rows.push({
    key: "cost",
    label: words.tr("numbers.cost"),
    cell: short ? { value: words.ts("locality.local"), sub: null } : { value: money.value, sub: compact ? null : money.sub },
  });
  if (!compact && (s.page_check === "loads" || s.page_check === "blank")) {
    rows.push({
      key: "page",
      label: words.tr("numbers.page"),
      cell: {
        value: words.tr(s.page_check === "loads" ? "numbers.pageLoads" : "numbers.pageBlank"),
        sub: null,
        check: s.page_check === "loads",
      },
    });
  }
  return rows;
}

function ValueCell({ cell, align = "right" }: { cell: Cell; align?: "right" | "left" }) {
  return (
    <span className={`flex flex-col ${align === "right" ? "items-end text-right" : "items-start text-left"}`}>
      <span
        className="text-sm tabular-nums flex items-center gap-1"
        style={{ color: cell.missing ? STATUS_TEXT.error : C.textPrimary }}
      >
        {cell.value}
        {cell.check && <Check size={14} aria-hidden style={{ color: C.online }} />}
      </span>
      {cell.sub && (
        <span className="text-xs tabular-nums" style={{ color: C.textMuted }}>
          {cell.sub}
        </span>
      )}
    </span>
  );
}

function Chips({ c, words }: { c: ContestantResult; words: BenchWords }) {
  const items = [words.harness(c.harness), words.effort(c)].filter(Boolean) as string[];
  if (items.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {items.map((x) => (
        <span
          key={x}
          className="text-xs px-2 py-1 rounded-full"
          style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
        >
          {x}
        </span>
      ))}
    </div>
  );
}

function whereLine(c: ContestantResult, words: BenchWords): string | null {
  const parts = [c.alias, c.device_name, c.locality ? words.ts(`locality.${c.locality}`) : null].filter(Boolean);
  return parts.length ? parts.join(" · ") : null;
}

function tieLine(c: ContestantResult, words: BenchWords): string | null {
  return c.summary.tie_with.length ? words.tr("numbers.tie", { names: c.summary.tie_with.join(", ") }) : null;
}

export function blockTitle(block: ResultBlock, index: number, total: number, words: BenchWords): string | null {
  if (total < 2) return null;
  const base = words.tr("numbers.divider", { i: index + 1, cls: words.runClass(block.run_class) });
  return block.starts_after ? `${base} ${words.tr("numbers.afterwards")}` : base;
}

function Divider({ text }: { text: string }) {
  return (
    <div className="flex items-center gap-2 mt-4 mb-1">
      <span className="text-xs whitespace-nowrap" style={{ color: C.textMuted }}>
        {text}
      </span>
      <span aria-hidden className="flex-1 h-px" style={{ backgroundColor: C.border }} />
    </div>
  );
}

/** Phone list (and the default in tests). */
export function NumbersList({
  blocks,
  words,
  showNames,
}: {
  blocks: ResultBlock[];
  words: BenchWords;
  showNames: boolean;
}) {
  return (
    <div data-testid="numbers-list" className="flex flex-col">
      {blocks.map((block, bi) => {
        const title = blockTitle(block, bi, blocks.length, words);
        return (
          <div key={`${block.run_class}-${bi}`} data-testid={`block-${block.run_class}`}>
            {title && <Divider text={title} />}
            {block.contestants.map((c) => {
              const where = whereLine(c, words);
              const tie = tieLine(c, words);
              return (
                <div key={c.key} className="flex flex-col gap-2 pt-3">
                  {showNames && (
                    <div className="flex flex-col">
                      <span className="text-sm font-semibold" style={{ color: C.textPrimary }}>
                        {c.display_name}
                      </span>
                      {where && (
                        <span className="text-xs" style={{ color: C.textMuted }}>
                          {where}
                        </span>
                      )}
                    </div>
                  )}
                  <Chips c={c} words={words} />
                  {tie && (
                    <span className="text-xs" style={{ color: C.textSecondary }}>
                      {tie}
                    </span>
                  )}
                  <dl className="flex flex-col">
                    {contestantRows(c, words).map((row) => (
                      <div
                        key={row.key}
                        className="flex items-start justify-between gap-4 py-3"
                        style={{ borderBottom: `1px solid ${C.borderSubtle}` }}
                      >
                        <dt className="text-sm shrink-0" style={{ color: C.textMuted }}>
                          {row.label}
                        </dt>
                        <dd className="min-w-0">
                          <ValueCell cell={row.cell} />
                        </dd>
                      </div>
                    ))}
                  </dl>
                </div>
              );
            })}
          </div>
        );
      })}
    </div>
  );
}

/** Desktop: one table per class block (never one table for A and B). */
export function NumbersTable({
  blocks,
  words,
  showNames,
}: {
  blocks: ResultBlock[];
  words: BenchWords;
  /** false = the verdict already names the model (K3): no model column. */
  showNames: boolean;
}) {
  return (
    <div data-testid="numbers-table" className="flex flex-col">
      {blocks.map((block, bi) => {
        const title = blockTitle(block, bi, blocks.length, words);
        const keys = new Set<string>();
        const perContestant = block.contestants.map((c) => {
          const rows = contestantRows(c, words, true);
          for (const r of rows) keys.add(r.key);
          return { c, rows: new Map(rows.map((r) => [r.key, r])) };
        });
        const order = ["time", "tokens", "cache", "speed", "cost", "page"].filter((k) => keys.has(k));
        const labelOf = (k: string) =>
          perContestant.map((p) => p.rows.get(k)?.label).find(Boolean) ?? k;
        return (
          <div key={`${block.run_class}-${bi}`}>
            {title && <Divider text={title} />}
            <table className="w-full text-sm" style={{ borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ borderBottom: `1px solid ${C.border}` }}>
                  {[...(showNames ? [words.tr("numbers.model")] : []), words.tr("numbers.harness"), words.tr("numbers.effort"), ...order.map(labelOf)].map(
                    (h) => (
                      <th key={h} scope="col" className="text-left text-xs font-normal py-2 pr-4" style={{ color: C.textMuted }}>
                        {h}
                      </th>
                    ),
                  )}
                </tr>
              </thead>
              <tbody>
                {perContestant.map(({ c, rows }) => {
                  const where = whereLine(c, words);
                  return (
                    <tr key={c.key} className="align-top" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
                      {showNames && (
                        <td className="py-3 pr-4">
                          <span className="flex flex-col">
                            <span style={{ color: C.textPrimary }}>{c.display_name}</span>
                            {where && (
                              <span className="text-xs" style={{ color: C.textMuted }}>
                                {where}
                              </span>
                            )}
                          </span>
                        </td>
                      )}
                      <td className="py-3 pr-4" style={{ color: C.textSecondary }}>
                        {words.harness(c.harness)}
                      </td>
                      <td className="py-3 pr-4" style={{ color: C.textSecondary }}>
                        {words.effort(c)}
                      </td>
                      {order.map((k) => {
                        const r = rows.get(k);
                        return (
                          <td key={k} className="py-3 pr-4">
                            {r ? <ValueCell cell={r.cell} align="left" /> : null}
                          </td>
                        );
                      })}
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        );
      })}
    </div>
  );
}
