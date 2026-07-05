import type {
  ActivityEvent,
  Agent,
  AgentMetrics,
  AgentSkillsResponse,
  AgentTemplate,
  Approval,
  Board,
  BoardGroup,
  BoardMemory,
  BrowserLiveTarget,
  Credential,
  CostOverview,
  DiscordChannel,
  HenrySessionState,
  IntelligenceConfig,
  IntelligenceInsights,
  Loop,
  LoopCreate,
  LoopDetail,
  LoopUpdate,
  Meeting,
  MeetingMessage,
  MetricsHistoryResponse,
  ModelCatalog,
  ModelInfo,
  OpenClawSkill,
  Playbook,
  PlaybookCatalogItem,
  PlaybookRunProjection,
  PlaybookVersion,
  PlannerMessage,
  ProviderTemplate,
  Project,
  ProjectPhase,
  ProjectGitInfo,
  ResearchSaveResponse,
  ResearchStartResponse,
  SkillCandidate,
  SkillPack,
  ScheduledJob,
  ScheduledJobCreate,
  ScheduledJobRun,
  ScheduleJobStats,
  ScheduleHeatmapCell,
  ScheduleUpcomingFiring,
  ScheduleFiringPreview,
  SecretEntry,
  SkillsResponse,
  SystemMetrics,
  SystemStatus,
  Automation,
  Tag,
  Task,
  TaskChecklistItem,
  TaskComment,
  TaskDependencyInfo,
  TaskEvent,
  TaskGitInfo,
  CommitDiff,
  TaskPipelineResponse,
  Runtime,
  RuntimesResponse,
  RuntimeActionResult,
  RuntimeSchedule,
  RuntimeScheduleCreate,
  RuntimeScheduleRun,
  LMStudioModel,
  LMStudioModelsResponse,
  LMSCatalogModel,
  HFRepoInfo,
  Repo,
  RepoImportCandidate,
  RepoUpdate,
  GithubStatus,
  GithubConfigStatus,
  GithubConfigUpdate,
  ReferenceFile,
  Host,
  HostCreate,
  HostMetrics,
  SparkMetrics,
  CliGlobalSession,
  CliPlugin,
  GithubSkillRepo,
  InstallRequestBody,
  InstallRequestResponse,
  MCPServer,
  VaultNote,
  VaultNotesListResponse,
  VaultSearchResponse,
  VaultNoteDetail,
  VaultTrackViewResponse,
  VaultGraphResponse,
  ModelPrice,
  ModelPriceCreate,
  ModelPriceUpdate,
  UnmatchedModel,
  CostByModel,
  CostTimeseries,
  CostByTask,
  FsRoot,
  FsEntry,
  FsMeta,
  FsSearchResult,
  TrashEntry,
} from "./types";

export const BASE_URL = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");

export const AUTH_TOKEN_KEY = "mc_auth_token";
export const USER_INFO_KEY = "mc_user";

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return localStorage.getItem(AUTH_TOKEN_KEY) ?? "";
}

export function clearToken() {
  if (typeof window !== "undefined") {
    localStorage.removeItem(AUTH_TOKEN_KEY);
    localStorage.removeItem(USER_INFO_KEY);
  }
}

export function getStoredUser(): { id: string; email: string; name: string; role: string } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = localStorage.getItem(USER_INFO_KEY);
    return raw ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

export function setStoredUser(user: { id: string; email: string; name: string; role: string }) {
  if (typeof window !== "undefined") {
    localStorage.setItem(USER_INFO_KEY, JSON.stringify(user));
  }
}

export async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    cache: "no-store",
    headers: {
      "Content-Type": "application/json",
      Authorization: `Bearer ${getToken()}`,
      ...init?.headers,
    },
  });

  // Auto-redirect on 401 (expired JWT or invalid token)
  if (res.status === 401 && typeof window !== "undefined" && !path.includes("/auth/")) {
    clearToken();
    window.location.href = "/login";
    throw new Error("Session abgelaufen");
  }

  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(`API ${res.status}: ${text}`);
  }

  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

// Unauthenticated request (for login/register)
async function publicRequest<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${BASE_URL}${path}`, {
    ...init,
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (!res.ok) {
    const text = await res.text().catch(() => res.statusText);
    throw new Error(text);
  }
  return res.json() as Promise<T>;
}

// ── API ─────────────────────────────────────────────────────────────────────

interface AuthUser { id: string; email: string; name: string; role: string }
interface TokenResponse { access_token: string; token_type: string; user: AuthUser }

export const api = {
  // ── Auth ──────────────────────────────────────────────────────────────────
  auth: {
    setupRequired: () => publicRequest<{ setup_required: boolean }>("/api/v1/auth/setup-required"),
    login: (email: string, password: string) =>
      publicRequest<TokenResponse>("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ email, password }) }),
    register: (email: string, name: string, password: string) =>
      publicRequest<TokenResponse>("/api/v1/auth/register", { method: "POST", body: JSON.stringify({ email, name, password }) }),
    me: () => request<AuthUser & { preferred_name?: string; avatar_url?: string; timezone?: string }>("/api/v1/auth/me"),
    updateProfile: (data: { name?: string; preferred_name?: string; timezone?: string; current_password?: string; new_password?: string }) =>
      request<AuthUser & { preferred_name?: string; avatar_url?: string; timezone?: string }>("/api/v1/auth/me", { method: "PATCH", body: JSON.stringify(data) }),
    users: {
      list: () => request<(AuthUser & { is_active: boolean; has_password: boolean; created_at: string })[]>("/api/v1/auth/users"),
      create: (data: { email: string; name: string; password: string; role?: string }) =>
        request<AuthUser>("/api/v1/auth/users", { method: "POST", body: JSON.stringify(data) }),
      update: (id: string, data: { name?: string; role?: string; is_active?: boolean; password?: string }) =>
        request<AuthUser>(`/api/v1/auth/users/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    },
  },

  // ── System ──────────────────────────────────────────────────────────────────
  system: {
    version: () =>
      request<{ current: string; latest: string | null; release_url: string | null; update_available: boolean }>(
        "/api/v1/system/version",
      ),
    status: () => request<SystemStatus>("/api/v1/system/status"),
    metrics: () => request<SystemMetrics>("/api/v1/system/metrics"),
    metricsHistory: () => request<MetricsHistoryResponse>("/api/v1/system/metrics/history"),
    mode: () => request<import("./types").SystemModeMeta>("/api/v1/system/mode"),
    setMode: (mode: import("./types").SystemMode, reason: string = "") =>
      request<import("./types").SystemModeMeta>("/api/v1/system/mode", {
        method: "PUT",
        body: JSON.stringify({ mode, reason }),
      }),
  },

  // ── Intelligence ──────────────────────────────────────────────────────────────
  intelligence: {
    insights: () => request<IntelligenceInsights>("/api/v1/intelligence/insights"),
    costs: (days = 30, includeSessions = false) =>
      request<CostOverview>(`/api/v1/intelligence/costs?days=${days}&include_sessions=${includeSessions}`),
    reports: (limit?: number) => request<BoardMemory[]>(`/api/v1/intelligence/reports${limit ? `?limit=${limit}` : ""}`),
    config: () => request<IntelligenceConfig>("/api/v1/intelligence/config"),
    updateConfig: (data: IntelligenceConfig) =>
      request<IntelligenceConfig>("/api/v1/intelligence/config", { method: "PUT", body: JSON.stringify(data) }),
    trigger: () => request<{ analyzed_at: string }>("/api/v1/intelligence/trigger", { method: "POST" }),
    byModel: (days = 30) =>
      request<CostByModel[]>(`/api/v1/intelligence/costs/by-model?days=${days}`),
    timeseries: (days = 30) =>
      request<CostTimeseries[]>(`/api/v1/intelligence/costs/timeseries?days=${days}`),
    byTask: (days = 30, limit = 10) =>
      request<CostByTask[]>(`/api/v1/intelligence/costs/by-task?days=${days}&limit=${limit}`),
  },

  // ── Files (global filesystem browser, /api/v1/files/*) ──────────────────────
  // Browses deliverables / workspaces / vault / … as named roots. Binaries
  // (`/content`) are fetched via `fetchBlob` because <img>/<iframe> can't carry
  // a Bearer header — same gotcha as knowledge.getAttachmentUrl.
  files: {
    roots: () =>
      request<{ roots: FsRoot[]; native_open_available: boolean }>("/api/v1/files/roots"),

    list: (root: string, subpath?: string) => {
      const qs = new URLSearchParams({ root });
      if (subpath) qs.set("subpath", subpath);
      return request<{ root: string; subpath: string; entries: FsEntry[] }>(
        `/api/v1/files/list?${qs.toString()}`,
      );
    },

    search: (params: {
      q: string; type?: string; agent?: string; root?: string; limit?: number;
    }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(
          Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]),
        ),
      ).toString();
      return request<{ results: FsSearchResult[] }>(`/api/v1/files/search?${qs}`);
    },

    meta: (root: string, subpath: string) => {
      const qs = new URLSearchParams({ root, subpath }).toString();
      return request<FsMeta>(`/api/v1/files/meta?${qs}`);
    },

    /** BASE-prefixed URL to the raw bytes. The `<img>`/`<iframe>` element can't
     *  carry a Bearer header, so binary consumers use `fetchBlob` instead;
     *  `download` links can use this directly (see fetchBlob fallback). */
    contentUrl: (root: string, subpath: string, download = false): string => {
      const qs = new URLSearchParams({ root, subpath });
      if (download) qs.set("download", "true");
      return `${BASE_URL}/api/v1/files/content?${qs.toString()}`;
    },

    /** Fetch file bytes with the Bearer header and wrap in an object URL.
     *  Caller MUST revokeObjectURL when done (see useAuthBlob pattern). */
    fetchBlob: async (root: string, subpath: string): Promise<string> => {
      const res = await fetch(api.files.contentUrl(root, subpath), {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        throw new Error(`API ${res.status}: ${text}`);
      }
      return URL.createObjectURL(await res.blob());
    },

    /** Reveal in Finder / open with default app. Non-200 means "not available
     *  here" (409 container-only, 501 native open unavailable) — callers treat
     *  any throw as "hide the Finder button", never surface the raw error. */
    open: (root: string, subpath: string, reveal: boolean) =>
      request<{ ok: boolean; available: boolean }>("/api/v1/files/open", {
        method: "POST",
        body: JSON.stringify({ root, subpath, reveal }),
      }),

    reindex: () =>
      request<{ ok: boolean }>("/api/v1/files/reindex", { method: "POST" }),

    /** Move files to the trash (~/.mc/.trash) — recoverable, never a hard delete.
     *  POST (not DELETE-with-body) to match the backend contract and keep
     *  client/server in sync. Returns what was trashed vs. skipped (with reason)
     *  plus how many linked deliverables cascaded. */
    delete: (root: string, subpaths: string[]) =>
      request<{
        trashed: { root: string; subpath: string; trash_path: string }[];
        skipped: { root?: string; subpath: string; reason: string }[];
        cascaded_deliverables: number;
      }>("/api/v1/files/delete", {
        method: "POST",
        body: JSON.stringify({ root, subpaths }),
      }),

    /** Operator-gated trash (~/.mc/.trash) management: list the soft-deleted
     *  files, restore them to their original root (re-indexed), or purge them
     *  for good. All three require Role.OPERATOR — identical to `delete`. */
    trash: {
      list: () =>
        request<{ entries: TrashEntry[] }>("/api/v1/files/trash"),

      /** Restore files back to their original root. Each id is validated
       *  before any move; failures land in `skipped` with a reason rather
       *  than aborting the batch. */
      restore: (trash_ids: string[]) =>
        request<{
          restored: { trash_id: string; root: string; subpath: string }[];
          skipped: { trash_id: string; reason: string }[];
        }>("/api/v1/files/trash/restore", {
          method: "POST",
          body: JSON.stringify({ trash_ids }),
        }),

      /** Hard-delete files from the trash — the one irreversible path. */
      purge: (trash_ids: string[]) =>
        request<{
          purged: string[];
          skipped: { trash_id: string; reason: string }[];
        }>("/api/v1/files/trash/purge", {
          method: "POST",
          body: JSON.stringify({ trash_ids }),
        }),
    },
  },

  // ── Model Prices ──────────────────────────────────────────────────────────────
  modelPrices: {
    list: () => request<ModelPrice[]>("/api/v1/model-prices"),
    create: (data: ModelPriceCreate) =>
      request<ModelPrice>("/api/v1/model-prices", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    update: (id: string, data: ModelPriceUpdate) =>
      request<ModelPrice>(`/api/v1/model-prices/${id}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      }),
    delete: (id: string) =>
      request<void>(`/api/v1/model-prices/${id}`, { method: "DELETE" }),
    unmatched: () => request<UnmatchedModel[]>("/api/v1/model-prices/unmatched"),
    recompute: (from_ts?: string) =>
      request<{ updated: number }>("/api/v1/model-prices/recompute", {
        method: "POST",
        body: JSON.stringify({ from_ts: from_ts ?? null }),
      }),
  },

  // ── Vault ─────────────────────────────────────────────────────────────────────
  vault: {
    list: (params?: { agent?: string; type?: string; limit?: number; offset?: number }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(
          Object.entries(params ?? {}).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])
        )
      ).toString();
      return request<VaultNotesListResponse>(`/api/v1/vault/notes${qs ? `?${qs}` : ""}`);
    },

    search: (params: { q: string; agent?: string; type?: string; limit?: number }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(
          Object.entries(params).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)])
        )
      ).toString();
      return request<VaultSearchResponse>(`/api/v1/vault/search?${qs}`);
    },

    get: (path: string) =>
      request<VaultNoteDetail>(`/api/v1/vault/note/${encodeURIComponent(path)}`),

    create: (body: {
      title: string;
      content: string;
      type?: string;
      tags?: string[];
      agent?: string;
    }) =>
      request<{
        ok: boolean;
        path: string;
        frontmatter: Record<string, unknown>;
        content: string;
      }>(`/api/v1/vault/note`, {
        method: "POST",
        body: JSON.stringify(body),
      }),

    update: (
      path: string,
      body: { title?: string; content?: string; tags?: string[] },
    ) =>
      request<{
        ok: boolean;
        path: string;
        frontmatter: Record<string, unknown>;
        content: string;
      }>(`/api/v1/vault/note/${encodeURIComponent(path)}`, {
        method: "PATCH",
        body: JSON.stringify(body),
      }),

    trackView: (path: string) =>
      request<VaultTrackViewResponse>("/api/v1/vault/track-view", {
        method: "POST",
        body: JSON.stringify({ path }),
      }),

    // Phase E task bracket — fetch every vault note that shares a `task`
    // frontmatter field. Used by the Reading Panel's "Verwandt" section to
    // surface the rest of the task's output once the operator opens any single hit.
    related: (taskId: string) =>
      request<{
        task_id: string;
        count: number;
        notes: VaultNote[];
      }>(`/api/v1/vault/related/${encodeURIComponent(taskId)}`),

    // Phase 4-followup: URL to the raw binary attachment for a deliverable
    // wrapper. Same auth gotcha as knowledge.getAttachmentUrl — `<img>` and
    // `<iframe>` can't carry a Bearer header, so consumers fetch as blob and
    // wrap in URL.createObjectURL. Returns a BASE-prefixed absolute URL so
    // `fetch(url, {headers: {Authorization}})` works from any component.
    getAttachmentUrl: (deliverableId: string): string =>
      `${BASE_URL}/api/v1/vault/attachment/${encodeURIComponent(deliverableId)}`,

    graph: (params?: { cluster?: boolean; heatmap?: string; similarity_edges?: boolean }) => {
      const qs = new URLSearchParams();
      if (params?.cluster !== undefined) qs.set("cluster", String(params.cluster));
      if (params?.heatmap) qs.set("heatmap", params.heatmap);
      // similarity_edges defaults to true on the backend; the Qdrant
      // round-trip per node makes cold builds 1-4 s. Callers should
      // typically pass false for fast UX (see useVaultGraph).
      if (params?.similarity_edges !== undefined) qs.set("similarity_edges", String(params.similarity_edges));
      const s = qs.toString();
      return request<VaultGraphResponse>(`/api/v1/vault/graph${s ? `?${s}` : ""}`);
    },

    backrefs: (path: string) =>
      request<{ path: string; backrefs: Array<{ path: string; title: string; agent: string }> }>(
        `/api/v1/vault/note/${encodeURIComponent(path)}/backrefs`,
      ),

    delete: (path: string) =>
      request<{ ok: boolean; path: string; trashed_to: string; backrefs: number }>(
        `/api/v1/vault/note/${encodeURIComponent(path)}`,
        { method: "DELETE" },
      ),

    trash: {
      list: () =>
        request<{
          count: number;
          items: Array<{
            trash_filename: string;
            original_path: string | null;
            trashed_at: string | null;
            title: string;
            agent: string;
            type: string;
            tags: string[];
            date: string;
            size_bytes: number;
          }>;
        }>("/api/v1/vault/_trash"),

      restore: (trashFilename: string) =>
        request<{ ok: boolean; path: string }>(
          `/api/v1/vault/_trash/${encodeURIComponent(trashFilename)}/restore`,
          { method: "POST" },
        ),

      purge: (trashFilename: string) =>
        request<{ ok: boolean; filename: string }>(
          `/api/v1/vault/_trash/${encodeURIComponent(trashFilename)}`,
          { method: "DELETE" },
        ),
    },

    topics: (k?: number) => {
      const qs = k != null ? `?k=${k}` : "";
      return request<{ topics: { cluster_id: number; label: string; note_count: number; top_notes: string[]; agents: string[] }[]; total_notes: number }>(
        `/api/v1/vault/topics${qs}`,
      );
    },
  },

  // ── Board Groups ────────────────────────────────────────────────────────────
  boardGroups: {
    list: () => request<BoardGroup[]>("/api/v1/board-groups"),
    create: (data: Partial<BoardGroup>) =>
      request<BoardGroup>("/api/v1/board-groups", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<BoardGroup>) =>
      request<BoardGroup>(`/api/v1/board-groups/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/api/v1/board-groups/${id}`, { method: "DELETE" }),
  },

  // ── Boards ──────────────────────────────────────────────────────────────────
  boards: {
    list: () => request<Board[]>("/api/v1/boards"),
    create: (data: Partial<Board>) =>
      request<Board>("/api/v1/boards", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<Board>(`/api/v1/boards/${id}`),
    update: (id: string, data: Partial<Board>) =>
      request<Board>(`/api/v1/boards/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/api/v1/boards/${id}`, { method: "DELETE" }),
    snapshot: (id: string) => request<{ board: Board; agents: Agent[]; tasks: Task[]; memory: BoardMemory[] }>(`/api/v1/boards/${id}/snapshot`),
  },

  // ── Projects ────────────────────────────────────────────────────────────────
  projects: {
    list: (boardId: string) => request<Project[]>(`/api/v1/boards/${boardId}/projects`),
    create: (boardId: string, data: Partial<Project>) =>
      request<Project>(`/api/v1/boards/${boardId}/projects`, { method: "POST", body: JSON.stringify(data) }),
    get: (boardId: string, id: string) => request<Project>(`/api/v1/boards/${boardId}/projects/${id}`),
    update: (boardId: string, id: string, data: Partial<Project>) =>
      request<Project>(`/api/v1/boards/${boardId}/projects/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (boardId: string, id: string) =>
      request<void>(`/api/v1/boards/${boardId}/projects/${id}`, { method: "DELETE" }),
    phases: (projectId: string) =>
      request<ProjectPhase[]>(`/api/v1/projects/${projectId}/phases`),
    gitInfo: (boardId: string, projectId: string) =>
      request<ProjectGitInfo>(`/api/v1/boards/${boardId}/projects/${projectId}/git-info`),
    initRepo: (boardId: string, projectId: string) =>
      request<{ github_repo_url: string; github_repo_name: string }>(
        `/api/v1/boards/${boardId}/projects/${projectId}/init-repo`,
        { method: "POST" }
      ),
    deliverables: (boardId: string, projectId: string) =>
      request<import("./types").TaskDeliverable[]>(`/api/v1/boards/${boardId}/projects/${projectId}/deliverables`),
  },

  // Planner disabled 2026-04-11 (Boss autonomy overhaul). Backend router returns 404.
  // PlannerMessage type stays — still used by research:.

  // ── Playbooks / Henry ───────────────────────────────────────────────────────
  playbooks: {
    catalog: () => request<{ playbooks: PlaybookCatalogItem[] }>("/api/v1/playbooks/catalog"),
    skillPacks: () => request<SkillPack[]>("/api/v1/playbooks/skill-packs"),
    list: (boardId?: string, includeArchived?: boolean) => {
      const params = new URLSearchParams();
      if (boardId) params.set("board_id", boardId);
      if (includeArchived) params.set("include_archived", "true");
      const qs = params.toString();
      return request<Playbook[]>(`/api/v1/playbooks${qs ? `?${qs}` : ""}`);
    },
    create: (data: {
      kind: string;
      name: string;
      summary?: string;
      goal?: string;
      board_id?: string | null;
      project_id?: string | null;
      skill_pack_id?: string | null;
      default_agent_id?: string | null;
      scope?: "global" | "board" | "project";
      status?: "draft" | "review" | "active" | "archived";
      current_config?: Record<string, unknown>;
      metadata?: Record<string, unknown> | null;
      review_notes?: string | null;
    }) => request<Playbook>("/api/v1/playbooks", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<Playbook>(`/api/v1/playbooks/${id}`),
    update: (id: string, data: Partial<Playbook> & { metadata?: Record<string, unknown> | null }) =>
      request<Playbook>(`/api/v1/playbooks/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    approve: (id: string) => request<Playbook>(`/api/v1/playbooks/${id}/approve`, { method: "POST" }),
    versions: (id: string) => request<PlaybookVersion[]>(`/api/v1/playbooks/${id}/versions`),
    createVersion: (id: string, changeReason?: string) =>
      request<PlaybookVersion>(`/api/v1/playbooks/${id}/versions`, {
        method: "POST",
        body: JSON.stringify({ change_reason: changeReason }),
      }),
    automations: (id: string) => request<Automation[]>(`/api/v1/playbooks/${id}/automations`),
    createAutomation: (id: string, data: {
      name: string;
      summary?: string;
      board_id?: string | null;
      project_id?: string | null;
      trigger_type?: "manual" | "scheduled";
      trigger_config?: Record<string, unknown> | null;
      delivery_config?: Record<string, unknown> | null;
      status?: "draft" | "active" | "paused" | "archived";
      runtime_overrides?: Record<string, unknown> | null;
    }) => request<Automation>(`/api/v1/playbooks/${id}/automations`, { method: "POST", body: JSON.stringify(data) }),
    recentRuns: (boardId?: string, limit?: number) => {
      const params = new URLSearchParams();
      if (boardId) params.set("board_id", boardId);
      if (limit) params.set("limit", String(limit));
      const qs = params.toString();
      return request<PlaybookRunProjection[]>(`/api/v1/playbooks/runs/recent${qs ? `?${qs}` : ""}`);
    },
    henryCurrent: (boardId: string) =>
      request<HenrySessionState | null>(`/api/v1/playbooks/henry/current?board_id=${boardId}`),
    henryStart: (data: { board_id: string; kind?: string; playbook_id?: string }) =>
      request<HenrySessionState>("/api/v1/playbooks/henry/sessions/start", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    henryMessage: (sessionId: string, content: string) =>
      request<HenrySessionState>(`/api/v1/playbooks/henry/sessions/${sessionId}/message`, {
        method: "POST",
        body: JSON.stringify({ content }),
      }),
  },

  automations: {
    list: (boardId?: string) =>
      request<Automation[]>(`/api/v1/automations${boardId ? `?board_id=${boardId}` : ""}`),
    get: (id: string) => request<Automation>(`/api/v1/automations/${id}`),
    update: (id: string, data: Partial<Automation>) =>
      request<Automation>(`/api/v1/automations/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    activate: (id: string) => request<Automation>(`/api/v1/automations/${id}/activate`, { method: "POST" }),
    pause: (id: string) => request<Automation>(`/api/v1/automations/${id}/pause`, { method: "POST" }),
    run: (id: string) => request<unknown>(`/api/v1/automations/${id}/run`, { method: "POST" }),
  },

  skillLab: {
    candidates: (boardId?: string) =>
      request<SkillCandidate[]>(`/api/v1/skill-lab/candidates${boardId ? `?board_id=${boardId}` : ""}`),
    updateCandidate: (id: string, data: Partial<SkillCandidate>) =>
      request<SkillCandidate>(`/api/v1/skill-lab/candidates/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
  },

  // ── Research ────────────────────────────────────────────────────────────────
  research: {
    list: (boardId?: string) =>
      request<Project[]>(`/api/v1/research${boardId ? `?board_id=${boardId}` : ""}`),
    start: (data: { title: string; description?: string; board_id: string; initial_message?: string }) =>
      request<ResearchStartResponse>(`/api/v1/research/start`, { method: "POST", body: JSON.stringify(data) }),
    chat: (projectId: string) => request<PlannerMessage[]>(`/api/v1/research/${projectId}/chat`),
    message: (projectId: string, content: string) =>
      request<PlannerMessage>(`/api/v1/research/${projectId}/message`, { method: "POST", body: JSON.stringify({ content }) }),
    save: (projectId: string, data?: { title?: string; content?: string; tags?: string[]; agent_id?: string }) =>
      request<ResearchSaveResponse>(`/api/v1/research/${projectId}/save`, {
        method: "POST",
        body: JSON.stringify(data ?? {}),
      }),
    delete: (projectId: string) =>
      request<void>(`/api/v1/research/${projectId}`, { method: "DELETE" }),
  },

  // ── Tasks ───────────────────────────────────────────────────────────────────
  tasks: {
    pipeline: (boardId: string) =>
      request<TaskPipelineResponse>(`/api/v1/boards/${boardId}/tasks/pipeline`),
    hierarchy: (boardId: string, taskId: string) =>
      request<import("./types").TaskHierarchy>(`/api/v1/boards/${boardId}/tasks/${taskId}/hierarchy`),
    list: (boardId: string, params?: { status?: string; agent_id?: string; project_id?: string; parent_task_id?: string }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null)) as Record<string, string>
      ).toString();
      return request<Task[]>(`/api/v1/boards/${boardId}/tasks${qs ? `?${qs}` : ""}`);
    },
    subtasks: (boardId: string, parentTaskId: string) =>
      request<Task[]>(`/api/v1/boards/${boardId}/tasks?parent_task_id=${parentTaskId}`),
    /** `defer_dispatch: true` skips the server's normal auto-dispatch-on-create
     *  (ADR-054 follow-up, C2 review fix) — used when reference files are
     *  staged so the agent brief isn't built before they've been uploaded.
     *  Follow up with `dispatchDeferred` once the uploads are done. */
    create: (boardId: string, data: Partial<Task> & { defer_dispatch?: boolean }) =>
      request<Task>(`/api/v1/boards/${boardId}/tasks`, { method: "POST", body: JSON.stringify(data) }),
    get: (boardId: string, id: string) => request<Task>(`/api/v1/boards/${boardId}/tasks/${id}`),
    update: (boardId: string, id: string, data: Partial<Task>) =>
      request<Task>(`/api/v1/boards/${boardId}/tasks/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (boardId: string, id: string) =>
      request<void>(`/api/v1/boards/${boardId}/tasks/${id}`, { method: "DELETE" }),
    reorder: (boardId: string, items: { id: string; sort_order: number; status?: string }[]) =>
      request<{ updated: number }>(`/api/v1/boards/${boardId}/tasks/reorder`, { method: "PATCH", body: JSON.stringify(items) }),
    /** Fetch up a task created with `defer_dispatch: true` once its reference
     *  uploads are done. 409 means the task moved on some other way (no
     *  longer inbox/undispatched) — callers should tolerate that, not treat
     *  it as a hard failure. */
    dispatchDeferred: (boardId: string, taskId: string) =>
      request<{ status: string }>(`/api/v1/boards/${boardId}/tasks/${taskId}/dispatch`, { method: "POST" }),
    dependencies: (boardId: string, taskId: string) =>
      request<TaskDependencyInfo[]>(`/api/v1/boards/${boardId}/tasks/${taskId}/dependencies`),
    events: (boardId: string, taskId: string) =>
      request<TaskEvent[]>(`/api/v1/boards/${boardId}/tasks/${taskId}/events`),
    comments: {
      list: (boardId: string, taskId: string) =>
        request<TaskComment[]>(`/api/v1/boards/${boardId}/tasks/${taskId}/comments`),
      create: (boardId: string, taskId: string, content: string, comment_type?: string) =>
        request<TaskComment>(`/api/v1/boards/${boardId}/tasks/${taskId}/comments`, {
          method: "POST",
          body: JSON.stringify({ content, ...(comment_type ? { comment_type } : {}) }),
        }),
    },
    deliverables: {
      list: (
        boardId: string,
        taskId: string,
        opts?: { includeSubtasks?: boolean; depth?: number },
      ) => {
        const qs = new URLSearchParams();
        if (opts?.includeSubtasks) qs.set("include_subtasks", "true");
        if (opts?.depth != null) qs.set("depth", String(opts.depth));
        const suffix = qs.toString() ? `?${qs.toString()}` : "";
        return request<import("./types").TaskDeliverable[]>(
          `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables${suffix}`,
        );
      },
      open: (
        boardId: string,
        taskId: string,
        deliverableId: string,
        body: { reveal: boolean; subpath?: string },
      ) =>
        request<{ ok: boolean }>(
          `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${deliverableId}/open`,
          { method: "POST", body: JSON.stringify(body) },
        ),
      directory: (boardId: string, taskId: string, deliverableId: string, subpath?: string) =>
        request<import("./types").DeliverableDirectory>(
          `/api/v1/boards/${boardId}/tasks/${taskId}/deliverables/${deliverableId}/directory${subpath ? `?subpath=${encodeURIComponent(subpath)}` : ""}`,
        ),
    },
    checklist: {
      list: (boardId: string, taskId: string) =>
        request<TaskChecklistItem[]>(`/api/v1/boards/${boardId}/tasks/${taskId}/checklist`),
    },
    gitInfo: (boardId: string, taskId: string) =>
      request<TaskGitInfo>(`/api/v1/boards/${boardId}/tasks/${taskId}/git-info`),
    gitDiff: (boardId: string, taskId: string, commit: string) =>
      request<CommitDiff>(`/api/v1/boards/${boardId}/tasks/${taskId}/git-diff?commit=${encodeURIComponent(commit)}`),
    review: (boardId: string, taskId: string, body: { decision: "approve" | "request_changes" | "hold"; comment: string }) =>
      request<{ status: string; decision: string }>(`/api/v1/boards/${boardId}/tasks/${taskId}/review`, {
        method: "POST",
        body: JSON.stringify(body),
      }),
    promote: (boardId: string, taskId: string) =>
      request<Task>(`/api/v1/boards/${boardId}/tasks/${taskId}/promote`, { method: "POST" }),
    stop: (boardId: string, taskId: string, reason: string = "") =>
      request<Task>(`/api/v1/boards/${boardId}/tasks/${taskId}/stop`, {
        method: "POST",
        body: JSON.stringify({ reason }),
      }),
    resume: (boardId: string, taskId: string) =>
      request<Task>(`/api/v1/boards/${boardId}/tasks/${taskId}/resume`, {
        method: "POST",
      }),
    transcript: (taskId: string, limit?: number) =>
      request<import("./types").TaskTranscriptResponse>(
        `/api/v1/tasks/${taskId}/transcript${limit ? `?limit=${limit}` : ""}`
      ),
  },

  // ── Reference Files (ADR-054) ────────────────────────────────────────────────
  // Operator-uploaded example/asset files for tasks & projects. Agents read
  // them directly — their paths flow into the dispatch directive automatically.
  references: {
    /** Upload bypasses the json-encoding default of `request` — FormData must
     *  set its own multipart boundary in the Content-Type header (same
     *  pattern as knowledge.uploadAttachment). */
    upload: async (
      target: { taskId: string } | { projectId: string },
      file: File,
      note?: string,
    ): Promise<ReferenceFile> => {
      const fd = new FormData();
      fd.append("file", file);
      if ("taskId" in target) fd.append("task_id", target.taskId);
      else fd.append("project_id", target.projectId);
      if (note) fd.append("note", note);
      const res = await fetch(`${BASE_URL}/api/v1/references/upload`, {
        method: "POST",
        body: fd,
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        throw new Error(`API ${res.status}: ${text}`);
      }
      return res.json();
    },
    list: (target: { taskId: string } | { projectId: string }) => {
      const qs = new URLSearchParams(
        "taskId" in target ? { task_id: target.taskId } : { project_id: target.projectId },
      ).toString();
      return request<ReferenceFile[]>(`/api/v1/references?${qs}`);
    },
    downloadUrl: (id: string): string => `${BASE_URL}/api/v1/references/${id}/download`,
    /** The download route requires a Bearer header, which `<a href>` can't
     *  carry — fetch as blob and wrap in an object URL (same gotcha as
     *  knowledge.getAttachmentUrl / files.fetchBlob). */
    fetchBlob: async (id: string): Promise<string> => {
      const res = await fetch(api.references.downloadUrl(id), {
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        throw new Error(`API ${res.status}: ${text}`);
      }
      return URL.createObjectURL(await res.blob());
    },
    remove: (id: string) => request<void>(`/api/v1/references/${id}`, { method: "DELETE" }),
  },

  // ── Agents ──────────────────────────────────────────────────────────────────
  agents: {
    list: (boardId?: string, includeUnassigned?: boolean) => {
      const params = new URLSearchParams();
      if (boardId) params.set("board_id", boardId);
      if (includeUnassigned) params.set("include_unassigned", "true");
      const qs = params.toString();
      return request<Agent[]>(`/api/v1/agents${qs ? `?${qs}` : ""}`);
    },
    create: (data: { name: string; emoji?: string; role?: string; model?: string; board_id?: string; agent_runtime?: string; runtime_id?: string }) =>
      request<Agent>("/api/v1/agents", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<Agent>(`/api/v1/agents/${id}`),
    update: (id: string, data: Partial<Agent>, opts?: { restart?: boolean }) =>
      request<Agent>(
        `/api/v1/agents/${id}${opts?.restart ? "?restart=true" : ""}`,
        { method: "PATCH", body: JSON.stringify(data) }
      ),
    /**
     * Phase 15 T3.1/T3.2 — dry-run runtime switch preview.
     * Returns the same shape as a real switch (image_switched + warnings +
     * old/new runtime summaries) without mutating anything. Used by the
     * confirm modal in AgentDetailPage to surface compatibility warnings
     * and image-rebuild duration upfront.
     */
    previewRuntimeSwitch: (
      id: string,
      data: { runtime_id: string; force_when_in_progress?: boolean; harness?: import("@/lib/types").Harness },
    ) =>
      request<import("@/lib/types").RuntimeSwitchPreview>(
        `/api/v1/agents/${id}/preview-runtime-switch`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    /**
     * Phase 15 T2.2 — atomic runtime switch with force flag. Thin wrapper
     * over PATCH /agents/{id} that always passes force_when_in_progress so
     * the UI doesn't have to flatten it into the generic Partial<Agent>.
     */
    switchRuntime: (
      id: string,
      runtime_id: string | null,
      opts?: { force_when_in_progress?: boolean; harness?: import("@/lib/types").Harness },
    ) =>
      request<Agent & { _switch?: import("@/lib/types").RuntimeSwitchPreview }>(
        `/api/v1/agents/${id}`,
        {
          method: "PATCH",
          body: JSON.stringify({
            runtime_id,
            force_when_in_progress: opts?.force_when_in_progress ?? false,
            ...(opts?.harness ? { harness: opts.harness } : {}),
          }),
        },
      ),
    runtimeSwitchProgress: (id: string) =>
      request<import("@/lib/types").RuntimeSwitchProgress>(
        `/api/v1/agents/${id}/runtime-switch-progress`
      ),
    delete: (id: string) => request<void>(`/api/v1/agents/${id}`, { method: "DELETE" }),
    config: {
      all: (id: string) => request<Record<string, string | null>>(`/api/v1/agents/${id}/config`),
      get: (id: string, fileType: string) =>
        request<{ file_type: string; content: string | null }>(`/api/v1/agents/${id}/config/${fileType}`),
      update: (id: string, fileType: string, content: string) =>
        request<{ saved: boolean; gateway_sync: boolean; warnings: string[] }>(
          `/api/v1/agents/${id}/config/${fileType}`,
          { method: "PUT", body: JSON.stringify({ content }) }
        ),
    },
    trigger: (id: string, message?: string) =>
      request<{ source: string; reply: string | null }>(`/api/v1/agents/${id}/trigger`, {
        method: "POST",
        body: JSON.stringify(message ? { message } : {}),
      }),
    reset: (id: string) =>
      request<unknown>(`/api/v1/agents/${id}/reset`, { method: "POST" }),
    heartbeat: (id: string) =>
      request<unknown>(`/api/v1/agents/${id}/heartbeat`, { method: "POST" }),
    metrics: {
      list: (id: string, period?: string) =>
        request<AgentMetrics[]>(`/api/v1/agents/${id}/metrics${period ? `?period=${period}` : ""}`),
      summary: (id: string) =>
        request<Record<string, unknown>>(`/api/v1/agents/${id}/metrics/summary`),
      comparison: () => request<Record<string, unknown>[]>("/api/v1/agents/metrics/comparison"),
    },
    // Phase 31 / OCS-15: legacy `provision` (OpenClaw) removed.
    // CLI Bridge Provisioning
    // NOTE: rename to `provision` deferred to Phase 32+ housekeeping per PATTERNS.md gotcha.
    provisionCli: (id: string, opts?: { model?: string; system_prompt?: string; extra_plugins?: string[] }) =>
      request<{ provision_status: string; token: string | null; bridge_result: unknown }>(`/api/v1/agents/${id}/provision`, {
        method: "POST",
        body: JSON.stringify(opts ?? {}),
      }),
    restartWorker: (id: string) =>
      request<{ ok: boolean; agent: string; session: string }>(`/api/v1/agents/${id}/restart-worker`, {
        method: "POST",
      }),
    restartContainer: (id: string) =>
      request<{ ok: boolean; container: string; state: string }>(`/api/v1/agents/${id}/restart`, {
        method: "POST",
      }),
    startContainer: (id: string) =>
      request<{ ok: boolean; container: string; state: string }>(`/api/v1/agents/${id}/start`, {
        method: "POST",
      }),
    stopContainer: (id: string) =>
      request<{ ok: boolean; container: string; state: string }>(`/api/v1/agents/${id}/stop`, {
        method: "POST",
      }),
    forceRecreateContainer: (id: string, force = false) =>
      request<{ ok: boolean; container: string; state: string; duration_seconds: number }>(
        `/api/v1/agents/${id}/force-recreate${force ? "?force=true" : ""}`,
        { method: "POST" },
      ),
    localMemory: {
      list: (id: string) =>
        request<{
          directory: string;
          files: { name: string; size: number; content: string; truncated: boolean }[];
          container_state?: string;
        }>(`/api/v1/agents/${id}/local-memory`),
      delete: (id: string, filename: string) =>
        request<{ ok: boolean; deleted: string }>(
          `/api/v1/agents/${id}/local-memory/${encodeURIComponent(filename)}`,
          { method: "DELETE" },
        ),
    },
    restartHost: (id: string) =>
      request<{ ok: boolean; action: string; agent: string }>(`/api/v1/host-agents/${id}/restart`, {
        method: "POST",
      }),
    startHost: (id: string) =>
      request<{ ok: boolean; action: string; agent: string }>(`/api/v1/host-agents/${id}/start`, {
        method: "POST",
      }),
    stopHost: (id: string) =>
      request<{ ok: boolean; action: string; agent: string }>(`/api/v1/host-agents/${id}/stop`, {
        method: "POST",
      }),
    listDockerSessions: () =>
      request<(Agent & { container_state: string })[]>(`/api/v1/docker-sessions/agents`),
    listHostSessions: () =>
      request<(Agent & { session_name: string; session_running: boolean })[]>(
        `/api/v1/host-sessions/agents`,
      ),
    syncConfig: (id: string, opts?: { fileTypes?: string[]; restart?: boolean }) => {
      const query = opts?.restart ? "?restart=true" : "";
      return request<{
        synced: Record<string, string>;
        runtime?: string;
        restart?: { status: string; container: string };
      }>(`/api/v1/agents/${id}/sync-config${query}`, {
        method: "POST",
        body: JSON.stringify(opts?.fileTypes ? { file_types: opts.fileTypes } : {}),
      });
    },
    // Specialized Agents Setup
    setupSpecialized: (boardId: string, provision?: boolean) =>
      request<{
        created: { id: string; name: string; emoji: string; model: string | null; token: string }[];
        count: number;
        note: string;
      }>("/api/v1/agents/setup-specialized", {
        method: "POST",
        body: JSON.stringify({ board_id: boardId, provision: provision ?? false }),
      }),
    // Setup coordination (regenerate templates + USER.md + MEMORY.md)
    setupCoordination: (boardSlug = "mc-dev") =>
      request<{ board: string; agents: unknown[] }>("/api/v1/agents/setup-coordination", {
        method: "POST",
        body: JSON.stringify({ board_slug: boardSlug, sync_to_gateway: true }),
      }),
    // Change board assignment
    assignBoard: (id: string, boardId: string | null) =>
      request<Agent>(`/api/v1/agents/${id}/assign-board`, {
        method: "PATCH",
        body: JSON.stringify({ board_id: boardId }),
      }),
    taskSessions: (id: string, limit?: number) =>
      request<import("./types").TaskSessionInfo[]>(
        `/api/v1/agents/${id}/task-sessions${limit ? `?limit=${limit}` : ""}`
      ),
    // Agent Council: Discord
    discord: {
      create: (id: string, data: { name: string; context: string }) =>
        request<{ channel_id: string; name: string; agent: Agent }>(`/api/v1/agents/${id}/discord-channel`, {
          method: "POST",
          body: JSON.stringify(data),
        }),
      rename: (id: string, newName: string) =>
        request<{ old_name: string; new_name: string }>(`/api/v1/agents/${id}/discord-channel`, {
          method: "PATCH",
          body: JSON.stringify({ new_name: newName }),
        }),
      remove: (id: string) =>
        request<{ unbound: boolean }>(`/api/v1/agents/${id}/discord-channel`, { method: "DELETE" }),
    },
    cli: {
      sessions: (agentId: string) =>
        request<{ task_id: string; session: string; elapsed_seconds: number }[]>(
          `/api/v1/agents/${agentId}/cli-sessions`
        ),
      input: (agentId: string, taskId: string, text: string) =>
        request<{ ok: boolean }>(`/api/v1/agents/${agentId}/terminal/${taskId}/input`, {
          method: "POST",
          body: JSON.stringify({ text }),
        }),
      kill: (agentId: string, taskId: string) =>
        request<{ ok: boolean }>(`/api/v1/agents/${agentId}/terminal/${taskId}`, {
          method: "DELETE",
        }),
      wsUrl: (agentId: string, taskId: string): string => {
        const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
        const ws = base.replace(/^http/, "ws");
        return `${ws}/api/v1/agents/${agentId}/terminal/${taskId}/ws?token=${getToken()}`;
      },
    },
  },

  // ── Agent Templates ─────────────────────────────────────────────────────────
  agentTemplates: {
    list: () => request<AgentTemplate[]>("/api/v1/agent-templates"),
    create: (data: { name: string; emoji?: string; role?: string; default_model?: string; soul_md?: string; skills?: string[] }) =>
      request<AgentTemplate>("/api/v1/agent-templates", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<AgentTemplate>) =>
      request<AgentTemplate>(`/api/v1/agent-templates/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/api/v1/agent-templates/${id}`, { method: "DELETE" }),
    instantiate: (id: string, data: { board_id: string; model?: string; name?: string }) =>
      request<{ agent: Agent; token: string }>(`/api/v1/agent-templates/${id}/instantiate`, {
        method: "POST",
        body: JSON.stringify(data),
      }),
  },

  // ── Approvals ───────────────────────────────────────────────────────────────
  approvals: {
    list: () => request<Approval[]>("/api/v1/approvals"),
    boardList: (boardId: string) => request<Approval[]>(`/api/v1/boards/${boardId}/approvals`),
    resolve: (id: string, status: "approved" | "rejected", note?: string) =>
      request<Approval>(`/api/v1/approvals/${id}`, {
        method: "PATCH",
        body: JSON.stringify({ status, resolver_note: note }),
      }),
  },

  // ── Install Requests ────────────────────────────────────────────────────────
  installRequests: {
    create: (body: InstallRequestBody) =>
      request<InstallRequestResponse>("/api/v1/agent/install-requests", {
        method: "POST",
        body: JSON.stringify(body),
      }),
  },

  // ── MCP Servers ──────────────────────────────────────────────────────────────
  mcpServers: {
    list: (): Promise<MCPServer[]> =>
      request<MCPServer[]>("/api/v1/mcp-servers"),
    get: (name: string): Promise<MCPServer> =>
      request<MCPServer>(`/api/v1/mcp-servers/${encodeURIComponent(name)}`),
    setForAgent: (agentId: string, servers: string[] | null): Promise<unknown> =>
      request<unknown>(`/api/v1/agents/${agentId}/mcp-servers`, {
        method: "PATCH",
        body: JSON.stringify({ mcp_servers: servers }),
      }),
    create: (data: {
      name: string;
      transport: "stdio" | "http" | "sse";
      command?: string;
      args?: string[];
      url?: string;
      description?: string;
    }): Promise<MCPServer> =>
      request<MCPServer>("/api/v1/mcp-servers", {
        method: "POST",
        body: JSON.stringify(data),
      }),
    delete: (name: string): Promise<{ ok: boolean; cleaned_agents: string[] }> =>
      request<{ ok: boolean; cleaned_agents: string[] }>(
        `/api/v1/mcp-servers/${encodeURIComponent(name)}`,
        { method: "DELETE" }
      ),
  },

  // ── Memory (board-scoped, backwards compatible) ─────────────────────────────
  memory: {
    list: (boardId: string, params?: { memory_type?: string; source?: string; pinned_only?: boolean }) => {
      const qs = new URLSearchParams(params as Record<string, string>).toString();
      return request<BoardMemory[]>(`/api/v1/boards/${boardId}/memory${qs ? `?${qs}` : ""}`);
    },
    create: (boardId: string, data: Partial<BoardMemory>) =>
      request<BoardMemory>(`/api/v1/boards/${boardId}/memory`, { method: "POST", body: JSON.stringify(data) }),
    update: (boardId: string, id: string, data: Partial<BoardMemory>) =>
      request<BoardMemory>(`/api/v1/boards/${boardId}/memory/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (boardId: string, id: string) =>
      request<void>(`/api/v1/boards/${boardId}/memory/${id}`, { method: "DELETE" }),
  },

  // ── Knowledge Base (global / agent / cross-board) ──────────────────────────
  knowledge: {
    list: (params?: {
      memory_type?: string; source?: string; agent_id?: string;
      board_id?: string; auto_generated?: string; pinned_only?: string;
      search?: string; limit?: number; offset?: number;
      scope?: "global" | "board" | "agent" | "all";
    }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]))
      ).toString();
      return request<BoardMemory[]>(`/api/v1/knowledge${qs ? `?${qs}` : ""}`);
    },
    timeline: (params?: { days?: number; agent_id?: string }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]))
      ).toString();
      return request<BoardMemory[]>(`/api/v1/knowledge/timeline${qs ? `?${qs}` : ""}`);
    },
    get: (id: string) =>
      request<{ entry: BoardMemory; linked: BoardMemory[] }>(`/api/v1/knowledge/${id}`),
    create: (data: {
      content: string; title?: string; tags?: string[]; memory_type?: string;
      source?: string; board_id?: string; agent_id?: string;
      linked_ids?: string[]; auto_generated?: boolean;
    }) =>
      request<BoardMemory>("/api/v1/knowledge", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<BoardMemory>) =>
      request<BoardMemory>(`/api/v1/knowledge/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/api/v1/knowledge/${id}`, { method: "DELETE" }),
    // ── Phase 5 MSY-03: attachments ──────────────────────────────────────
    /** Upload an attachment to a knowledge entry.
     *  Bypasses the json-encoding default of `request` because FormData
     *  must set its own multipart boundary in the Content-Type header. */
    uploadAttachment: async (id: string, file: File): Promise<{
      path: string; mime_type: string; size_bytes: number; original_name: string;
    }> => {
      const fd = new FormData();
      fd.append("file", file);
      const res = await fetch(`${BASE_URL}/api/v1/knowledge/${id}/attachments`, {
        method: "POST",
        body: fd,
        // No Content-Type — let the browser set the multipart boundary.
        headers: { Authorization: `Bearer ${getToken()}` },
      });
      if (!res.ok) {
        const text = await res.text().catch(() => res.statusText);
        throw new Error(`API ${res.status}: ${text}`);
      }
      return res.json();
    },
    /** Returns the URL for a stored attachment (BASE-prefixed, encoded filename).
     *  The `<img>` element CANNOT carry a Bearer header, so AttachmentThumb
     *  fetches via `request`-equivalent and uses URL.createObjectURL. */
    getAttachmentUrl: (id: string, filename: string): string =>
      `${BASE_URL}/api/v1/knowledge/${id}/attachments/${encodeURIComponent(filename)}`,
    /** Remove a single attachment file + drop it from the entry's array. */
    deleteAttachment: (id: string, filename: string) =>
      request<void>(
        `/api/v1/knowledge/${id}/attachments/${encodeURIComponent(filename)}`,
        { method: "DELETE" },
      ),
    // ── Phase 5 MSY-02: MERGE-Badge user-confirm endpoints ───────────────
    /** Merge SOURCE into TARGET — appends source content + tags + linked_ids
     *  to target, deletes source row + its Qdrant vector. Idempotent. */
    mergeInto: (id: string, targetId: string) =>
      request<{ merged_into: string; deleted: string }>(
        `/api/v1/knowledge/${id}/merge_into/${targetId}`,
        { method: "POST" },
      ),
    /** Clear merge_candidate_id, keep both entries side-by-side. */
    keepBoth: (id: string) =>
      request<{ kept_both: string }>(
        `/api/v1/knowledge/${id}/keep_both`,
        { method: "POST" },
      ),
    /** Clear merge_candidate_id + tag entry as user-reviewed-unrelated for
     *  future-similarity suppression. */
    markUnrelated: (id: string) =>
      request<{ marked_unrelated: string }>(
        `/api/v1/knowledge/${id}/unrelated`,
        { method: "POST" },
      ),
    stats: (params?: { agent_id?: string; board_id?: string }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]))
      ).toString();
      return request<{ stats: Record<string, number>; total: number }>(`/api/v1/knowledge/stats${qs ? `?${qs}` : ""}`);
    },
    /** List memories filtered by layer (semantic/agent/episodic).
     *  Maps each layer to its memory_types from memory_indexing.py. */
    listByLayer: (layer: "semantic" | "agent" | "episodic", params?: {
      search?: string; agent_id?: string; board_id?: string;
      limit?: number; offset?: number;
      scope?: "global" | "board" | "agent" | "all";
    }) => {
      const typeMap: Record<string, string[]> = {
        semantic: ["knowledge", "reference", "research"],
        episodic: ["journal", "weekly_review", "insight"],
        agent: ["lesson"],
      };
      const types = typeMap[layer] ?? [];
      // Fetch all matching types in parallel and flatten
      return Promise.all(
        types.map((t) => api.knowledge.list({ ...params, memory_type: t }))
      ).then((results) =>
        results.flat().sort((a, b) =>
          new Date(b.created_at).getTime() - new Date(a.created_at).getTime()
        )
      );
    },
    /** List agent-scoped lessons, optionally filtered to a single agent. */
    listAgentLessons: (agentId?: string) =>
      api.knowledge.list({
        memory_type: "lesson",
        ...(agentId ? { agent_id: agentId } : {}),
      }),
    // Semantic Memory Query via Qdrant (Phase 3, 2026-04-11)
    // Note: backend endpoint is agent-scoped (/api/v1/agent/memory/query),
    // not user-scoped. Frontend call requires a Mission Control user who
    // also has agent-bridge token OR we add a parallel user-auth endpoint.
    // For now: no-op on user-auth, query is routed via user-scoped endpoint.
    query: (data: {
      query: string;
      layers?: ("semantic" | "agent" | "episodic")[];
      top_k?: number;
      agent_id?: string | null;
      board_id?: string | null;
    }) =>
      request<{
        query: string;
        agent_id?: string | null;
        board_id?: string | null;
        fallback?: boolean;
        results: Record<string, Array<{
          memory_id: string;
          score: number;
          title: string;
          content_preview: string;
          memory_type?: string;
          tags?: string[];
          source: string;
        }>>;
      }>("/api/v1/memory/query", { method: "POST", body: JSON.stringify(data) }),
  },

  // ── Activity ────────────────────────────────────────────────────────────────
  activity: {
    list: (params?: { board_id?: string; agent_id?: string; event_type?: string; severity?: string; limit?: number }) => {
      const qs = new URLSearchParams(params as Record<string, string>).toString();
      return request<ActivityEvent[]>(`/api/v1/activity${qs ? `?${qs}` : ""}`);
    },
    pluginAudit: (params?: { limit?: number; offset?: number }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null).map(([k, v]) => [k, String(v)]))
      ).toString();
      return request<{ events: ActivityEvent[]; total: number }>(`/api/v1/plugins/audit${qs ? `?${qs}` : ""}`);
    },
  },

  // Phase 31 / OCS-15: api.gateways group removed entirely. Backend routes
  // /api/v1/gateways/* were deleted in Phase 29; the gateways table was
  // dropped in Phase 30. Discord channel management now lives under
  // api.discord (see below) — channels come from settings.discord_guild_id.


  // Loops (ADR-051) — outcome-driven task loops.
  loops: {
    list: (boardId?: string) =>
      request<Loop[]>(`/api/v1/loops${boardId ? `?board_id=${boardId}` : ""}`),
    create: (data: LoopCreate) =>
      request<Loop>("/api/v1/loops", { method: "POST", body: JSON.stringify(data) }),
    get: (id: string) => request<LoopDetail>(`/api/v1/loops/${id}`),
    update: (id: string, data: LoopUpdate) =>
      request<Loop>(`/api/v1/loops/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    start: (id: string) => request<Loop>(`/api/v1/loops/${id}/start`, { method: "POST" }),
    pause: (id: string) => request<Loop>(`/api/v1/loops/${id}/pause`, { method: "POST" }),
    stop: (id: string) => request<Loop>(`/api/v1/loops/${id}/stop`, { method: "POST" }),
    remove: (id: string) => request<void>(`/api/v1/loops/${id}`, { method: "DELETE" }),
  },

  // ── Skills (filesystem-local) ─────────────────────────────────────────────
  // Phase 31 / OCS-15: backend now reads skills from ~/.mc/skills/ (Phase 29).
  skills: {
    list: () => request<SkillsResponse>("/api/v1/skills"),
    get: (name: string) => request<OpenClawSkill>(`/api/v1/skills/${encodeURIComponent(name)}`),
    install: (name: string, installId: string) =>
      request<{ success: boolean; result: unknown }>(`/api/v1/skills/${encodeURIComponent(name)}/install`, {
        method: "POST",
        body: JSON.stringify({ install_id: installId }),
      }),
    update: (name: string, data: { enabled?: boolean; api_key?: string; env?: Record<string, string> }) =>
      request<{ success: boolean; result: unknown }>(`/api/v1/skills/${encodeURIComponent(name)}`, {
        method: "PATCH",
        body: JSON.stringify(data),
      }),
    content: (name: string) =>
      request<{ skill_name: string; path: string; content: string; found: boolean }>(
        `/api/v1/skills/${encodeURIComponent(name)}/content`
      ),
    saveContent: (name: string, content: string) =>
      request<{ skill_name: string; path: string; saved: boolean }>(
        `/api/v1/skills/${encodeURIComponent(name)}/content`,
        { method: "PUT", body: JSON.stringify({ content }) }
      ),
    agentSkills: (agentId: string) =>
      request<AgentSkillsResponse>(`/api/v1/agents/${agentId}/skills`),
    setAgentSkills: (agentId: string, data: {
      skills?: string[] | null; cli_plugins?: string[] | null; update_cli_plugins?: boolean;
      cli_skills?: string[] | null; update_cli_skills?: boolean;
    }) =>
      request<{ agent_id: string; skill_filter: string[] | null; cli_plugins: string[] | null; cli_skills: string[] | null; changed: boolean; cli_synced: boolean; skills_synced: boolean }>(
        `/api/v1/agents/${agentId}/skills`,
        { method: "PATCH", body: JSON.stringify(data) }
      ),
  },

  // ── CLI Plugins ──────────────────────────────────────────────────────────────
  plugins: {
    list: () =>
      request<{ plugins: CliPlugin[]; total: number }>("/api/v1/plugins"),
    listGithubSkills: () =>
      request<{ repos: GithubSkillRepo[]; total: number }>("/api/v1/plugins/github-skills"),
    listCustomSkills: () =>
      request<{ skills: { name: string; description: string; path: string }[]; total: number }>("/api/v1/plugins/custom-skills"),
    install: (pluginKey: string) =>
      request<{ success: boolean; plugin_key: string }>("/api/v1/plugins/install", {
        method: "POST",
        body: JSON.stringify({ plugin_key: pluginKey }),
      }),
    update: (pluginKey: string) =>
      request<{ success: boolean; plugin_key: string }>(
        `/api/v1/plugins/${encodeURIComponent(pluginKey)}/update`,
        { method: "POST" }
      ),
    remove: (pluginKey: string) =>
      request<{ success: boolean; plugin_key: string; agents_updated: string[] }>(
        `/api/v1/plugins/${encodeURIComponent(pluginKey)}`,
        { method: "DELETE" }
      ),
    // Shell
    startShell: () =>
      request<{ ok: boolean; session: string }>("/api/v1/plugins/shell", { method: "POST" }),
    stopShell: () =>
      request<{ ok: boolean; session: string }>("/api/v1/plugins/shell", { method: "DELETE" }),
    shellWsUrl: (): string => {
      const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
      const ws = base.replace(/^http/, "ws");
      return `${ws}/api/v1/plugins/shell/ws?token=${getToken()}`;
    },
  },

  // Phase 31 / OCS-15: api.clawhub group removed (Marketplace UI deleted in
  // Plan 31-01; backend /api/v1/clawhub/* routes removed in Phase 29).

  // ── Models Catalog ──────────────────────────────────────────────────────────
  models: {
    list: () => request<ModelCatalog>("/api/v1/models"),
    get: (id: string) => request<ModelInfo>(`/api/v1/models/${encodeURIComponent(id)}`),
  },

  // Phase 31 / OCS-15: api.openclaw group removed (Phase 29 deleted the
  // /api/v1/gateways/openclaw/* routes; Phase 30 dropped the backing tables).

  // ── Tags ────────────────────────────────────────────────────────────────────
  tags: {
    list: () => request<Tag[]>("/api/v1/tags"),
    create: (data: Partial<Tag>) =>
      request<Tag>("/api/v1/tags", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<Tag>) =>
      request<Tag>(`/api/v1/tags/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: string) => request<void>(`/api/v1/tags/${id}`, { method: "DELETE" }),
    forProject: (projectId: string) =>
      request<Tag[]>(`/api/v1/projects/${projectId}/tags`),
    assignToProject: (projectId: string, data: { tag_id?: string; name?: string; color?: string }) =>
      request<Tag>(`/api/v1/projects/${projectId}/tags`, { method: "POST", body: JSON.stringify(data) }),
    removeFromProject: (projectId: string, tagId: string) =>
      request<void>(`/api/v1/projects/${projectId}/tags/${tagId}`, { method: "DELETE" }),
  },

  // ── Secrets (API Keys) ──────────────────────────────────────────────────────
  secrets: {
    providers: () => request<ProviderTemplate[]>("/api/v1/secrets/providers"),
    list: () => request<SecretEntry[]>("/api/v1/secrets"),
    create: (data: { key: string; value: string; provider?: string; label?: string; description?: string }) =>
      request<SecretEntry>("/api/v1/secrets", { method: "POST", body: JSON.stringify(data) }),
    update: (key: string, data: { value?: string; label?: string; description?: string }) =>
      request<SecretEntry>(`/api/v1/secrets/${encodeURIComponent(key)}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (key: string) =>
      request<void>(`/api/v1/secrets/${encodeURIComponent(key)}`, { method: "DELETE" }),
    // Phase 31 / OCS-15: secrets.syncToGateway removed — Phase 29 deleted
    // the gateway sync route; LLM-Provider-Keys now live per-runtime via
    // `runtime_manager.build_runtime_env()`.
  },

  // ── Discord (Phase 29-01 router; singleton guild) ───────────────────────────
  discord: {
    channels: () =>
      request<DiscordChannel[]>("/api/v1/discord/channels"),
    createChannel: (agentId: string, data: { name: string; context?: string; category_id?: string }) =>
      request<{ channel_id: string; name: string }>(
        `/api/v1/discord/agents/${agentId}/channel`,
        { method: "POST", body: JSON.stringify(data) },
      ),
    renameChannel: (agentId: string, data: { new_name: string }) =>
      request<{ ok: true }>(
        `/api/v1/discord/agents/${agentId}/channel`,
        { method: "PATCH", body: JSON.stringify(data) },
      ),
    deleteChannel: (agentId: string) =>
      request<{ ok: true }>(
        `/api/v1/discord/agents/${agentId}/channel`,
        { method: "DELETE" },
      ),
  },

  // ── Credentials ──────────────────────────────────────────────────────────────
  credentials: {
    list: () => request<Credential[]>("/api/v1/credentials"),
    get: (id: string) => request<Credential>(`/api/v1/credentials/${id}`),
    create: (data: { name: string; credential_type: string; data: Record<string, string>; url?: string; notes?: string }) =>
      request<Credential>("/api/v1/credentials", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: { name?: string; credential_type?: string; data?: Record<string, string>; url?: string; notes?: string }) =>
      request<Credential>(`/api/v1/credentials/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    delete: (id: string) =>
      request<void>(`/api/v1/credentials/${id}`, { method: "DELETE" }),
  },

  // ── Settings ─────────────────────────────────────────────────────────────────
  settings: {
    list: () => request<Record<string, unknown>>("/api/v1/settings"),
    get: (key: string) => request<{ key: string; value: unknown }>(`/api/v1/settings/${key}`),
    set: (key: string, value: unknown) =>
      request<{ key: string; value: unknown }>(`/api/v1/settings/${key}`, {
        method: "PUT",
        body: JSON.stringify({ value }),
      }),
    autonomy: () =>
      request<import("./types").AutonomyConfig>("/api/v1/settings/autonomy"),
    updateAutonomy: (levels: Record<string, string>) =>
      request<{ levels: Record<string, string> }>("/api/v1/settings/autonomy", {
        method: "PATCH",
        body: JSON.stringify({ levels }),
      }),
  },

  // ── Analytics (Usage V1) ─────────────────────────────────────────────────
  analytics: {
    usage: (agentId?: string) => {
      const params = new URLSearchParams();
      if (agentId) params.set("agent_id", agentId);
      const qs = params.toString();
      return request<import("./types").UsageAnalytics>(`/api/v1/analytics/usage${qs ? `?${qs}` : ""}`);
    },
  },

  // ── Schedule ─────────────────────────────────────────────────────────────────
  // ── Meetings ───────────────────────────────────────────────────────────────
  meetings: {
    list: (params?: { board_id?: string; status?: string; limit?: number }) => {
      const qs = new URLSearchParams(
        Object.fromEntries(Object.entries(params ?? {}).filter(([, v]) => v != null)) as Record<string, string>,
      ).toString();
      return request<Meeting[]>(`/api/v1/meetings${qs ? `?${qs}` : ""}`);
    },
    get: (id: string) => request<Meeting>(`/api/v1/meetings/${id}`),
    create: (data: { board_id: string; title: string; agenda: string[]; meeting_type?: string; participant_ids?: string[] }) =>
      request<Meeting>("/api/v1/meetings", { method: "POST", body: JSON.stringify(data) }),
    cancel: (id: string) =>
      request<Meeting>(`/api/v1/meetings/${id}/cancel`, { method: "POST" }),
    messages: (id: string, limit = 100) =>
      request<MeetingMessage[]>(`/api/v1/meetings/${id}/messages?limit=${limit}`),
  },

  schedule: {
    listJobs: () =>
      request<ScheduledJob[]>("/api/v1/schedule/jobs"),
    createJob: (data: ScheduledJobCreate) =>
      request<ScheduledJob>("/api/v1/schedule/jobs", { method: "POST", body: JSON.stringify(data) }),
    updateJob: (id: string, data: Partial<ScheduledJobCreate> & { enabled?: boolean }) =>
      request<ScheduledJob>(`/api/v1/schedule/jobs/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    deleteJob: (id: string) =>
      request<void>(`/api/v1/schedule/jobs/${id}`, { method: "DELETE" }),
    triggerJob: (id: string) =>
      request<{ status: string }>(`/api/v1/schedule/jobs/${id}/trigger`, { method: "POST" }),
    getRuns: (id: string, limit = 50) =>
      request<ScheduledJobRun[]>(`/api/v1/schedule/jobs/${id}/runs?limit=${limit}`),
    getStats: (id: string) =>
      request<ScheduleJobStats>(`/api/v1/schedule/jobs/${id}/stats`),
    getHeatmap: (id: string, days = 30) =>
      request<ScheduleHeatmapCell[]>(`/api/v1/schedule/jobs/${id}/heatmap?days=${days}`),
    getCreatedTasks: (id: string, limit = 20) =>
      request<Task[]>(`/api/v1/schedule/jobs/${id}/tasks?limit=${limit}`),
    getUpcoming: (hours = 24) =>
      request<ScheduleUpcomingFiring[]>(`/api/v1/schedule/upcoming?hours=${hours}`),
    previewFirings: (params: { cron?: string; schedule_type: string; schedule_time?: string; schedule_weekdays?: number[]; schedule_interval_hours?: number; count?: number }) =>
      request<ScheduleFiringPreview>(`/api/v1/schedule/preview-firings`, { method: "POST", body: JSON.stringify(params) }),
    snoozeJob: (id: string, hours: number) =>
      request<ScheduledJob>(`/api/v1/schedule/jobs/${id}/snooze`, { method: "PATCH", body: JSON.stringify({ hours }) }),
    duplicateJob: (id: string) =>
      request<ScheduledJob>(`/api/v1/schedule/jobs/${id}/duplicate`, { method: "POST" }),
  },

  // cli-bridge host helper (scripts/cli-bridge.py) — powers the health pill
  // in the create modal + agent detail page.
  cliBridge: {
    health: (): Promise<{ reachable: boolean; bridge_url: string }> =>
      request("/api/v1/cli-bridge/health"),
  },

  runtimes: {
    list: (): Promise<RuntimesResponse> =>
      request("/api/v1/runtimes"),
    // ADR-056 — harness x runtime compatibility matrix for the harness
    // selector in RuntimeSwitchModal.
    compatMatrix: (): Promise<import("@/lib/types").CompatMatrix> =>
      request("/api/v1/runtimes/compat-matrix"),
    addLmstudio: (data: { lms_identifier: string; display_name: string; endpoint?: string }): Promise<Runtime> =>
      request("/api/v1/runtimes", { method: "POST", body: JSON.stringify(data) }),
    vllm: {
      discover: (): Promise<import("@/lib/types").VllmDiscoverResponse> =>
        request("/api/v1/runtimes/vllm/discover"),
      add: (data: {
        container_name: string;
        display_name: string;
        endpoint: string;
        role_tags?: string[];
      }): Promise<Runtime> =>
        request("/api/v1/runtimes/vllm", { method: "POST", body: JSON.stringify(data) }),
    },
    get: (id: string): Promise<Runtime> =>
      request(`/api/v1/runtimes/${id}`),
    health: (id: string): Promise<Runtime> =>
      request(`/api/v1/runtimes/${id}/health`),
    start: (id: string, contextLength?: number): Promise<RuntimeActionResult> =>
      request(`/api/v1/runtimes/${id}/start`, {
        method: "POST",
        body: JSON.stringify({ context_length: contextLength ?? null }),
      }),
    stop: (id: string): Promise<RuntimeActionResult> =>
      request(`/api/v1/runtimes/${id}/stop`, { method: "POST" }),
    restart: (id: string): Promise<RuntimeActionResult> =>
      request(`/api/v1/runtimes/${id}/restart`, { method: "POST" }),
    // Wake a power_managed runtime's host via Wake-on-LAN (e.g. PORSCHE).
    wake: (id: string): Promise<{ ok: boolean; message: string }> =>
      request(`/api/v1/runtimes/${id}/wake`, { method: "POST" }),
    probeModel: (runtimeId: string): Promise<{
      slug: string;
      old_model_identifier: string | null;
      new_model_identifier: string | null;
      changed: boolean;
    }> =>
      request(`/api/v1/runtimes/${runtimeId}/probe-model`, { method: "POST" }),
    liveStatus: () =>
      request<import("@/lib/types").RuntimesLiveResponse>("/api/v1/runtimes/live-status"),
    probeEndpoint: (url: string) =>
      request<import("@/lib/types").ProbeEndpointResult>("/api/v1/runtimes/probe-endpoint", {
        method: "POST",
        body: JSON.stringify({ url }),
      }),
    // Sparkrun recipe management (Phase 35) — applies to vllm_docker runtimes
    // whose launch_command invokes `sparkrun run <recipe>`.
    sparkrun: {
      listRecipes: (): Promise<{
        recipes: { name: string; model: string | null; registry: string }[];
      }> => request("/api/v1/runtimes/sparkrun/recipes"),
      currentRecipe: (runtimeId: string): Promise<{
        slug: string;
        current_recipe: string | null;
        sparkrun_managed: boolean;
      }> => request(`/api/v1/runtimes/${runtimeId}/current-recipe`),
      switchRecipe: (runtimeId: string, recipe: string): Promise<{
        ok: boolean;
        message: string;
        old_recipe: string | null;
        new_recipe: string;
        launch_command: string;
      }> =>
        request(`/api/v1/runtimes/${runtimeId}/switch-recipe`, {
          method: "POST",
          body: JSON.stringify({ recipe }),
        }),
    },
    // DB-backed CRUD (Phase 3) — will become the source of truth once the
    // runtime_manager is fully refactored off the JSON seed.
    db: {
      create: (data: import("@/lib/types").RuntimeCreate): Promise<Runtime> =>
        request("/api/v1/runtimes/db", { method: "POST", body: JSON.stringify(data) }),
      update: (slug: string, data: Partial<import("@/lib/types").RuntimeCreate>): Promise<Runtime> =>
        request(`/api/v1/runtimes/db/${slug}`, { method: "PATCH", body: JSON.stringify(data) }),
      delete: (slug: string): Promise<void> =>
        request(`/api/v1/runtimes/db/${slug}`, { method: "DELETE" }),
      agents: (slug: string): Promise<import("@/lib/types").RuntimeAgentsResponse> =>
        request(`/api/v1/runtimes/db/${slug}/agents`),
      syncAgents: (slug: string) =>
        request<{ synced: boolean }>(`/api/v1/runtimes/db/${slug}/sync-agents`, {
          method: "POST",
        }),
    },
    schedules: {
      list: (runtimeId: string): Promise<RuntimeSchedule[]> =>
        request(`/api/v1/runtimes/${runtimeId}/schedules`),
      create: (runtimeId: string, data: RuntimeScheduleCreate): Promise<RuntimeSchedule> =>
        request(`/api/v1/runtimes/${runtimeId}/schedules`, {
          method: "POST",
          body: JSON.stringify(data),
        }),
      update: (
        runtimeId: string,
        scheduleId: string,
        data: Partial<RuntimeScheduleCreate>
      ): Promise<RuntimeSchedule> =>
        request(`/api/v1/runtimes/${runtimeId}/schedules/${scheduleId}`, {
          method: "PATCH",
          body: JSON.stringify(data),
        }),
      delete: (runtimeId: string, scheduleId: string): Promise<void> =>
        request(`/api/v1/runtimes/${runtimeId}/schedules/${scheduleId}`, {
          method: "DELETE",
        }),
      runs: (runtimeId: string, scheduleId: string): Promise<RuntimeScheduleRun[]> =>
        request(`/api/v1/runtimes/${runtimeId}/schedules/${scheduleId}/runs`),
    },
  },
  lmstudio: {
    list: (): Promise<LMStudioModelsResponse> =>
      request("/api/v1/runtimes/lmstudio/models"),
    load: (modelId: string, contextLength?: number): Promise<RuntimeActionResult> =>
      request("/api/v1/runtimes/lmstudio/load", {
        method: "POST",
        body: JSON.stringify({ model_id: modelId, context_length: contextLength ?? null }),
      }),
    unload: (modelId: string): Promise<RuntimeActionResult> =>
      request("/api/v1/runtimes/lmstudio/unload", {
        method: "POST",
        body: JSON.stringify({ model_id: modelId }),
      }),
    download: (modelId: string, quantization?: string): Promise<RuntimeActionResult> =>
      request("/api/v1/runtimes/lmstudio/download", {
        method: "POST",
        body: JSON.stringify({ model_id: modelId, quantization: quantization ?? null }),
      }),
    delete: (modelId: string): Promise<RuntimeActionResult> =>
      request("/api/v1/runtimes/lmstudio/delete", {
        method: "POST",
        body: JSON.stringify({ model_id: modelId }),
      }),
    kvReset: (): Promise<{ ok: boolean; message: string; reloaded: string[] }> =>
      request("/api/v1/runtimes/lmstudio/kv-reset", { method: "POST" }),
    downloads: (): Promise<{ downloads: import("@/lib/types").LMSActiveDownload[] }> =>
      request("/api/v1/runtimes/lmstudio/downloads"),
    cancelDownload: (modelName: string): Promise<{ ok: boolean; message: string }> =>
      request("/api/v1/runtimes/lmstudio/downloads/cancel", {
        method: "POST",
        body: JSON.stringify({ model_name: modelName }),
      }),
    catalogSearch: (query: string): Promise<{ models: LMSCatalogModel[] }> =>
      request(`/api/v1/runtimes/lmstudio/catalog/search?q=${encodeURIComponent(query)}`),
    hfFiles: (repo: string): Promise<HFRepoInfo> =>
      request(`/api/v1/runtimes/lmstudio/hf/files?repo=${encodeURIComponent(repo)}`),
    downloadHf: (repo: string, filename: string): Promise<RuntimeActionResult> =>
      request("/api/v1/runtimes/lmstudio/download-hf", {
        method: "POST",
        body: JSON.stringify({ repo, filename }),
      }),
  },
  spark: {
    // Back-compat alias — delegates to the host with slug `dgx-spark` (ADR-048).
    metrics: (): Promise<SparkMetrics> =>
      request("/api/v1/runtimes/spark/metrics"),
  },

  // ── Repos (Repo Registry, ADR-050) ─────────────────────────────────────────
  repos: {
    list: (includeInactive = false): Promise<Repo[]> =>
      request(`/api/v1/repos${includeInactive ? "?include_inactive=true" : ""}`),
    get: (id: string): Promise<Repo> => request(`/api/v1/repos/${id}`),
    importCandidates: (): Promise<RepoImportCandidate[]> =>
      request("/api/v1/repos/import-candidates"),
    register: (fullName: string): Promise<Repo> =>
      request("/api/v1/repos", { method: "POST", body: JSON.stringify({ full_name: fullName }) }),
    createNew: (name: string, description?: string): Promise<Repo> =>
      request("/api/v1/repos/new", {
        method: "POST",
        body: JSON.stringify({ name, ...(description ? { description } : {}) }),
      }),
    update: (id: string, data: RepoUpdate): Promise<Repo> =>
      request(`/api/v1/repos/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    remove: (id: string): Promise<void> =>
      request(`/api/v1/repos/${id}`, { method: "DELETE" }),
    sync: (id: string): Promise<Repo> =>
      request(`/api/v1/repos/${id}/sync`, { method: "POST" }),
    linkProject: (id: string, projectId: string): Promise<Repo> =>
      request(`/api/v1/repos/${id}/link-project`, { method: "POST", body: JSON.stringify({ project_id: projectId }) }),
    unlinkProject: (id: string, projectId: string): Promise<Repo> =>
      request(`/api/v1/repos/${id}/link-project/${projectId}`, { method: "DELETE" }),
    // GitHub connection (ADR-055) — probe=true runs a live gh api check (~15s).
    githubStatus: (probe = false): Promise<GithubStatus> =>
      request(`/api/v1/repos/github-status${probe ? "?probe=true" : ""}`),
    setGithubConfig: (data: GithubConfigUpdate): Promise<GithubConfigStatus> =>
      request("/api/v1/repos/github-config", { method: "PUT", body: JSON.stringify(data) }),
  },

  // ── Hosts (Host Registry, ADR-048) ─────────────────────────────────────────
  hosts: {
    list: (): Promise<Host[]> =>
      request("/api/v1/hosts"),
    create: (data: HostCreate): Promise<Host> =>
      request("/api/v1/hosts", { method: "POST", body: JSON.stringify(data) }),
    update: (id: string, data: Partial<HostCreate>): Promise<Host> =>
      request(`/api/v1/hosts/${id}`, { method: "PATCH", body: JSON.stringify(data) }),
    // 409 when runtimes are still bound to the host (rebind first).
    delete: (id: string): Promise<void> =>
      request(`/api/v1/hosts/${id}`, { method: "DELETE" }),
    metrics: (id: string): Promise<HostMetrics> =>
      request(`/api/v1/hosts/${id}/metrics`),
  },

  // ── CLI Sessions (global) ────────────────────────────────────────────────
  cliSessions: {
    list: () => request<CliGlobalSession[]>("/api/v1/cli-sessions"),
    restart: () => request<{ ok: boolean; message: string }>("/api/v1/cli-sessions/restart", { method: "POST" }),
    startShell: (agentId: string) =>
      request<{ ok: boolean; session: string }>(`/api/v1/agents/${agentId}/shell`, { method: "POST" }),
    stopShell: (agentId: string) =>
      request<{ ok: boolean }>(`/api/v1/agents/${agentId}/shell`, { method: "DELETE" }),
    wsUrl: (agentId: string, shell?: boolean): string => {
      const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
      const ws = base.replace(/^http/, "ws");
      const shellParam = shell ? "&shell=1" : "";
      return `${ws}/api/v1/agents/${agentId}/terminal/ws?token=${getToken()}${shellParam}`;
    },
    ptyWsUrl: (agentId: string): string => {
      const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
      const ws = base.replace(/^http/, "ws");
      return `${ws}/api/v1/agents/${agentId}/terminal?token=${getToken()}`;
    },
    hostPtyWsUrl: (agentId: string): string => {
      const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
      const ws = base.replace(/^http/, "ws");
      return `${ws}/api/v1/host-agents/${agentId}/terminal?token=${getToken()}`;
    },
  },

  // ── Browser Live View (view-only CDP screencast) ─────────────────────────
  browserLive: {
    // 502 when the shared cdp-browser container isn't running (browser profile).
    targets: (): Promise<BrowserLiveTarget[]> => request("/api/v1/browser-live/targets"),
  },
};

// Separate helper (not on `api`, mirrors cliSessions.*WsUrl) so components can
// build the WS URL without an extra network round-trip.
export function browserLiveWsUrl(targetId?: string): string {
  const base = (process.env.NEXT_PUBLIC_API_URL ?? "").replace(/\/$/, "");
  const ws = base.replace(/^http/, "ws");
  const targetParam = targetId ? `&target=${encodeURIComponent(targetId)}` : "";
  return `${ws}/api/v1/browser-live/ws?token=${getToken()}${targetParam}`;
}

// ── SSE URLs ──────────────────────────────────────────────────────────────────
export const sseUrls = {
  tasks: (boardId: string) => `${BASE_URL}/api/v1/boards/${boardId}/tasks/stream`,
  agents: () => `${BASE_URL}/api/v1/agents/stream`,
  approvals: () => `${BASE_URL}/api/v1/approvals/stream`,
  activity: () => `${BASE_URL}/api/v1/activity/stream`,
  memory: (boardId: string) => `${BASE_URL}/api/v1/boards/${boardId}/memory/stream`,
  schedule: () => `${BASE_URL}/api/v1/schedule/stream`,
  meetings: () => `${BASE_URL}/api/v1/meetings/stream`,
};
