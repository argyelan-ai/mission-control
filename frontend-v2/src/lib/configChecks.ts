// Config checks — the frontend side of the one check registry (ADR-093 §2).
//
// Every settings page reads the latest result of a key/bot/connection from
// GET /config-checks (one call, shared cache) and runs a check through
// POST /config-checks/{kind}/{subject}/run. The result and its age come from
// the database, so "tested · 3 min ago" survives a reload and a restart.
import { useMutation, useQuery, useQueryClient, type QueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { ConfigCheckItem, ConfigCheckRunResult, ConfigChecksResponse } from "@/lib/types";

export const CONFIG_CHECKS_KEY = ["config-checks"] as const;

function stamp(item: ConfigCheckItem): ConfigCheckItem {
  return { ...item, received_at_ms: Date.now() };
}

/** All registry items with their latest result ("never tested" included). */
export function useConfigChecks() {
  return useQuery<ConfigChecksResponse>({
    queryKey: CONFIG_CHECKS_KEY,
    queryFn: async () => {
      const res = await api.configChecks.list();
      return { ...res, items: (res.items ?? []).map(stamp) };
    },
    staleTime: 30_000,
    retry: false,
  });
}

export function findCheck(
  items: ConfigCheckItem[] | undefined,
  kind: string,
  subjectId: string,
): ConfigCheckItem | undefined {
  return items?.find((i) => i.kind === kind && i.subject_id === subjectId);
}

/** The check that tests a given secret key (a key field looks itself up). */
export function findSecretCheck(
  items: ConfigCheckItem[] | undefined,
  secretKey: string,
): ConfigCheckItem | undefined {
  return (
    items?.find((i) => i.kind === "secret" && i.subject_id === secretKey) ??
    items?.find((i) => i.secret_key === secretKey)
  );
}

/** Put one fresh item into the shared list without refetching everything. */
export function mergeCheck(qc: QueryClient, item: ConfigCheckItem) {
  const fresh = stamp(item);
  qc.setQueryData<ConfigChecksResponse>(CONFIG_CHECKS_KEY, (old) => {
    if (!old) return old;
    const rest = old.items.filter(
      (i) => !(i.kind === fresh.kind && i.subject_id === fresh.subject_id),
    );
    return { ...old, items: [...rest, fresh] };
  });
}

/** Re-read the list — after an older test button stored a result. */
export function refreshChecks(qc: QueryClient) {
  return qc.invalidateQueries({ queryKey: CONFIG_CHECKS_KEY });
}

export interface RunCheckVars {
  kind: string;
  subjectId: string;
}

/** Run one check now (on save, or on the operator's click). */
export function useRunCheck() {
  const qc = useQueryClient();
  return useMutation<ConfigCheckRunResult, Error, RunCheckVars>({
    mutationFn: ({ kind, subjectId }) => api.configChecks.run(kind, subjectId),
    onSuccess: async (res) => {
      // A list that was never loaded cannot take a merge — load it instead.
      if (qc.getQueryData(CONFIG_CHECKS_KEY)) mergeCheck(qc, res.item);
      else await refreshChecks(qc);
    },
  });
}

export function isRunning(
  run: { isPending: boolean; variables?: RunCheckVars },
  kind: string,
  subjectId: string,
): boolean {
  return run.isPending && run.variables?.kind === kind && run.variables?.subjectId === subjectId;
}

/** Age of a result in seconds, still counting after it arrived. */
export function currentAgeSeconds(item: ConfigCheckItem, nowMs: number): number | null {
  if (item.age_seconds == null) return null;
  const since = item.received_at_ms != null ? Math.max(0, nowMs - item.received_at_ms) / 1000 : 0;
  return Math.max(0, Math.floor(item.age_seconds + since));
}

/** Age buckets in fixed forms (DESIGN K10): just now · N min · N h · N days. */
export function ageBucket(seconds: number): { key: "justNow" | "minutes" | "hours" | "days"; n: number } {
  if (seconds < 60) return { key: "justNow", n: 0 };
  if (seconds < 3600) return { key: "minutes", n: Math.floor(seconds / 60) };
  if (seconds < 86400) return { key: "hours", n: Math.floor(seconds / 3600) };
  return { key: "days", n: Math.floor(seconds / 86400) };
}
