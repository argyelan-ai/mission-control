// Vertical-owned API namespace (ADR-044 §4). Uses the core request() helper
// (auth header + BASE_URL) — vertical -> core imports are allowed.
import { BASE_URL, getToken, request } from "@/lib/api";
import type {
  BenchChallenge,
  BenchChallengeCreate,
  BenchEntry,
  PromptTemplate,
} from "./types";

export const benchApi = {
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
    update: (id: string, body: { model_label?: string; display_tag?: string }) =>
      request<BenchEntry>(`/api/v1/bench/entries/${id}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),
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
  /** Opens a rendered entry's index.html as a real interactive page (new
   *  tab, works on mobile). `<a href>` can't carry a Bearer header, so the
   *  token rides in the query string — same fallback require_user already
   *  offers WS/stream URLs opened bare (frontend-v2/src/lib/sse.ts pattern).
   *  Backend sandboxes the response (CSP `sandbox`, opaque origin) so the
   *  model-generated page can never read this app's localStorage/token. */
  entryViewUrl: (challengeId: string, entryId: string) =>
    `${BASE_URL}/api/v1/bench/challenges/${challengeId}/entries/${entryId}/view?token=${getToken()}`,
};
