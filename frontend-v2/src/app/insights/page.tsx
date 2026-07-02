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

// ── Chart helpers ─────────────────────────────────────────────────────────────
const IN_chartAxis = C.textMuted;
const IN_bg = "rgba(255,255,255,0.03)";
const IN_borderSubtle = C.borderSubtle;

const CHART_COLORS = [C.accent, C.info, C.online, C.warning, C.error];

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
  label, value, sub, color, icon: Icon,
}: {
  label: string; value: string; sub: string; color?: string; icon: LucideIcon;
}) {
  return (
    <div
      className="rounded-2xl p-5"
      style={{ background: IN_bg, border: `1px solid ${C.border}` }}
    >
      <div className="flex items-center gap-2 mb-3">
        <Icon size={14} style={{ color: color || C.accent }} />
        <span className="text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--color-text-muted)" }}>
          {label}
        </span>
      </div>
      <div className="text-3xl font-bold tracking-tight" style={{ color: color || "var(--color-text-primary)" }}>
        {value}
      </div>
      <div className="text-xs mt-1.5" style={{ color: "var(--color-text-muted)" }}>{sub}</div>
    </div>
  );
}

const tooltipStyle = {
  backgroundColor: C.bgBase,
  border: `1px solid ${C.border}`,
  borderRadius: 8,
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

  // Cache-Hit-Quote: cache_read / (cache_read + input) %
  const cacheHitPct = (() => {
    if (!byModel || byModel.length === 0) return null;
    const totalCacheRead = byModel.reduce((s: number, m: CostByModel) => s + m.cache_read_tokens, 0);
    const totalInput = byModel.reduce((s: number, m: CostByModel) => s + m.input_tokens, 0);
    const denom = totalCacheRead + totalInput;
    if (denom === 0) return null;
    return Math.round((totalCacheRead / denom) * 100 * 10) / 10;
  })();

  // Harness-Split fuer PieChart
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

  // Farben fuer Harness-Split aus C-Tokens (KEINE neuen Hex-Werte)
  const HARNESS_COLORS: Record<string, string> = {
    "cli-bridge": C.accent,
    "host": C.info,
    "sparky": C.online,
    "backend-ollama": C.warning,
  };
  function harnessColor(harness: string, idx: number): string {
    return HARNESS_COLORS[harness] ?? CHART_COLORS[idx % CHART_COLORS.length];
  }

  const tabs = [
    { id: "overview", label: "Übersicht" },
    { id: "cost", label: "💰 Kosten" },
    { id: "performance", label: "Performance" },
    { id: "reports", label: "KI-Reports" },
  ] as const;

  return (
    <AppShell>
      <div className="max-w-6xl mx-auto">
        {/* Header */}
        <div className="flex items-end justify-between mb-6">
          <div>
            <h1 className="text-2xl font-bold tracking-tight" style={{ color: "var(--color-text-primary)" }}>
              Insights
            </h1>
            <p className="text-sm mt-1" style={{ color: "var(--color-text-muted)" }}>
              Performance · Kosten · Token-Verbrauch · KI-Analyse
            </p>
          </div>
          <div className="flex items-center gap-3">
            {insights?.analyzed_at && (
              <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>
                Analysiert {timeAgo(insights.analyzed_at)}
              </span>
            )}
            <div className="relative">
              <select
                value={days}
                onChange={(e) => setDays(Number(e.target.value))}
                aria-label="Zeitraum auswählen"
                className="appearance-none pl-3 pr-8 py-1.5 text-xs rounded-lg cursor-pointer"
                style={{
                  background: IN_bg,
                  border: `1px solid ${C.border}`,
                  color: "var(--color-text-secondary)",
                }}
              >
                <option value={7}>7 Tage</option>
                <option value={30}>30 Tage</option>
                <option value={90}>90 Tage</option>
              </select>
              <ChevronDown size={12} className="absolute right-2 top-1/2 -translate-y-1/2 pointer-events-none" style={{ color: "var(--color-text-muted)" }} />
            </div>
          </div>
        </div>

        {/* Tabs — .tab-strip: mobile horizontal scroll + edge-fade (MOBILE-SPEC M17) */}
        <div className="flex gap-0 border-b mb-6 tab-strip" style={{ borderColor: IN_borderSubtle }}>
          {tabs.map((tab) => (
            <button
              key={tab.id}
              onClick={() => setActiveTab(tab.id)}
              className="px-4 py-2.5 text-sm font-medium transition-all cursor-pointer"
              style={{
                color: activeTab === tab.id ? "var(--color-text-primary)" : "var(--color-text-muted)",
                borderBottom: `2px solid ${activeTab === tab.id ? C.accent : "transparent"}`,
                marginBottom: -1,
                background: "transparent",
                border: "none",
                borderBottomStyle: "solid",
                borderBottomWidth: 2,
                borderBottomColor: activeTab === tab.id ? C.accent : "transparent",
              }}
            >
              {tab.label}
            </button>
          ))}
        </div>

        {loading && !insights && !costs ? (
          <div className="flex items-center justify-center h-64">
            <RefreshCw size={20} className="animate-spin" style={{ color: C.accent }} />
          </div>
        ) : (
          <>
            {/* ── Tab: Übersicht ── */}
            {activeTab === "overview" && (
              <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
                {/* KPI row */}
                <div className="grid grid-cols-2 sm:grid-cols-4 gap-3 mb-5">
                  <KPICard
                    label="Tasks erledigt"
                    value={String(insights?.agent_performance?.reduce((s, a) => s + a.done, 0) ?? 0)}
                    sub={`letzte ${days} Tage`}
                    icon={CheckCircle2}
                    color={C.online}
                  />
                  <KPICard
                    label="Fehlgeschlagen"
                    value={String(insights?.agent_performance?.reduce((s, a) => s + a.failed, 0) ?? 0)}
                    sub="gesamt"
                    icon={XCircle}
                    color={C.error}
                  />
                  <KPICard
                    label="Gesamtkosten"
                    value={costs ? fmtUsd(costs.total_cost_usd) : "—"}
                    sub={`${fmtK((costs?.total_tokens_in ?? 0) + (costs?.total_tokens_out ?? 0))} Tokens`}
                    icon={DollarSign}
                    color={C.accent}
                  />
                  <KPICard
                    label="Anomalien"
                    value={String(insights?.anomalies?.length ?? 0)}
                    icon={AlertTriangle}
                    sub={insights?.anomalies?.some((a: IntelligenceAnomaly) => a.severity === "warning") ? "⚠ Warnungen aktiv" : "Alles normal"}
                    color={insights?.anomalies?.length ? C.warning : C.online}
                  />
                </div>

                {/* Agent performance + cost side by side */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 mb-4">
                  <div className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="text-xs font-semibold mb-4" style={{ color: "var(--color-text-secondary)" }}>
                      Agent Performance (Tasks)
                    </div>
                    {agentPerfData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={200}>
                        <BarChart data={agentPerfData} barSize={20}>
                          <XAxis dataKey="name" tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} />
                          <YAxis tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} width={30} />
                          <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
                          <Bar dataKey="done" name="Erledigt" stackId="a" fill="rgba(0,204,136,0.6)" radius={[0, 0, 0, 0]} />
                          <Bar dataKey="failed" name="Fehlgeschlagen" stackId="a" fill="rgba(239,68,68,0.6)" radius={[4, 4, 0, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyChart message="Keine Performance-Daten" />
                    )}
                  </div>

                  <div className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="text-xs font-semibold mb-4" style={{ color: "var(--color-text-secondary)" }}>
                      Kosten pro Agent (USD)
                    </div>
                    {agentCostData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={200}>
                        <BarChart data={agentCostData} barSize={24} layout="vertical">
                          <XAxis type="number" tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} tickFormatter={(v) => `$${v}`} />
                          <YAxis type="category" dataKey="name" tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} width={60} />
                          <Tooltip contentStyle={tooltipStyle} formatter={(v) => [`$${Number(v).toFixed(4)}`, "Kosten"]} />
                          <Bar dataKey="cost" fill={`${C.accent}B3`} radius={[0, 4, 4, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : (
                      <EmptyChart message="Noch keine Kosten erfasst" />
                    )}
                  </div>
                </div>

                {/* Anomalies */}
                {(insights?.anomalies?.length ?? 0) > 0 && (
                  <div className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="text-xs font-semibold mb-3" style={{ color: "var(--color-text-secondary)" }}>Anomalien</div>
                    <div className="space-y-2">
                      {insights!.anomalies.map((a: IntelligenceAnomaly, i: number) => (
                        <div
                          key={i}
                          className="flex items-start gap-3 p-3 rounded-xl"
                          style={{
                            background: a.severity === "warning" ? `${C.warning}0F` : `${C.info}0F`,
                            border: `1px solid ${a.severity === "warning" ? `${C.warning}33` : `${C.info}26`}`,
                          }}
                        >
                          <AlertTriangle size={14} style={{ color: a.severity === "warning" ? C.warning : C.info, marginTop: 1, flexShrink: 0 }} />
                          <div>
                            <div className="text-sm" style={{ color: "var(--color-text-body)" }}>{a.description}</div>
                            {a.agent_name && (
                              <div className="text-xs mt-0.5" style={{ color: "var(--color-text-muted)" }}>{a.agent_name}</div>
                            )}
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
                  <KPICard label="Gesamtkosten" value={costs ? fmtUsd(costs.total_cost_usd) : "—"} sub={`letzte ${days} Tage`} icon={DollarSign} color={C.accent} />
                  <KPICard label="Input Tokens" value={costs ? fmtK(costs.total_tokens_in) : "—"} sub="Prompt-Tokens" icon={TrendingUp} />
                  <KPICard label="Output Tokens" value={costs ? fmtK(costs.total_tokens_out) : "—"} sub="Completion-Tokens" icon={Zap} />
                </div>

                {/* Agent table */}
                <div className="rounded-2xl overflow-hidden mb-4" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                  <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                    <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--color-text-secondary)" }}>
                      Kosten pro Agent
                    </span>
                  </div>
                  {(costs?.agents?.length ?? 0) === 0 ? (
                    <div className="px-5 py-10 text-center text-sm" style={{ color: "var(--color-text-muted)" }}>
                      Noch keine Kosten-Events erfasst.
                    </div>
                  ) : (
                    <table className="w-full">
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}>
                          {["Agent", "Input Tokens", "Output Tokens", "Events", "Kosten USD"].map((h) => (
                            <th key={h} className="px-5 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--color-text-muted)" }}>
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
                              onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.02)")}
                              onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                            >
                              <td className="px-5 py-3 text-sm font-medium" style={{ color: "var(--color-text-primary)" }}>
                                {a.agent_name}
                                <div className="h-1 mt-1.5 rounded-full overflow-hidden" style={{ background: "rgba(255,255,255,0.06)", width: 80 }}>
                                  <div className="h-full rounded-full" style={{ width: `${pct}%`, background: `linear-gradient(90deg, ${C.accent}, ${C.accentHover})` }} />
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
                  )}
                </div>

                {/* Session table */}
                <div className="rounded-2xl overflow-hidden" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                  <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                    <span className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--color-text-secondary)" }}>
                      Token-Verbrauch pro Session
                    </span>
                    <span className="ml-2 text-[10px]" style={{ color: "var(--color-text-muted)" }}>Top 100 nach Kosten</span>
                  </div>
                  {(costs?.sessions?.length ?? 0) === 0 ? (
                    <div className="px-5 py-10 text-center text-sm" style={{ color: "var(--color-text-muted)" }}>
                      Keine Session-Daten für diesen Zeitraum.
                    </div>
                  ) : (
                    <table className="w-full">
                      <thead>
                        <tr style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}>
                          {["Session", "Agent", "Input", "Output", "Kosten", "Zuletzt"].map((h) => (
                            <th key={h} className="px-5 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider" style={{ color: "var(--color-text-muted)" }}>{h}</th>
                          ))}
                        </tr>
                      </thead>
                      <tbody>
                        {costs!.sessions!.map((s: CostSessionSummary, i: number) => (
                          <tr key={i} className="transition-colors" style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}
                            onMouseEnter={(e) => (e.currentTarget.style.background = "rgba(255,255,255,0.02)")}
                            onMouseLeave={(e) => (e.currentTarget.style.background = "transparent")}
                          >
                            <td className="px-5 py-2.5">
                              <code className="text-[11px] px-1.5 py-0.5 rounded" style={{ background: "rgba(255,255,255,0.06)", color: C.accent }}>
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
                  )}
                </div>

                {/* ── Cache-Hit-Quote KPI + Harness-Split Zaehler ── */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-3 mt-4 mb-4">
                  <KPICard
                    label="Cache-Hit-Quote"
                    value={cacheHitPct !== null ? `${cacheHitPct}%` : "—"}
                    sub="cache_read / (cache_read + input)"
                    icon={TrendingUp}
                    color={cacheHitPct !== null && cacheHitPct > 40 ? C.online : C.accent}
                  />
                  <KPICard
                    label="Harness-Split"
                    value={harnessData.length > 0 ? `${harnessData.length} Typen` : "—"}
                    sub={harnessData.map((h) => h.name).join(", ") || "Keine Daten"}
                    icon={BarChart3}
                  />
                </div>

                {/* ── Tokens & Kosten pro Modell ── */}
                <div
                  className="rounded-2xl overflow-hidden mb-4"
                  style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                >
                  <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                    <span
                      className="text-xs font-semibold uppercase tracking-wider"
                      style={{ color: "var(--color-text-secondary)" }}
                    >
                      Tokens &amp; Kosten pro Modell
                    </span>
                  </div>
                  {(byModel?.length ?? 0) === 0 ? (
                    <div
                      className="px-5 py-10 text-center text-sm"
                      style={{ color: "var(--color-text-muted)" }}
                    >
                      Keine Modell-Daten fuer diesen Zeitraum.
                    </div>
                  ) : (
                    <div className="overflow-x-auto">
                      <table className="w-full" style={{ minWidth: 640 }}>
                        <thead>
                          <tr style={{ borderBottom: `1px solid ${IN_borderSubtle}` }}>
                            {[
                              "Modell", "Harness", "Input", "Output",
                              "Cache-R", "Cache-W", "Events", "Kosten USD",
                            ].map((h) => (
                              <th
                                key={h}
                                className="px-4 py-2.5 text-left text-[11px] font-semibold uppercase tracking-wider"
                                style={{ color: "var(--color-text-muted)" }}
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
                                (e.currentTarget.style.background = "rgba(255,255,255,0.02)")
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
                                      className="text-[10px] px-1.5 py-0.5 rounded"
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

                {/* ── Kosten pro Tag (AreaChart) ── */}
                <div
                  className="rounded-2xl p-5 mb-4"
                  style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                >
                  <div
                    className="text-xs font-semibold mb-4"
                    style={{ color: "var(--color-text-secondary)" }}
                  >
                    Kosten pro Tag (USD)
                  </div>
                  {(timeseries?.length ?? 0) === 0 ? (
                    <EmptyChart message="Keine Zeitreihen-Daten" />
                  ) : (
                    <ResponsiveContainer width="100%" height={200}>
                      <AreaChart
                        data={timeseries as CostTimeseries[]}
                        margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                      >
                        <defs>
                          <linearGradient id="costGradient" x1="0" y1="0" x2="0" y2="1">
                            <stop offset="5%" stopColor={C.accent} stopOpacity={0.3} />
                            <stop offset="95%" stopColor={C.accent} stopOpacity={0} />
                          </linearGradient>
                        </defs>
                        <CartesianGrid
                          strokeDasharray="3 3"
                          stroke="rgba(255,255,255,0.04)"
                        />
                        <XAxis
                          dataKey="date"
                          tick={{ fontSize: 10, fill: IN_chartAxis }}
                          axisLine={false}
                          tickLine={false}
                          tickFormatter={(v: string) => v.slice(5)}
                        />
                        <YAxis
                          tick={{ fontSize: 10, fill: IN_chartAxis }}
                          axisLine={false}
                          tickLine={false}
                          width={45}
                          tickFormatter={(v: number) => `$${v}`}
                        />
                        <Tooltip
                          contentStyle={tooltipStyle}
                          formatter={(v: number) => [`$${v.toFixed(4)}`, "Kosten"]}
                        />
                        <Area
                          type="monotone"
                          dataKey="cost_usd"
                          name="Kosten"
                          stroke={C.accent}
                          strokeWidth={1.5}
                          fill="url(#costGradient)"
                        />
                      </AreaChart>
                    </ResponsiveContainer>
                  )}
                </div>

                {/* ── Teuerste Tasks + Harness-Split ── */}
                <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
                  {/* Teuerste Tasks */}
                  <div
                    className="rounded-2xl overflow-hidden"
                    style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                  >
                    <div className="px-5 py-4 border-b" style={{ borderColor: IN_borderSubtle }}>
                      <span
                        className="text-xs font-semibold uppercase tracking-wider"
                        style={{ color: "var(--color-text-secondary)" }}
                      >
                        Teuerste Tasks (Top 10)
                      </span>
                    </div>
                    {(byTask?.length ?? 0) === 0 ? (
                      <div
                        className="px-5 py-8 text-center text-sm"
                        style={{ color: "var(--color-text-muted)" }}
                      >
                        Keine Task-Daten.
                      </div>
                    ) : (
                      <div className="divide-y" style={{ borderColor: IN_borderSubtle }}>
                        {(byTask as CostByTask[]).map((t, i) => (
                          <div
                            key={t.task_id}
                            className="px-5 py-2.5 flex items-center gap-3 transition-colors"
                            onMouseEnter={(e) =>
                              (e.currentTarget.style.background = "rgba(255,255,255,0.02)")
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
                                {t.event_count} Events ·{" "}
                                {t.input_tokens.toLocaleString("de-CH")} Tokens
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

                  {/* Harness-Split PieChart */}
                  <div
                    className="rounded-2xl p-5"
                    style={{ background: IN_bg, border: `1px solid ${C.border}` }}
                  >
                    <div
                      className="text-xs font-semibold mb-4"
                      style={{ color: "var(--color-text-secondary)" }}
                    >
                      Harness-Split (nach Kosten)
                    </div>
                    {harnessData.length === 0 ? (
                      <EmptyChart message="Keine Harness-Daten" />
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
                            formatter={(v: number) => [`$${v.toFixed(4)}`, "Kosten"]}
                          />
                          <Legend
                            wrapperStyle={{
                              fontSize: 11,
                              color: "var(--color-text-secondary)",
                            }}
                          />
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
                  <div className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="text-xs font-semibold mb-4" style={{ color: "var(--color-text-secondary)" }}>Erledigt vs. Fehlgeschlagen</div>
                    {agentPerfData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <BarChart data={agentPerfData}>
                          <XAxis dataKey="name" tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} />
                          <YAxis tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} width={30} />
                          <Tooltip contentStyle={tooltipStyle} cursor={{ fill: "rgba(255,255,255,0.03)" }} />
                          <Legend wrapperStyle={{ fontSize: 11, color: C.textSecondary }} />
                          <Bar dataKey="done" name="Erledigt" stackId="a" fill="rgba(0,204,136,0.7)" />
                          <Bar dataKey="failed" name="Fehlgeschlagen" stackId="a" fill="rgba(239,68,68,0.7)" radius={[4, 4, 0, 0]} />
                        </BarChart>
                      </ResponsiveContainer>
                    ) : <EmptyChart message="Keine Daten" />}
                  </div>

                  <div className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="text-xs font-semibold mb-4" style={{ color: "var(--color-text-secondary)" }}>Fehler-Muster</div>
                    {failureData.length > 0 ? (
                      <ResponsiveContainer width="100%" height={240}>
                        <PieChart>
                          <Pie data={failureData} dataKey="value" nameKey="name" cx="50%" cy="50%" outerRadius={80} innerRadius={50}>
                            {failureData.map((_, i) => (
                              <Cell key={i} fill={CHART_COLORS[i % CHART_COLORS.length]} />
                            ))}
                          </Pie>
                          <Tooltip contentStyle={tooltipStyle} />
                          <Legend wrapperStyle={{ fontSize: 11, color: C.textSecondary }} />
                        </PieChart>
                      </ResponsiveContainer>
                    ) : <EmptyChart message="Keine Fehler-Muster" />}
                  </div>
                </div>

                {/* Avg task duration per agent */}
                {insights?.task_durations?.per_agent && (
                  <div className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                    <div className="text-xs font-semibold mb-4" style={{ color: "var(--color-text-secondary)" }}>
                      Ø Task-Dauer pro Agent (Minuten)
                    </div>
                    <ResponsiveContainer width="100%" height={200}>
                      <BarChart
                        data={Object.entries(insights.task_durations.per_agent).map(([name, mins]) => ({ name, mins: Math.round(Number(mins)) }))}
                        layout="vertical"
                      >
                        <XAxis type="number" tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} unit="min" />
                        <YAxis type="category" dataKey="name" tick={{ fontSize: 11, fill: IN_chartAxis }} axisLine={false} tickLine={false} width={60} />
                        <Tooltip contentStyle={tooltipStyle} formatter={(v) => [`${v} min`, "Ø Dauer"]} />
                        <Bar dataKey="mins" fill={`${C.info}B3`} radius={[0, 4, 4, 0]} />
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
                      Noch keine KI-Analysen. Intelligence-Service läuft täglich.
                    </p>
                  </div>
                ) : (
                  <div className="space-y-4">
                    {reports!.map((r) => (
                      <div key={r.id} className="rounded-2xl p-5" style={{ background: IN_bg, border: `1px solid ${C.border}` }}>
                        <div className="flex items-center gap-2 mb-3">
                          <Clock size={12} style={{ color: "var(--color-text-muted)" }} />
                          <span className="text-xs" style={{ color: "var(--color-text-muted)" }}>{timeAgo(r.created_at)}</span>
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
