"use client";

/**
 * Insights → "Details": everything the old Cost / Performance / AI reports
 * tabs showed, folded away so the story above stays short. One time range
 * (7 / 30 / 90 days) for all of them. A panel only renders — and the
 * session list only loads — once it is opened.
 *
 *   Details                                  ( 7 | 30 | 90 days )
 *   By model                    6 models · cache hits 91 %       ⌄
 *   Most expensive tasks        top 10                           ⌄
 *   By agent                    14 agents                        ⌄
 *   All sessions                top 100 by cost                  ⌄
 *   Page views                  this week                        ⌄
 *   AI reports                  last 41 days ago                 ⌄
 */

import { useState, type ReactNode } from "react";
import Link from "next/link";
import { useLocale, useTranslations } from "next-intl";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown } from "lucide-react";
import { api } from "@/lib/api";
import { C } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { formatShare, formatUsd, topRoutes } from "@/lib/usage";
import { formatTokens } from "@/lib/insights";
import type { CostByModel } from "@/lib/types";
import { Unfold } from "@/components/ui/Unfold";
import { CappedList } from "@/components/shared/CappedList";
import { MarkdownContent } from "@/components/chat/MarkdownContent";

const RANGES = [7, 30, 90] as const;

/** cache_read / (cache_read + input) over all models; null without input. */
export function cacheHitRate(models: CostByModel[]): number | null {
  const read = models.reduce((s, m) => s + m.cache_read_tokens, 0);
  const input = models.reduce((s, m) => s + m.input_tokens, 0);
  return read + input > 0 ? read / (read + input) : null;
}

/** Cost per harness, biggest first (a model can run under several harnesses). */
export function harnessCosts(models: CostByModel[]): [string, number][] {
  const map: Record<string, number> = {};
  for (const m of models) for (const h of m.harness_list) map[h] = (map[h] ?? 0) + m.cost_usd;
  return Object.entries(map).sort((a, b) => b[1] - a[1]);
}

export function InsightsDetails() {
  const t = useTranslations("insights.details");
  const locale = useLocale();
  const [days, setDays] = useState<number>(30);
  const [open, setOpen] = useState<string | null>(null);
  const toggle = (id: string) => setOpen((o) => (o === id ? null : id));

  const byModel = useQuery({ queryKey: ["intelligence-costs-by-model", days], queryFn: () => api.intelligence.byModel(days) });
  const byTask = useQuery({ queryKey: ["intelligence-costs-by-task", days], queryFn: () => api.intelligence.byTask(days, 10) });
  const costs = useQuery({ queryKey: ["intelligence-costs", days, false], queryFn: () => api.intelligence.costs(days, false) });
  const sessions = useQuery({
    queryKey: ["intelligence-costs", days, true],
    queryFn: () => api.intelligence.costs(days, true),
    enabled: open === "sessions",
  });
  const pages = useQuery({ queryKey: ["usage", "pages", 1], queryFn: () => api.usage.pages(1) });
  const reports = useQuery({ queryKey: ["intelligence-reports"], queryFn: () => api.intelligence.reports(5) });

  const models = byModel.data ?? [];
  const hit = cacheHitRate(models);
  const agents = (costs.data?.agents ?? []).filter((a) => a.event_count > 0);
  const routes = topRoutes(pages.data?.weeks[pages.data.weeks.length - 1], 20);
  const latest = reports.data?.[0];

  return (
    <section data-region="details" aria-labelledby="details-heading">
      <div className="flex items-center justify-between gap-3">
        <div className="min-w-0">
          <h2 id="details-heading" className="text-base font-semibold" style={{ color: C.textPrimary }}>{t("title")}</h2>
          <p className="text-xs" style={{ color: C.textMuted }}>{t("hint")}</p>
        </div>
        <div role="radiogroup" aria-label={t("rangeAria")} className="inline-flex rounded-full p-1 shrink-0" style={{ border: `1px solid ${C.border}` }}>
          {RANGES.map((d) => (
            <button
              key={d}
              type="button"
              role="radio"
              aria-checked={days === d}
              aria-label={t("days", { days: d })}
              onClick={() => setDays(d)}
              className="min-h-11 min-w-11 px-3 rounded-full text-sm tabular-nums cursor-pointer transition-colors"
              style={{ background: days === d ? C.bgElevated : "transparent", color: days === d ? C.textPrimary : C.textSecondary }}
            >
              {d}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-3" style={{ borderTop: `1px solid ${C.border}` }}>
        <Panel
          id="models"
          title={t("models")}
          summary={models.length ? (hit !== null ? t("modelsSummary", { count: models.length, rate: formatShare(hit, locale) ?? "" }) : t("modelsSummaryNoCache", { count: models.length })) : null}
          open={open}
          onToggle={toggle}
        >
          {models.length === 0 ? <Empty text={t("empty")} /> : (
            <>
              <ul>
                {models.map((m) => (
                  <Line
                    key={m.model}
                    main={<span className="font-mono text-sm break-all" style={{ color: C.textPrimary }}>{m.model}</span>}
                    meta={`${m.harness_list.join(", ")} · ${t("modelTokens", {
                      input: formatTokens(m.input_tokens, locale),
                      output: formatTokens(m.output_tokens, locale),
                      cacheRead: formatTokens(m.cache_read_tokens, locale),
                    })}`}
                    value={formatUsd(m.cost_usd, locale)}
                  />
                ))}
              </ul>
              <h3 className="mt-4 text-sm font-medium" style={{ color: C.textPrimary }}>{t("harnesses")}</h3>
              <ul>
                {harnessCosts(models).map(([h, cost]) => (
                  <Line key={h} main={<span className="font-mono text-sm" style={{ color: C.textSecondary }}>{h}</span>} value={formatUsd(cost, locale)} />
                ))}
              </ul>
            </>
          )}
        </Panel>

        <Panel
          id="tasks"
          title={t("tasks")}
          summary={byTask.data?.length ? t("tasksSummary", { count: byTask.data.length }) : null}
          open={open}
          onToggle={toggle}
        >
          {!byTask.data?.length ? <Empty text={t("empty")} /> : (
            <ol>
              {byTask.data.map((task) => (
                <Line
                  key={task.task_id}
                  main={
                    <Link href={`/tasks?task=${encodeURIComponent(task.task_id)}`} className="text-sm hover:underline" style={{ color: C.textPrimary }}>
                      {task.task_title}
                    </Link>
                  }
                  meta={t("taskMeta", { events: task.event_count, tokens: formatTokens(task.input_tokens, locale) })}
                  value={formatUsd(task.cost_usd, locale)}
                />
              ))}
            </ol>
          )}
        </Panel>

        <Panel
          id="agents"
          title={t("agents")}
          summary={agents.length ? t("agentsSummary", { count: agents.length }) : null}
          open={open}
          onToggle={toggle}
        >
          {agents.length === 0 ? <Empty text={t("empty")} /> : (
            <ul>
              {agents.map((a) => (
                <Line
                  key={a.agent_id}
                  main={<span className="text-sm" style={{ color: C.textPrimary }}>{a.agent_name}</span>}
                  meta={t("agentMeta", { input: formatTokens(a.tokens_in, locale), output: formatTokens(a.tokens_out, locale), events: a.event_count })}
                  value={formatUsd(a.cost_usd, locale)}
                />
              ))}
            </ul>
          )}
        </Panel>

        <Panel id="sessions" title={t("sessions")} summary={t("sessionsSummary", { count: 100 })} open={open} onToggle={toggle}>
          {sessions.isLoading ? null : !sessions.data?.sessions?.length ? <Empty text={t("empty")} /> : (
            <CappedList maxRows={20} fadeTo={C.bgBase} className="gap-0">
              {sessions.data.sessions.map((s) => (
                <Line
                  key={s.session_key}
                  main={<span className="font-mono text-sm" style={{ color: C.textPrimary }}>{sessionLabel(s.session_key)}</span>}
                  meta={`${t("sessionMeta", { agent: s.agent_name, input: formatTokens(s.tokens_in, locale), output: formatTokens(s.tokens_out, locale) })}${s.last_event_at ? ` · ${timeAgo(s.last_event_at, locale)}` : ""}`}
                  value={formatUsd(s.cost_usd, locale)}
                  as="div"
                />
              ))}
            </CappedList>
          )}
        </Panel>

        <Panel id="pages" title={t("pages")} summary={t("pagesSummary")} open={open} onToggle={toggle}>
          {routes.length === 0 ? <Empty text={t("empty")} /> : (
            <ul>
              {routes.map(([route, count]) => (
                <Line key={route} main={<span className="font-mono text-sm" style={{ color: C.textSecondary }}>{route}</span>} value={String(count)} />
              ))}
            </ul>
          )}
        </Panel>

        <Panel
          id="reports"
          title={t("reports")}
          summary={latest ? t("reportsSummary", { ago: timeAgo(latest.created_at, locale) }) : null}
          open={open}
          onToggle={toggle}
        >
          {!reports.data?.length ? <Empty text={t("noReports")} /> : (
            <div className="space-y-6">
              {reports.data.map((r) => (
                <article key={r.id}>
                  <p className="text-xs" style={{ color: C.textMuted }}>{timeAgo(r.created_at, locale)}</p>
                  <div className="mt-1 max-w-prose" style={{ color: C.textSecondary }}>
                    <MarkdownContent content={r.content} compact />
                  </div>
                </article>
              ))}
            </div>
          )}
        </Panel>
      </div>
    </section>
  );
}

function sessionLabel(key: string) {
  // "agent:{id}:task:{taskId}:work" → "task:{taskId}"
  const m = key.match(/task:([^:]+)/);
  return m ? `task:${m[1].slice(0, 8)}` : key.slice(0, 20);
}

function Panel({
  id,
  title,
  summary,
  open,
  onToggle,
  children,
}: {
  id: string;
  title: string;
  summary: string | null;
  open: string | null;
  onToggle: (id: string) => void;
  children: ReactNode;
}) {
  const isOpen = open === id;
  return (
    <div style={{ borderBottom: `1px solid ${C.border}` }} data-testid={`details-${id}`}>
      <button
        type="button"
        onClick={() => onToggle(id)}
        aria-expanded={isOpen}
        aria-controls={`details-panel-${id}`}
        className="w-full min-h-12 flex items-center gap-3 text-left cursor-pointer"
      >
        <span className="text-sm flex-1 min-w-0" style={{ color: C.textPrimary }}>{title}</span>
        {summary && <span className="text-xs text-right truncate" style={{ color: C.textMuted }}>{summary}</span>}
        <ChevronDown
          size={16}
          aria-hidden
          className="shrink-0 transition-transform"
          style={{ color: C.textMuted, transform: isOpen ? "rotate(180deg)" : undefined }}
        />
      </button>
      <Unfold open={isOpen}>
        <div id={`details-panel-${id}`} className="pb-4">{children}</div>
      </Unfold>
    </div>
  );
}

function Line({ main, meta, value, as = "li" }: { main: ReactNode; meta?: string; value: string; as?: "li" | "div" }) {
  const Tag = as;
  return (
    <Tag className="flex items-start justify-between gap-3 py-2" style={{ borderBottom: `1px solid ${C.borderSubtle}` }}>
      <div className="min-w-0">
        {main}
        {meta && <p className="mt-1 text-xs tabular-nums" style={{ color: C.textMuted }}>{meta}</p>}
      </div>
      <span className="font-mono text-sm tabular-nums shrink-0" style={{ color: C.textPrimary }}>{value}</span>
    </Tag>
  );
}

function Empty({ text }: { text: string }) {
  return <p className="py-2 text-sm" style={{ color: C.textSecondary }}>{text}</p>;
}
