"use client";

/**
 * Bench W1 P1 — which Runtimes cards get "Testen" (GET /bench/runtime-summary).
 *
 * Core page → bench only through lib/benchLinks (ADR-044 §4). One query for
 * the whole page (live cards and cloud rows share the cache entry); the key
 * is the vertical's `benchW1Keys.runtimeSummary()` written out here, because
 * core may not import from src/verticals — a vertical test pins the equality.
 * A stripped build (benchEnabled = false) sends no request and shows nothing.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { benchEnabled, fetchBenchRuntimeSummary, type BenchRuntimeSummary } from "@/lib/benchLinks";

export const BENCH_RUNTIME_SUMMARY_KEY = ["bench-w1", "runtime-summary"] as const;

export function useBenchRuntimeSummary(): Map<string, BenchRuntimeSummary> {
  const { data } = useQuery({
    queryKey: BENCH_RUNTIME_SUMMARY_KEY,
    queryFn: fetchBenchRuntimeSummary,
    enabled: benchEnabled,
    staleTime: 15_000,
    // A started test or a model that finished warming up changes the
    // button ("Testen ab 22:27" → "Testen") — the live poll cadence.
    refetchInterval: 30_000,
    retry: false,
  });
  return useMemo(() => new Map((data ?? []).map((row) => [row.runtime_id, row])), [data]);
}
