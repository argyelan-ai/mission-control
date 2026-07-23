// Vertical-owned API namespace (ADR-044 §4). Uses the core request() helper
// (auth header + BASE_URL) — vertical -> core imports are allowed.
import { BASE_URL, request } from "@/lib/api";
import type {
  BenchChallenge,
  BenchChallengeCreate,
  BenchEntry,
  PromptTemplate,
  SparkModelsStatus,
} from "./types";

export const benchApi = {
  /** Bench #21: live Spark model list for the vanilla row's select in
   *  NewChallengeDialog — always resolves (reachable: false on error). */
  sparkModels: {
    get: () => request<SparkModelsStatus>("/api/v1/bench/spark-models"),
  },
  challenges: {
    list: (includeArchived = false) =>
      request<BenchChallenge[]>(
        `/api/v1/bench/challenges${includeArchived ? "?include_archived=true" : ""}`
      ),
    get: (id: string) => request<BenchChallenge>(`/api/v1/bench/challenges/${id}`),
    create: (body: BenchChallengeCreate) =>
      request<BenchChallenge>("/api/v1/bench/challenges", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    draft: (
      id: string,
      body: { tweet_text: string; include_speed_labels: boolean; board_id?: string | null }
    ) =>
      request<{ approval_id: string; challenge_status: string; warnings: string[] }>(
        `/api/v1/bench/challenges/${id}/draft`,
        { method: "POST", body: JSON.stringify(body) }
      ),
    rerender: (id: string) =>
      request<{ ok: boolean }>(`/api/v1/bench/challenges/${id}/rerender`, {
        method: "POST",
      }),
    stop: (id: string) =>
      request<BenchChallenge>(`/api/v1/bench/challenges/${id}/stop`, {
        method: "POST",
      }),
    archive: (id: string) =>
      request<BenchChallenge>(`/api/v1/bench/challenges/${id}/archive`, {
        method: "POST",
      }),
    unarchive: (id: string) =>
      request<BenchChallenge>(`/api/v1/bench/challenges/${id}/unarchive`, {
        method: "POST",
      }),
    remove: (id: string) =>
      request<void>(`/api/v1/bench/challenges/${id}`, { method: "DELETE" }),
    update: (id: string, body: { title: string }) =>
      request<BenchChallenge>(`/api/v1/bench/challenges/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    recompose: (id: string) =>
      request<{ ok: boolean }>(`/api/v1/bench/challenges/${id}/recompose`, {
        method: "POST",
      }),
  },
  entries: {
    retry: (id: string) =>
      request<{ ok: boolean }>(`/api/v1/bench/entries/${id}/retry`, {
        method: "POST",
      }),
    /** Re-records ONLY this entry from its existing artifact, then
     *  recomposes the challenge — cheaper than challenges.rerender() when
     *  just one model's video looks off. Rate-limited server-side (60s
     *  cooldown per entry) — throws with the backend's "try again in Ns"
     *  detail on 429. */
    rerender: (id: string) =>
      request<{ ok: boolean }>(`/api/v1/bench/entries/${id}/rerender`, {
        method: "POST",
      }),
    update: (id: string, body: { model_label?: string; display_tag?: string }) =>
      request<BenchEntry>(`/api/v1/bench/entries/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    /** Mints a short-lived (30 min), resource-scoped view-token for
     *  entryViewUrl below — requires a full operator session (Bearer/JWT via
     *  the core request() helper), unlike the view URL itself. */
    viewToken: (challengeId: string, entryId: string) =>
      request<{ token: string; expires_in: number }>(
        `/api/v1/bench/challenges/${challengeId}/entries/${entryId}/view-token`,
        { method: "POST" }
      ),
  },
  // Prompt Library CRUD (core API from PR 2)
  promptTemplates: {
    list: () => request<PromptTemplate[]>("/api/v1/prompt-templates"),
    create: (body: { title: string; body: string; tags: string[] }) =>
      request<PromptTemplate>("/api/v1/prompt-templates", {
        method: "POST",
        body: JSON.stringify(body),
      }),
    update: (id: string, body: Partial<{ title: string; body: string; tags: string[] }>) =>
      request<PromptTemplate>(`/api/v1/prompt-templates/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
    remove: (id: string) =>
      request<void>(`/api/v1/prompt-templates/${id}`, { method: "DELETE" }),
  },
  /** Absolute /shared-deliverables path -> subpath for the core files API
   *  ("shared-deliverables" root, see backend fs_roots.py). */
  sharedSubpath: (absPath: string) => absPath.replace(/^\/shared-deliverables\//, ""),
  /** Builds the URL for a rendered entry's index.html — a real interactive
   *  page, works on mobile. `<a href>` can't carry a Bearer header, so auth
   *  rides in the query string, but NEVER the operator's session JWT: this
   *  link is copyable/shareable by design (mobile "Öffnen"), and a full
   *  session token in a shared/history'd URL would be a standing admin
   *  credential leak. Pass the short-lived token from entries.viewToken()
   *  instead — backend additionally sandboxes the response (CSP `sandbox`,
   *  opaque origin) so the model-generated page can never read this app's
   *  localStorage. */
  entryViewUrl: (challengeId: string, entryId: string, viewToken: string) =>
    `${BASE_URL}/api/v1/bench/challenges/${challengeId}/entries/${entryId}/view?token=${viewToken}`,
};
