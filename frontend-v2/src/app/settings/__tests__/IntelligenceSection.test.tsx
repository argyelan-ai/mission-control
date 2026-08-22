import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { AiProviderSettingsResponse, IntelligenceConfig } from "@/lib/types";

// Deep-link straight into ?section=intelligence so only that section mounts.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=intelligence"),
}));

const mockAppState = vi.hoisted(() => ({
  state: {
    activeBoardId: null as string | null,
    sidebarCollapsed: false,
    commandPaletteOpen: false,
    boards: [] as unknown[],
    boardGroups: [] as unknown[],
    currentUser: { id: "u1", email: "a@b.com", name: "Admin", role: "admin" } as {
      id: string; email: string; name: string; role: string;
    } | null,
    setActiveBoardId: (id: string | null) => { mockAppState.state.activeBoardId = id; },
    toggleSidebar: () => {},
    setCommandPaletteOpen: (open: boolean) => { mockAppState.state.commandPaletteOpen = open; },
    setBoards: (boards: unknown[]) => { mockAppState.state.boards = boards; },
    setBoardGroups: (boardGroups: unknown[]) => { mockAppState.state.boardGroups = boardGroups; },
    setCurrentUser: (user: typeof mockAppState.state.currentUser) => { mockAppState.state.currentUser = user; },
  },
}));
vi.mock("@/lib/store", () => ({
  useNotificationStore: Object.assign(
    (selector?: (s: { notifications: never[] }) => unknown) =>
      selector ? selector({ notifications: [] }) : { notifications: [] },
    { getState: () => ({ addNotification: vi.fn() }) }
  ),
  useAppStore: Object.assign(
    (selector?: (s: typeof mockAppState.state) => unknown) =>
      selector ? selector(mockAppState.state) : mockAppState.state,
    { setState: (partial: Partial<typeof mockAppState.state>) => Object.assign(mockAppState.state, partial) }
  ),
}));

import SettingsPage from "../page";

const CONFIG: IntelligenceConfig = {
  enabled: true,
  interval_seconds: 300,
  analysis_window_days: 7,
  ollama_model: "legacy-model",
  temperature: 0.3,
  max_tokens: 2048,
  system_prompt: "",
  outlier_multiplier: 3,
  success_rate_threshold: 0.5,
  failure_count_threshold: 3,
};

const AI_SETTINGS: AiProviderSettingsResponse = {
  values: {
    ai_embeddings_provider: "spark",
    ai_embeddings_url: "",
    ai_embeddings_model: "",
    ai_embeddings_cloud_url: "",
    ai_embeddings_cloud_model: "",
    ai_insights_provider: "ollama_cloud",
    ai_insights_model: "qwen3-coder:480b-cloud",
  },
  overridden: ["ai_insights_provider", "ai_insights_model"],
  insights_effective_model: "qwen3-coder:480b-cloud",
  choices: {
    ai_embeddings_provider: ["spark", "cloud"],
    ai_insights_provider: ["spark", "ollama_cloud", "off"],
  },
  embedding_providers: [],
  state: {
    hf_token_set: false,
    ollama_api_key_set: true,
    embeddings_api_key_set: false,
    embeddings_cloud_api_key_set: false,
    ollama_key_required: true,
    embeddings_cloud_key_required: false,
  },
};

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={qc}>
      <SettingsPage />
    </QueryClientProvider>
  );
}

describe("IntelligenceSection (Settings)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    mockAppState.state.currentUser = { id: "u1", email: "a@b.com", name: "Admin", role: "admin" };

    const store: Record<string, string> = { mc_auth_token: "tok" };
    Object.defineProperty(globalThis, "localStorage", {
      value: {
        getItem: (k: string) => store[k] ?? null,
        setItem: (k: string, v: string) => { store[k] = v; },
        removeItem: (k: string) => { delete store[k]; },
        clear: () => undefined,
      },
      configurable: true,
      writable: true,
    });
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.intelligence, "config").mockResolvedValue(CONFIG);
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue(AI_SETTINGS);
  });

  it("shows the effective insights LLM read-only instead of a second model field", async () => {
    renderPage();

    // The effective routing from the AI-providers page, read-only …
    expect(await screen.findByTestId("insights-llm-effective")).toHaveTextContent(
      "ollama_cloud · qwen3-coder:480b-cloud"
    );
    // … and the old editable duplicate is gone (its legacy value must not
    // surface anywhere as an input).
    expect(screen.queryByDisplayValue("legacy-model")).not.toBeInTheDocument();
  });

  it("jumps to the AI-providers section instead of editing here", async () => {
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: /AI providers/i })
    );
    // The AI-providers tab mounts — its intro heading appears.
    expect(await screen.findByRole("heading", { name: "AI providers" })).toBeInTheDocument();
  });
});
