"use client";

import { useState } from "react";
import AppShell from "@/components/layout/AppShell";
import { useQuery } from "@tanstack/react-query";
import { motion } from "framer-motion";
import {
  TrendingUp, DollarSign, Zap, AlertTriangle, BarChart3,
  RefreshCw, ChevronDown, Clock, CheckCircle2, XCircle,
  type LucideIcon,
} from "lucide-react";
import { api } from "@/lib/api";
import { timeAgo } from "@/lib/utils";
import { C } from "@/lib/colors";
import type {
  CostAgentSummary,
  CostSessionSummary,
  IntelligenceAnomaly,
  CostByModel,
  CostTimeseries,
  CostByTask,
} from "@/lib/types";
import {
  BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer,
  PieChart, Pie, Cell, Legend,
  AreaChart, Area, CartesianGrid,
} from "recharts";

// ── Chart helpers (v3 — Tokens only) ─────────────────────────────────────────
const IN_bg = "var(--color-bg-surface)";
const IN_borderSubtle = C.borderSubtle;
const IN_hover = "var(--color-bg-elevated)";

// Tick-Labels: Mono 10px muted; Chart-Farben aus C.chart + C.accent
const CHART_TICK = {
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  fill: "var(--color-text-muted)",
} as const;
const LEGEND_STYLE = {
  fontSize: 10,
  fontFamily: "var(--font-mono)",
  color: "var(--color-text-muted)",
} as const;

const CHART_COLORS = [C.chart.cpu, C.chart.ram, C.chart.disk];

// ── Helpers ───────────────────────────────────────────────────────────────────
function fmtK(n: number) {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return n.toString();
}
function fmtUsd(n: number) {
  return n < 0.01 ? "<$0.01" : `$${n.toFixed(2)}`;
}
function sessionLabel(key: string) {
  // "agent:{id}:task:{taskId}:work" → "task:{taskId}"
  const m = key.match(/task:([^:]+)/);
  return m ? `task:${m[1].slice(0, 8)}` : key.slice(0, 20);
}

// ── Sub-components ────────────────────────────────────────────────────────────
function KPICard({
  label, value, sub, color, valueColor, icon: Icon, hero,
}: {
  label: string; value: string; sub: string; color?: string; valueColor?: string; icon: LucideIcon; hero?: boolean;
}) {
  return (
    <div
      className={`rounded-md p-5${hero ? " corner-ticks" : ""}`}
      style={{ background: "var(--color-bg-surface)", border: `1px solid ${C.border}` }}
    >
      <div className="flex items-center justify-between gap-2 mb-3">
        <span className="label-sys">{label}</span>
        <Icon size={14} style={{ color: color || C.accent }} />
      </div>
      <div
        className="display font-semibold"
        style={{ fontSize: 30, color: valueColor || "var(--color-text-primary)" }}
      >
        {value}
      </div>
      <div className="font-mono text-[10px] mt-1.5" style={{ color: "var(--color-text-muted)" }}>{sub}</div>
    </div>
  );
}

const tooltipStyle = {
  backgroundColor: "var(--color-bg-elevated)",
  border: `1px solid ${C.border}`,
  borderRadius: 4,
  fontSize: 12,
  color: "var(--color-text-primary)",
};

// ── Page ─────────────────────────────────────────────────────────────────────
export default function InsightsPage() {
  const [days, setDays] = useState(30);
  const [activeTab, setActiveTab] = useState<"overview" | "cost" | "performance" | "reports">("overview");

  const { data: insights, isLoading: loadingInsights } = useQuery({
    queryKey: ["intelligence-insights"],
    queryFn: () => api.intelligence.insights(),
    refetchInterval: 60_000,
  });

  const { data: costs, isLoading: loadingCosts } = useQuery({
    queryKey: ["intelligence-costs", days],
    queryFn: () => api.intelligence.costs(days, true),
    refetchInterval: 60_000,
  });

  const { data: reports, isLoading: loadingReports } = useQuery({
    queryKey: ["intelligence-reports"],
    queryFn: () => api.intelligence.reports(5),
    refetchInterval: 120_000,
  });

  const { data: byModel } = useQuery({
    queryKey: ["intelligence-costs-by-model", days],
    queryFn: () => api.intelligence.byModel(days),
    refetchInterval: 60_000,
  });

  const { data: timeseries } = useQuery({
    queryKey: ["intelligence-costs-timeseries", days],
    queryFn: () => api.intelligence.timeseries(days),
    refetchInterval: 60_000,
  });

  const { data: byTask } = useQuery({
    queryKey: ["intelligence-costs-by-task", days],
    queryFn: () => api.intelligence.byTask(days, 10),
    refetchInterval: 60_000,
  });

  const loading = loadingInsights || loadingCosts;

  // Chart data derived from real data
  const agentPerfData = insights?.agent_performance?.map((a) => ({
    name: a.name,
    done: a.done,
    failed: a.failed,
  })) ?? [];

  const agentCostData = costs?.agents?.map((a: CostAgentSummary) => ({
    name: a.agent_name,
    cost: a.cost_usd,
    tokensIn: a.tokens_in,
    tokensOut: a.tokens_out,
  })) ?? [];

  const failureData = Object.entries(insights?.failure_patterns?.patterns ?? {}).map(
    ([name, count]) => ({ name, value: count as number })
  );

  // KPI totals
  const tasksDone = insights?.agent_performance?.reduce((s, a) => s + a.done, 0) ?? 0;
  const tasksFailed = insights?.agent_performance?.reduce((s, a) => s + a.failed, 0) ?? 0;

  // Cache hit rate: cache_read / (cache_read + input) %
  const cacheHitPct = (() => {
    if (!byModel || byModel.length === 0) return null;
    const totalCacheRead = byModel.reduce((s: number, m: CostByModel) => s + m.cache_read_tokens, 0);
    const totalInput = byModel.reduce((s: number, m: CostByModel) => s + m.input_tokens, 0);
    const denom = totalCacheRead + totalInput;
    if (denom === 0) return null;
    return Math.round((totalCacheRead / denom) * 100 * 10) / 10;
  })();

  // Harness split for PieChart
  const harnessData = (() => {
    if (!byModel) return [];
    const map: Record<string, number> = {};
    for (const m of byModel) {
      for (const h of m.harness_list) {
        map[h] = (map[h] ?? 0) + m.cost_usd;
      }
    }
    return Object.entries(map)
      .map(([name, value]) => ({ name, value: Math.round(value * 10000) / 10000 }))
      .sort((a, b) => b.value - a.value);
  })();

  // Colors for the harness split from C tokens (NO new hex values)
  const HARNESS_COLORS: Record<string, string> = {
    "cli-bridge": C.chart.cpu,
    "host": C.chart.ram,
    "sparky": C.chart.disk,
    "backend-ollama": C.info,
  };
  function harnessColor(harness: string, idx: number): string {
    return HARNESS_COLORS[harness] ?? CHART_COLORS[idx % CHART_COLORS.length];
  }

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "cost", label: "Cost" },
    { id: "performance", label: "Performance" },
    { id: "reports", label: "AI Reports" },
  ] as const;

  return (
    <AppShell>
      <div className="max-w-6xl mx-auto">
        {/* Header — v3: .label-sys Micro-Label, Clash Display Titel, Cyan-Messmarke */}
        <div className="mb-6">
          <div className="label-sys mb-2">Performance · Cost · Token Usage · AI Analysis</div>
          <div className="flex flex-col sm:flex-row sm:items-end sm:justify-between gap-2">
            <h1
              className="display text-2xl sm:text-[34px] font-semibold leading-[1.05]"
              style={{ color: "var(--color-text-primary)" }}
            >
              Insights
            </h1>
            <div className="flex items-center gap-3 shrink-0">
              {insights?.analyzed_at && (
                <span className="font-mono text-[10px]" style={{ color: "var(--color-text-muted)" }}>
                  Analyzed {timeAgo(insights.analyzed_at)}
                </span>
              )}
              <div className="relative">
                <select
                  value={days}
                  onChange={(e) => setDays(Number(e.target.value))}
                  aria-label="Select time range"
                  className="appearance-none pl-3 pr-8 py-1.5 font-mono text-[11px] rounded-md cursor-pointer"
                  style={{
                    background: "var(--color-bg-surface)",
                    border: `1px solid ${C.border}`,
                    color: "var(--color-text-secondary)",
                  }}
                >
                  <option value={7}>7 days</option>
                  <option value={30}>30 days</option>
                  <option value={90}>90 days</option>
                </select>
                <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: "var(--color-text-muted)" }} />
              </div>
            </div>
          </div>
          {/* Messmarke: 1px-Linie mit Cyan-Segment — wie Homepage-Header */}
          <div className="relative mt-4 h-px" style={{ backgroundColor: C.border }}>
            <div
              className="absolute left-0 -top-px h-[2px] w-16"
              style={{ backgroundColor: C.accent }}
            />
          </div>
        </div>

        {/* Tabs — eckig, Mono-Labels, aktiver Tab = 2px Cyan-Unterstrich.
            .tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17) */}
        <div className="flex gap-0 border-b mb-6 tab-strip" style={{ borderColor: IN_borderSubtle }}>
          {tabs.map((tab) => {
            const active = activeTab === tab.id;
            return (
              <button
                key={tab.id}
                onClick={() => setActiveTab(tab.id)}
                className="px-4 py-2.5 font-mono text-[11px] uppercase tracking-[0.12em] transition-colors cursor-pointer"
                style={{
                  color: active ? "var(--color-text-primary)" : "var(--color-text-muted)",
                  background: "transparent",
                  borderBottom: `2px solid ${active ? C.accent : "transparent"}`,
                  marginBottom: -1,
                }}
              >
                {tab.label}
              </button>
            );
          })}
        </div>

        {loading && !insights && !costs ? (
          <div className="flex items-center justify-center h-64">
            <RefreshCw size={20} className="animate-spin" style={{ color: C.accent }} />
          </div>
        ) : (
          <>
            {/* ── Tab: Overview ── */}
            {activeTab === "overview" && (
              <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                {/* KPI row */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
                  <KPICard
                    label="Tasks completed"
                    value={String(tasksDone)}
                    sub={`last ${days} days`}
                    icon={CheckCircle2}
                    color={C.online}
                    hero
                  />
                  <KPICard
                    label="Failed"
                    value={String(tasksFailed)}
                    sub="total"
                    icon={XCircle}
                    color={C.error}
                    valueColor={tasksFailed > 0 ? "var(--color-status-error-text)" : undefined}
                  />
                  <KPICard
                    label="Total cost"
                    value={costs ? fmtUsd(costs.total_cost_usd) : "—"}
                    sub={`${fmtK((costs?.total_tokens_in ?? 0) + (costs?.total_tokens_out ?? 0))} tokens`}
                    icon={DollarSign}
                    color={C.accent}
                  />
                  <KPICard
                    label="Anomalies"
                    value={String(insights?.anomalies?.length ?? 0)}
                    icon={AlertTriangle}
                    sub={insights?.anomalies?.some((a: IntelligenceAnomaly) => a.severity === "warning") ? "Warnings active" : "All normal"}
                    color={insights?.anomalies?.length ? C.warning : C.online}
                  />
                </div>

                {/* Agent performance + cost side by side */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                  <div className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="label-sys mb-4">
                      Agent Performance (Tasks)
                    </div>
                    {agentPerfData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={200}>
                        <BarChart data={agentPerfData} barSize={20}>
                          <XAxis dataKey="name" tick={CHART_TICK} axisLine={false} tickLine={false} />
                          <YAxis tick={CHART_TICK} axisLine={false} tickLine={false} width={30} />
                          <Tooltip contentStyle={tooltipStyle} cursor={{ fill: C.accentSubtle }} />
                          <Bar dataKey="done" name="Done" stackId="a" fill={`${C.online}B3`} radius={[0, 0, 0, 0]} />
                          <Bar dataKey="failed" name="Failed" stackId="a" fill={`${C.error}B3`} radius={[2, 2, 0, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyChart message="No performance data" />
                    )}
                  </div>

                  <div className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="label-sys mb-4">
                      Cost per agent (USD)
                    </div>
                    {agentCostData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={200}>
                        <BarChart data={agentCostData} barSize={24} layout="vertical">
                          <XAxis type="number" tick={CHART_TICK} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
                          <YAxis type="category" dataKey="name" tick={CHART_TICK} axisLine={false} tickLine={false} width={60} />
                          <Tooltip contentStyle={tooltipStyle} formatter={(v) => [`$${Number(v).toFixed(4)}`, "Cost"]} />
                          <Bar dataKey="cost" fill={`${C.accent}B3`} radius={[0, 2, 2, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyChart message="No costs recorded yet" />
                    )}
                  </div>
                </div>

                {/* Anomalies */}
                {(insights?.anomalies?.length ?? 0) > 0 && (
                  <div className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="label-sys mb-3">Anomalies</div>
                    <div className="space-y-2">
                      {insights!.anomalies.map((a: IntelligenceAnomaly, i: number) => (
                        <div
                          key={i}
                          className="flex items-start gap-3 p-3 rounded-md"
                          style={{
                            background: a.severity === "warning" ? `${C.warning}0F` : `${C.info}0F`,
                            border: `1px solid ${a.severity === "warning" ? `${C.warning}33` : `${C.info}26`}`,
                          }}
                        >
                          {/* Eckiges Status-Quadrat (8px) statt Icon-Kreis */}
                          <span
                            className="mt-1.5 h-2 w-2 shrink-0 rounded-sm"
                            style={{ background: a.severity === "warning" ? C.warning : C.info }}
                          />
                          <div className="min-w-0">
                            <div className="text-sm" style={{ color: "var(--color-text-secondary)" }}>{a.description}</div>
                            <div className="font-mono text-[10px] mt-1" style={{ color: "var(--color-text-muted)" }}>
                              {a.type}{a.agent_name ? ` · ${a.agent_name}` : ""}
                            </div>
                          </div>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </motion.div>
            )}

            {/* ── Tab: Cost ── */}
            {activeTab === "cost" && (
              <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                {/* Totals */}
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3 mb-5">
                  <KPICard label="Total cost" value={costs ? fmtUsd(costs.total_cost_usd) : "—"} sub={`last ${days} days`} icon={DollarSign} color={C.accent} />
                  <KPICard label="Input Tokens" value={costs ? fmtK(costs.total_tokens_in) : "—"} sub="Prompt tokens" icon={TrendingUp} />
                  <KPICard label="Output Tokens" value={costs ? fmtK(costs.total_tokens_out) : "—"} sub="Completion tokens" icon={Zap} />
                </div>

                {/* Agent table */}
                <div className="rounded-md overflow-hidden mb-4" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                  <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                    <span className="label-sys">
                      Cost per agent
                    </span>
                  </div>
                  {(costs?.agents?.length ?? 0) === 0 ? (
                    <div className="px-5 py-10 text-center text-sm" style={{ color: "var(--color-text-muted)" }}>
                      No cost events recorded yet.
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                    <table className="w-full" style={{ minWidth: 640 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}>
                          {["Agent", "Input Tokens", "Output Tokens", "Events", "Cost USD"].map((h) => (
                            <th key={h} className="label-sys px-5 py-2.5 text-left">
                              {h}
                            </th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {costs!.agents.map((a: CostAgentSummary) => {
                          const pct = costs!.total_cost_usd > 0 ? (a.cost_usd / costs!.total_cost_usd) * 100 : 0;
                          return (
                            <tr key={a.agent_id} className="transition-colors" style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}
                              onMouseEnter={(e) => (e.currentTarget.style.background = IN_hover)}
                              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                            >
                              <td className="px-5 py-3 text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                                {a.agent_name}
                                <div className="h-1 mt-1.5 rounded-sm overflow-hidden" style={{ background: "var(--color-bg-hover)", width: 80 }}>
                                  <div className="h-full rounded-sm" style={{ width: `${pct}%`, background: C.accent }} />
                                </div>
                              </td>
                              <td className="px-5 py-3 text-sm tabular-nums" style={{ color: "var(--color-text-body)" }}>{fmtK(a.tokens_in)}</td>
                              <td className="px-5 py-3 text-sm tabular-nums" style={{ color: "var(--color-text-body)" }}>{fmtK(a.tokens_out)}</td>
                              <td className="px-5 py-3 text-sm tabular-nums" style={{ color: "var(--color-text-muted)" }}>{a.event_count}</td>
                              <td className="px-5 py-3 text-sm font-semibold tabular-nums" style={{ color: C.accent }}>{fmtUsd(a.cost_usd)}</td>
                            </tr>
                          );
                        })}
                      </tbody>
                    </table>
                    </div>
                  )}
                </div>

                {/* Session table */}
                <div className="rounded-md overflow-hidden" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                  <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                    <span className="label-sys">
                      Token usage per session
                    </span>
                    <span className="ml-2 font-mono text-[10px]" style={{ color: "var(--color-text-muted)" }}>Top 100 by cost</span>
                  </div>
                  {(costs?.sessions?.length ?? 0) === 0 ? (
                    <div className="px-5 py-10 text-center text-sm" style={{ color: "var(--color-text-muted)" }}>
                      No session data for this time range.
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                    <table className="w-full" style={{ minWidth: 640 }}>
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}>
                          {["Session", "Agent", "Input", "Output", "Cost", "Last"].map((h) => (
                            <th key={h} className="label-sys px-5 py-2.5 text-left">{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {costs!.sessions!.map((s: CostSessionSummary, i: number) => (
                          <tr key={i} className="transition-colors" style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}
                            onMouseEnter={(e) => (e.currentTarget.style.background = IN_hover)}
                            onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                          >
                            <td className="px-5 py-2.5">
                              <code className="text-[11px] px-1.5 py-0.5 rounded-sm" style={{ background: "var(--color-bg-elevated)", border: `1px solid ${IN_borderSubtle}`, color: C.accent }}>
                                {sessionLabel(s.session_key)}
                              </code>
                            </td>
                            <td className="px-5 py-2.5 text-sm" style={{ color: "var(--color-text-body)" }}>{s.agent_name}</td>
                            <td className="px-5 py-2.5 text-sm tabular-nums" style={{ color: "var(--color-text-body)" }}>{fmtK(s.tokens_in)}</td>
                            <td className="px-5 py-2.5 text-sm tabular-nums" style={{ color: "var(--color-text-body)" }}>{fmtK(s.tokens_out)}</td>
                            <td className="px-5 py-2.5 text-sm font-medium tabular-nums" style={{ color: C.accent }}>{fmtUsd(s.cost_usd)}</td>
                            <td className="px-5 py-2.5 text-xs" style={{ color: "var(--color-text-muted)" }}>
                              {s.last_event_at ? timeAgo(s.last_event_at) : "—"}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                    </div>
                  )}
                </div>

                {/* ── Cache hit rate KPI + harness split counter ── */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4 mb-4">
                  <KPICard
                    label="Cache hit rate"
                    value={cacheHitPct !== null ? `${cacheHitPct}%` : "—"}
                    sub="cache_read / (cache_read + input)"
                    icon={TrendingUp}
                    color={cacheHitPct !== null && cacheHitPct > 40 ? C.online : C.accent}
                  />
                  <KPICard
                    label="Harness-Split"
                    value={harnessData.length > 0 ? `${harnessData.length} types` : "—"}
                    sub={harnessData.map((h) => h.name).join(", ") || "No data"}
                    icon={BarChart3}
                  />
                </div>

                {/* ── Tokens & cost per model ── */}
                <div
                  className="rounded-md overflow-hidden mb-4"
                  style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                >
                  <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                    <span className="label-sys">
                      Tokens &amp; cost per model
                    </span>
                  </div>
                  {(byModel?.length ?? 0) === 0 ? (
                    <div
                      className="px-5 py-10 text-center text-sm"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      No model data for this time range.
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full" style={{ minWidth: 640 }}>
                        <thead>
                          <tr style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}>
                            {[
                              "Model", "Harness", "Input", "Output",
                              "Cache-R", "Cache-W", "Events", "Cost USD",
                            ].map((h) => (
                              <th
                                key={h}
                                className="label-sys px-4 py-2.5 text-left"
                              >
                                {h}
                              </th>
                            ))}
                          </tr>
                        </thead>
                        <tbody>
                          {byModel!.map((m: CostByModel) => (
                            <tr
                              key={m.model}
                              className="transition-colors"
                              style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}
                              onMouseEnter={(e) =>
                                (e.currentTarget.style.background = IN_hover)
                              }
                              onMouseLeave={(e) =>
                                (e.currentTarget.style.background = "transparent")
                              }
                            >
                              <td className="px-4 py-2.5">
                                <code className="text-sm font-mono" style={{ color: C.accent }}>
                                  {m.model}
                                </code>
                              </td>
                              <td className="px-4 py-2.5">
                                <div className="flex flex-wrap gap-1">
                                  {m.harness_list.map((h, idx) => (
                                    <span
                                      key={h}
                                      className="font-mono text-[10px] px-1.5 py-0.5 rounded-sm"
                                      style={{
                                        backgroundColor: `${harnessColor(h, idx)}1F`,
                                        color: harnessColor(h, idx),
                                      }}
                                    >
                                      {h}
                                    </span>
                                  ))}
                                </div>
                              </td>
                              <td
                                className="px-4 py-2.5 text-sm tabular-nums"
                                style={{ color: "var(--color-text-body)" }}
                              >
                                {m.input_tokens.toLocaleString("de-CH")}
                              </td>
                              <td
                                className="px-4 py-2.5 text-sm tabular-nums"
                                style={{ color: "var(--color-text-body)" }}
                              >
                                {m.output_tokens.toLocaleString("de-CH")}
                              </td>
                              <td
                                className="px-4 py-2.5 text-sm tabular-nums"
                                style={{ color: "var(--color-text-muted)" }}
                              >
                                {m.cache_read_tokens.toLocaleString("de-CH")}
                              </td>
                              <td
                                className="px-4 py-2.5 text-sm tabular-nums"
                                style={{ color: "var(--color-text-muted)" }}
                              >
                                {m.cache_write_tokens.toLocaleString("de-CH")}
                              </td>
                              <td
                                className="px-4 py-2.5 text-sm tabular-nums"
                                style={{ color: "var(--color-text-muted)" }}
                              >
                                {m.event_count}
                              </td>
                              <td
                                className="px-4 py-2.5 text-sm font-semibold tabular-nums"
                                style={{ color: C.accent }}
                              >
                                {fmtUsd(m.cost_usd)}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </div>

                {/* ── Cost per day (AreaChart) ── */}
                <div
                  className="rounded-md p-5 mb-4"
                  style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                >
                  <div
                    className="label-sys mb-4"
                  >
                    Cost per day (USD)
                  </div>
                  {(timeseries?.length ?? 0) === 0 ? (
                    <EmptyChart message="No time series data" />
                  ) : (
                    <ResponsiveContainer width="100%" height={200}>
                      <AreaChart
                        data={timeseries as CostTimeseries[]}
                        margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                      >
                        <defs>
                          <linearGradient id="costGradient" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor={C.accent} stopOpacity={0.1} />
                            <stop offset="95%" stopColor={C.accent} stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid
                          strokeDasharray="3 3"
                          stroke={C.borderSubtle}
                        />
                        <XAxis
                          dataKey="date"
                          tick={CHART_TICK}
                          axisLine={false}
                          tickLine={false}
                          tickFormatter={(v: string) => v.slice(5)}
                        />
                        <YAxis
                          tick={CHART_TICK}
                          axisLine={false}
                          tickLine={false}
                          width={45}
                          tickFormatter={(v: number) => `$${v}`}
                        />
                        <Tooltip
                          contentStyle={tooltipStyle}
                          formatter={(v: number) => [`$${v.toFixed(4)}`, "Cost"]}
                        />
                        <Area
                          type="monotone"
                          dataKey="cost_usd"
                          name="Cost"
                          stroke={C.accent}
                          strokeWidth={1.5}
                          fill="url(#costGradient)"
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  )}
                </div>

                {/* ── Most expensive tasks + harness split ── */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  {/* Most expensive tasks */}
                  <div
                    className="rounded-md overflow-hidden"
                    style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                  >
                    <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                      <span className="label-sys">
                        Most expensive tasks (Top 10)
                      </span>
                    </div>
                    {(byTask?.length ?? 0) === 0 ? (
                      <div
                        className="px-5 py-8 text-center text-sm"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        No task data.
                      </div>
                    ) : (
                      <div className="divide-y" style={{ borderColor: IN_borderSubtle }}>
                        {(byTask as CostByTask[]).map((t, i) => (
                          <div
                            key={t.task_id}
                            className="px-5 py-2.5 flex items-center gap-3 transition-colors"
                            onMouseEnter={(e) =>
                              (e.currentTarget.style.background = IN_hover)
                            }
                            onMouseLeave={(e) =>
                              (e.currentTarget.style.background = "transparent")
                            }
                          >
                            <span
                              className="text-[11px] tabular-nums font-mono shrink-0 w-5"
                              style={{ color: "var(--color-text-muted)" }}
                            >
                              {i + 1}.
                            </span>
                            <div className="flex-1 min-w-0">
                              <div
                                className="text-sm truncate"
                                style={{ color: "var(--color-text-body)" }}
                              >
                                {t.task_title}
                              </div>
                              <div className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                                {t.event_count} events ·{" "}
                                {t.input_tokens.toLocaleString("de-CH")} tokens
                              </div>
                            </div>
                            <span
                              className="text-sm font-semibold tabular-nums shrink-0"
                              style={{ color: C.accent }}
                            >
                              {fmtUsd(t.cost_usd)}
                            </span>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  {/* Harness split PieChart */}
                  <div
                    className="rounded-md p-5"
                    style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                  >
                    <div
                      className="label-sys mb-4"
                    >
                      Harness split (by cost)
                    </div>
                    {harnessData.length === 0 ? (
                      <EmptyChart message="No harness data" />
                    ) : (
                      <ResponsiveContainer width="100%" height={200}>
                        <PieChart>
                          <Pie
                            data={harnessData}
                            dataKey="value"
                            nameKey="name"
                            cx="50%"
                            cy="50%"
                            outerRadius={70}
                            innerRadius={35}
                          >
                            {harnessData.map((entry, i) => (
                              <Cell key={entry.name} fill={harnessColor(entry.name, i)} />
                            ))}
                          </Pie>
                          <Tooltip
                            contentStyle={tooltipStyle}
                            formatter={(v: number) => [`$${v.toFixed(4)}`, "Cost"]}
                          />
                          <Legend wrapperStyle={LEGEND_STYLE} />
                        </PieChart>
                      </ResponsiveContainer>
                    )}
                  </div>
                </div>
              </motion.div>
            )}

            {/* ── Tab: Performance ── */}
            {activeTab === "performance" && (
              <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                  <div className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="label-sys mb-4">Done vs. Failed</div>
                    {agentPerfData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <BarChart data={agentPerfData}>
                          <XAxis dataKey="name" tick={CHART_TICK} axisLine={false} tickLine={false} />
                          <YAxis tick={CHART_TICK} axisLine={false} tickLine={false} width={30} />
                          <Tooltip contentStyle={tooltipStyle} cursor={{ fill: C.accentSubtle }} />
                          <Legend wrapperStyle={LEGEND_STYLE} />
                          <Bar dataKey="done" name="Done" stackId="a" fill={`${C.online}B3`} />
                          <Bar dataKey="failed" name="Failed" stackId="a" fill={`${C.error}B3`} radius={[2, 2, 0, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : <EmptyChart message="No data" />}
                  </div>

                  <div className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="label-sys mb-4">Failure patterns</div>
                    {failureData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <PieChart>
                          <Pie data={failureData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={80} innerRadius={50}>
                            {failureData.map((_, i) => (
                              <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                            ))}
                          </Pie>
                          <Tooltip contentStyle={tooltipStyle} />
                          <Legend wrapperStyle={LEGEND_STYLE} />
                        </PieChart>
                      </ResponsiveContainer>
                    ) : <EmptyChart message="No failure patterns" />}
                  </div>
                </div>

                {/* Avg task duration per agent */}
                {insights?.task_durations?.per_agent && (
                  <div className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="label-sys mb-4">
                      Ø Task duration per agent (minutes)
                    </div>
                    <ResponsiveContainer width="100%" height={200}>
                      <BarChart
                        data={Object.entries(insights.task_durations.per_agent).map(([name, mins]) => ({ name, mins: Math.round(Number(mins)) }))}
                        layout="vertical"
                      >
                        <XAxis type="number" tick={CHART_TICK} axisLine={false} tickLine={false} unit="min" />
                        <YAxis type="category" dataKey="name" tick={CHART_TICK} axisLine={false} tickLine={false} width={60} />
                        <Tooltip contentStyle={tooltipStyle} formatter={(v) => [`${v} min`, "Ø duration"]} />
                        <Bar dataKey="mins" fill={`${C.chart.ram}B3`} radius={[0, 2, 2, 0]} />
                      </BarChart>
                    </ResponsiveContainer>
                  </div>
                )}
              </motion.div>
            )}

            {/* ── Tab: AI Reports ── */}
            {activeTab === "reports" && (
              <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                {loadingReports ? (
                  <div className="flex items-center justify-center h-40">
                    <RefreshCw size={18} className="animate-spin" style={{ color: C.accent }} />
                  </div>
                ) : (reports?.length ?? 0) === 0 ? (
                  <div className="flex flex-col items-center justify-center py-20 gap-3">
                    <BarChart3 size={32} style={{ color: "var(--color-text-muted)" }} />
                    <p className="text-sm" style={{ color: "var(--color-text-muted)" }}>
                      No AI analyses yet. The intelligence service runs daily.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-4">
                    {reports!.map((r) => (
                      <div key={r.id} className="rounded-md p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                        <div className="flex items-center gap-2 mb-3">
                          <Clock size={12} style={{ color: "var(--color-text-muted)" }} />
                          <span className="font-mono text-[10px]" style={{ color: "var(--color-text-muted)" }}>{timeAgo(r.created_at)}</span>
                          {r.title && (
                            <span className="text-sm font-semibold ml-2" style={{ color: "var(--color-text-primary)" }}>{r.title}</span>
                          )}
                        </div>
                        <div className="text-sm leading-relaxed whitespace-pre-wrap" style={{ color: "var(--color-text-body)" }}>
                          {r.content}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </motion.div>
            )}
          </>
        )}
      </div>
    </AppShell>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="flex items-center justify-center h-[200px] text-sm" style={{ color: "var(--color-text-muted)" }}>
      {message}
    </div>
  );
}
