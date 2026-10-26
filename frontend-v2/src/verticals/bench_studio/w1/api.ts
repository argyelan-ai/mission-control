// Bench W1 API client (contract 24.10.2026) — one function per route in
// backend/app/verticals/bench_studio/w1_routes.py. Packages B/C/D call
// these; nobody adds a second fetch path.
import { BASE_URL, request } from "@/lib/api";
import type {
  AutoTestCreate,
  AutoTestResponse,
  BenchPlan,
  BenchTestCreate,
  BenchTestDetail,
  BenchTestList,
  ContestantList,
  CostConfirmation,
  RerunCostConfirm,
  DownloadToken,
  Harness,
  KitFileName,
  PresetId,
  RunClass,
  RuntimeTestSummary,
  SeenResponse,
  Section,
  XKit,
} from "./types";

const B = "/api/v1/bench";

function qs(params: Record<string, string | null | undefined | boolean>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === null || v === undefined || v === false || v === "") continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

export interface PlanQuery {
  runtimeId?: string | null;
  preset?: PresetId | null;
  templateId?: string | null;
  rerunOf?: string | null;
}

export interface TestListQuery {
  section?: Section | null;
  model?: string | null;
  device?: string | null;
  harness?: Harness | null;
  runClass?: RunClass | null;
  includeArchived?: boolean;
}

/** Query keys — shared so B/C/D invalidate the same caches. */
export const benchW1Keys = {
  plan: (q: PlanQuery) => ["bench-w1", "plan", q] as const,
  contestants: () => ["bench-w1", "contestants"] as const,
  tests: (q: TestListQuery = {}) => ["bench-w1", "tests", q] as const,
  testsAll: () => ["bench-w1", "tests"] as const,
  test: (id: string) => ["bench-w1", "test", id] as const,
  kit: (id: string) => ["bench-w1", "kit", id] as const,
  runtimeSummary: () => ["bench-w1", "runtime-summary"] as const,
};

/** Poll interval for a test that is waiting or running (live run, P4). */
export const LIVE_POLL_MS = 2000;

export const benchW1Api = {
  plan: (q: PlanQuery = {}) =>
    request<BenchPlan>(
      `${B}/plan${qs({
        runtime_id: q.runtimeId,
        preset: q.preset,
        template_id: q.templateId,
        rerun_of: q.rerunOf,
      })}`,
    ),
  contestants: () => request<ContestantList>(`${B}/contestants`),
  tests: {
    create: (body: BenchTestCreate) =>
      request<BenchTestDetail>(`${B}/tests`, { method: "POST", body: JSON.stringify(body) }),
    list: (q: TestListQuery = {}) =>
      request<BenchTestList>(
        `${B}/tests${qs({
          section: q.section,
          model: q.model,
          device: q.device,
          harness: q.harness,
          run_class: q.runClass,
          include_archived: q.includeArchived,
        })}`,
      ),
    get: (id: string) => request<BenchTestDetail>(`${B}/tests/${id}`),
    /** "Nochmal" / truncation re-run. A paid model answers 409
     *  CostConfirmation; the caller repeats with `confirm` (D4). */
    rerun: (id: string, budget: "same" | "larger" = "same", confirm?: RerunCostConfirm) =>
      request<BenchTestDetail>(`${B}/tests/${id}/rerun`, {
        method: "POST",
        body: JSON.stringify({ budget, ...confirm }),
      }),
    stop: (id: string) => request<BenchTestDetail>(`${B}/tests/${id}/stop`, { method: "POST" }),
    /** The result page of a finished test was opened: its Eingang item
     *  "Test fertig" is resolved (idempotent, no side effect). */
    seen: (id: string) => request<SeenResponse>(`${B}/tests/${id}/seen`, { method: "POST" }),
    harvest: (id: string) =>
      request<BenchTestDetail>(`${B}/tests/${id}/harvest`, { method: "POST" }),
    /** The gallery picture (WebP of a run's page) — fetched with the Bearer
     *  header (useAuthBlob); only when the list item says `poster`. */
    posterUrl: (id: string) => `${BASE_URL}${B}/tests/${id}/poster`,
  },
  kit: {
    get: (id: string) => request<XKit>(`${B}/tests/${id}/kit`),
    downloadToken: (id: string) =>
      request<DownloadToken>(`${B}/tests/${id}/kit/download-token`, { method: "POST" }),
    /** A real link (iOS "Save"): the server answers with
     *  Content-Disposition: attachment. Token from downloadToken(). */
    fileUrl: (id: string, name: KitFileName, token: string) =>
      `${BASE_URL}${B}/tests/${id}/kit/files/${name}?token=${encodeURIComponent(token)}`,
  },
  runtimeSummary: () => request<RuntimeTestSummary[]>(`${B}/runtime-summary`),
  autoTests: {
    create: (body: AutoTestCreate) =>
      request<AutoTestResponse>(`${B}/auto-tests`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },
};

/** The CostConfirmation detail of a 409 from tests.create, or null.
 *  request() throws `Error("API 409: <body>")` (lib/api.ts). */
export function parseCostConfirmation(err: unknown): CostConfirmation | null {
  if (!(err instanceof Error)) return null;
  const m = /^API 409: ([\s\S]*)$/.exec(err.message);
  if (!m) return null;
  try {
    const detail = (JSON.parse(m[1]) as { detail?: unknown }).detail as
      | Partial<CostConfirmation>
      | undefined;
    const unknown = detail?.price_unknown === true;
    if (detail && detail.code === "bench_cost_confirmation" && (typeof detail.usd === "number" || unknown)) {
      return {
        code: "bench_cost_confirmation",
        step: detail.step === 2 ? 2 : 1,
        usd: typeof detail.usd === "number" ? detail.usd : null,
        cap_usd: typeof detail.cap_usd === "number" ? detail.cap_usd : 1,
        price_unknown: unknown,
      };
    }
  } catch {
    // no JSON detail
  }
  return null;
}

/** The blockers of a 409 `bench_blocked` from tests.create / rerun
 *  (`{detail: {code, message, blockers: [{code, blocking}]}}`), or null.
 *  The sheet words them through `benchShared.blocker` — never raw JSON. */
export function parseBenchBlocked(err: unknown): { code: string; blocking: boolean }[] | null {
  if (!(err instanceof Error)) return null;
  const m = /^API 409: ([\s\S]*)$/.exec(err.message);
  if (!m) return null;
  try {
    const detail = (JSON.parse(m[1]) as { detail?: unknown }).detail as
      | { code?: unknown; blockers?: unknown }
      | undefined;
    if (detail && detail.code === "bench_blocked" && Array.isArray(detail.blockers)) {
      return detail.blockers
        .filter((b): b is { code: string; blocking?: unknown } => typeof b?.code === "string")
        .map((b) => ({ code: b.code, blocking: b.blocking !== false }));
    }
  } catch {
    // no JSON detail
  }
  return null;
}
