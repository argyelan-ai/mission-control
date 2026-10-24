// Bench W1 API client (contract 24.10.2026) — one function per route in
// backend/app/verticals/bench_studio/w1_routes.py. Frozen with the
// contract: packages B/C/D call these, nobody adds a second fetch path.
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
  DownloadToken,
  Harness,
  KitFileName,
  PresetId,
  RunClass,
  RuntimeTestSummary,
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
    rerun: (id: string, budget: "same" | "larger" = "same") =>
      request<BenchTestDetail>(`${B}/tests/${id}/rerun`, {
        method: "POST",
        body: JSON.stringify({ budget }),
      }),
    stop: (id: string) => request<BenchTestDetail>(`${B}/tests/${id}/stop`, { method: "POST" }),
    harvest: (id: string) =>
      request<BenchTestDetail>(`${B}/tests/${id}/harvest`, { method: "POST" }),
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
    if (detail && detail.code === "bench_cost_confirmation" && typeof detail.usd === "number") {
      return {
        code: "bench_cost_confirmation",
        step: detail.step === 2 ? 2 : 1,
        usd: detail.usd,
        cap_usd: typeof detail.cap_usd === "number" ? detail.cap_usd : 1,
      };
    }
  } catch {
    // no JSON detail
  }
  return null;
}
