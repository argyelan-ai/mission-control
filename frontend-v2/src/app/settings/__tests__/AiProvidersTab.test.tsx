import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type {
  AiProviderSettingsResponse,
  EmbeddingsConnectionResult,
  HfConnectionResult,
  SecretEntry,
} from "@/lib/types";

// Deep-link straight into ?section=ai-providers so only AiProvidersTab mounts.
vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  usePathname: () => "/settings",
  useSearchParams: () => new URLSearchParams("section=ai-providers"),
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

const DEFAULT_STATE: AiProviderSettingsResponse["state"] = {
  hf_token_set: false,
  ollama_api_key_set: false,
  embeddings_api_key_set: false,
  embeddings_cloud_api_key_set: false,
  ollama_key_required: false,
  embeddings_cloud_key_required: false,
};

const DEFAULTS: AiProviderSettingsResponse = {
  values: {
    ai_embeddings_provider: "spark",
    ai_embeddings_url: "",
    ai_embeddings_model: "",
    ai_embeddings_cloud_url: "",
    ai_embeddings_cloud_model: "",
    ai_insights_provider: "spark",
    ai_insights_model: "",
  },
  overridden: [],
  insights_effective_model: null,
  choices: {
    ai_embeddings_provider: ["spark", "cloud"],
    ai_insights_provider: ["spark", "ollama_cloud", "off"],
  },
  embedding_providers: [
    { key: "spark", label: "GPU box", active: true, url: "http://box:1234/v1/embeddings", model: "nomic" },
    { key: "cloud", label: "Cloud", active: false, url: null, model: null },
  ],
  state: DEFAULT_STATE,
};

const HF_ANONYMOUS: HfConnectionResult = {
  token_set: false, connected: false, username: null, error: null, anonymous_ok: true,
};

const EMB_OK: EmbeddingsConnectionResult = {
  provider: "spark",
  label: "GPU box",
  url: "http://box:1234/v1/embeddings",
  model: "nomic-embed",
  connected: true,
  dimension: 768,
  expected_dimension: 768,
  error: null,
};

const HF_SECRET: SecretEntry = {
  id: "s1",
  key: "hf_token",
  value_masked: "****wxyz",
  provider: "huggingface",
  label: "HuggingFace Access Token",
  description: null,
  created_at: null,
  updated_at: null,
};

describe("AiProvidersTab (Settings)", () => {
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
    vi.spyOn(api.secrets, "list").mockResolvedValue([]);
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue(DEFAULTS);
    vi.spyOn(api.aiProviders, "huggingfaceTestConnection").mockResolvedValue(HF_ANONYMOUS);
    vi.spyOn(api.aiProviders, "embeddingsTestConnection").mockResolvedValue(EMB_OK);
  });

  it("shows one provider select per function, seeded from the effective values", async () => {
    renderPage();

    expect(await screen.findByRole("heading", { name: "AI providers" })).toBeInTheDocument();
    const embeddings = await screen.findByTestId("ai_embeddings_provider");
    const insights = screen.getByTestId("ai_insights_provider");
    expect(embeddings).toHaveValue("spark");
    expect(insights).toHaveValue("spark");
    // "off" is a choice for insights only — embeddings without a provider is
    // not a mode, it is a broken memory system.
    expect(Array.from((insights as HTMLSelectElement).options).map((o) => o.value)).toEqual([
      "spark", "ollama_cloud", "off",
    ]);
    expect(Array.from((embeddings as HTMLSelectElement).options).map((o) => o.value)).toEqual([
      "spark", "cloud",
    ]);
  });

  it("shows each arm only its own fields — switching is a complete switch", async () => {
    const { unmount } = renderPage();

    // Self-hosted active: its url/model fields, no cloud fields.
    expect(await screen.findByTestId("ai_embeddings_url")).toBeInTheDocument();
    expect(screen.queryByTestId("ai_embeddings_cloud_url")).not.toBeInTheDocument();
    unmount();

    // Cloud active: ONLY the cloud fields — a self-hosted URL leaking into
    // the cloud arm is the trap this layout exists to prevent.
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue({
      ...DEFAULTS,
      values: { ...DEFAULTS.values, ai_embeddings_provider: "cloud" },
      state: { ...DEFAULT_STATE, embeddings_cloud_key_required: true, embeddings_cloud_api_key_set: true },
    });
    renderPage();
    expect(await screen.findByTestId("ai_embeddings_cloud_url")).toBeInTheDocument();
    expect(screen.getByTestId("ai_embeddings_cloud_model")).toBeInTheDocument();
    expect(screen.queryByTestId("ai_embeddings_url")).not.toBeInTheDocument();
  });

  it("does not leak an unsaved draft into the other arm's field", async () => {
    // Same-position, same-type fields across the two arms: without a key=
    // per arm, React keeps the unsaved draft state through the switch and a
    // typed self-hosted URL surfaces as the cloud endpoint draft.
    const cloudActive: AiProviderSettingsResponse = {
      ...DEFAULTS,
      values: { ...DEFAULTS.values, ai_embeddings_provider: "cloud" },
      state: { ...DEFAULT_STATE, embeddings_cloud_key_required: true, embeddings_cloud_api_key_set: true },
    };
    let switched = false;
    vi.spyOn(api.aiProviders, "getSettings").mockImplementation(async () =>
      switched ? cloudActive : DEFAULTS
    );
    vi.spyOn(api.aiProviders, "updateSettings").mockImplementation(async () => {
      switched = true;
      return { ok: true, applied: ["ai_embeddings_provider"] };
    });
    renderPage();

    const selfHostedUrl = await screen.findByTestId("ai_embeddings_url");
    await userEvent.type(selfHostedUrl, "http://192.0.2.50:8090/v1/embeddings");

    await userEvent.selectOptions(
      screen.getByTestId("ai_embeddings_provider"), "cloud"
    );

    const cloudUrl = await screen.findByTestId("ai_embeddings_cloud_url");
    expect(cloudUrl).toHaveValue("");
  });

  it("warns when the cloud embeddings arm has no stored key", async () => {
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue({
      ...DEFAULTS,
      values: { ...DEFAULTS.values, ai_embeddings_provider: "cloud" },
      state: { ...DEFAULT_STATE, embeddings_cloud_key_required: true },
    });
    renderPage();

    expect(await screen.findByTestId("cloud-emb-key-warning")).toBeInTheDocument();
  });

  it("saves the self-hosted embeddings key under the embeddings provider", async () => {
    const create = vi.spyOn(api.secrets, "create").mockResolvedValue(HF_SECRET);
    renderPage();

    const field = await screen.findByLabelText("Self-hosted — key (optional)");
    await userEvent.type(field, "emb-TESTONLY");
    await userEvent.click(
      within(field.closest("div.mc-card") as HTMLElement).getAllByRole("button", {
        name: "Save",
      })[1]
    );

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        key: "embeddings_api_key", value: "emb-TESTONLY", provider: "embeddings",
      })
    );
  });

  it("saves a provider change through the settings endpoint", async () => {
    const update = vi.spyOn(api.aiProviders, "updateSettings").mockResolvedValue({
      ok: true, applied: ["ai_insights_provider"],
    });
    renderPage();

    await userEvent.selectOptions(await screen.findByTestId("ai_insights_provider"), "off");

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith({ ai_insights_provider: "off" })
    );
  });

  it("marks a pinned value as overridden so the operator sees where it comes from", async () => {
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue({
      ...DEFAULTS,
      values: { ...DEFAULTS.values, ai_embeddings_provider: "cloud" },
      overridden: ["ai_embeddings_provider"],
      state: { ...DEFAULT_STATE, embeddings_cloud_key_required: true, embeddings_cloud_api_key_set: true },
    });
    renderPage();

    expect(await screen.findByTestId("ai_embeddings_provider")).toHaveValue("cloud");
    expect(await screen.findAllByTestId("overridden-badge")).toHaveLength(1);
  });

  it("creates the HF token secret on first save and updates it afterwards", async () => {
    const create = vi.spyOn(api.secrets, "create").mockResolvedValue(HF_SECRET);
    const update = vi.spyOn(api.secrets, "update").mockResolvedValue(HF_SECRET);

    const { unmount } = renderPage();
    const field = await screen.findByLabelText("HuggingFace token");
    await userEvent.type(field, "hf_TESTONLY");
    // Several cards carry a "Save" — scope to the one holding this field.
    await userEvent.click(
      within(field.closest("div.mc-card") as HTMLElement).getAllByRole("button", {
        name: "Save",
      })[0]
    );

    await waitFor(() =>
      expect(create).toHaveBeenCalledWith({
        key: "hf_token", value: "hf_TESTONLY", provider: "huggingface",
      })
    );
    expect(update).not.toHaveBeenCalled();
    unmount();

    // Second round: the secret exists now, so it must PATCH, not 409 on POST.
    vi.spyOn(api.secrets, "list").mockResolvedValue([HF_SECRET]);
    create.mockClear();
    renderPage();
    const again = await screen.findByLabelText("HuggingFace token");
    await userEvent.type(again, "hf_ROTATED");
    await userEvent.click(
      within(again.closest("div.mc-card") as HTMLElement).getAllByRole("button", {
        name: "Save",
      })[0]
    );

    await waitFor(() =>
      expect(update).toHaveBeenCalledWith("hf_token", { value: "hf_ROTATED" })
    );
    expect(create).not.toHaveBeenCalled();
  });

  it("treats a missing HF token as an OK state, not as an error", async () => {
    renderPage();

    expect(
      await screen.findByText("no token — anonymous, public repos only")
    ).toBeInTheDocument();
    expect(screen.queryByTestId("hf-error")).not.toBeInTheDocument();
  });

  it("shows HuggingFace's own failure message instead of a generic error", async () => {
    vi.spyOn(api.aiProviders, "huggingfaceTestConnection").mockResolvedValue({
      ...HF_ANONYMOUS,
      token_set: true,
      error: "Token abgelehnt (401) — abgelaufen oder widerrufen?",
    });
    renderPage();

    expect(await screen.findByText("token rejected")).toBeInTheDocument();
    expect(await screen.findByTestId("hf-error")).toHaveTextContent("401");
  });

  it("does not embed on open — only the button probes the GPU box", async () => {
    const probe = vi.spyOn(api.aiProviders, "embeddingsTestConnection");
    renderPage();

    expect(await screen.findByText("not tested yet")).toBeInTheDocument();
    expect(probe).not.toHaveBeenCalled();

    await userEvent.click(screen.getByRole("button", { name: "Test embeddings" }));

    await waitFor(() => expect(probe).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("GPU box answers (768 dimensions)")).toBeInTheDocument();
  });

  it("reports a dimension mismatch as a warning next to a successful call", async () => {
    vi.spyOn(api.aiProviders, "embeddingsTestConnection").mockResolvedValue({
      ...EMB_OK,
      dimension: 1024,
      error: "Antwort hat 1024 Dimensionen statt 768 — passt nicht zu den bestehenden Vektoren.",
    });
    renderPage();

    await userEvent.click(
      await screen.findByRole("button", { name: "Test embeddings" })
    );

    expect(await screen.findByTestId("embeddings-error")).toHaveTextContent("1024");
  });

  it("warns when insights runs on Ollama Cloud without a stored key", async () => {
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue({
      ...DEFAULTS,
      values: { ...DEFAULTS.values, ai_insights_provider: "ollama_cloud" },
      overridden: ["ai_insights_provider"],
      state: { ...DEFAULT_STATE, ollama_key_required: true },
    });
    renderPage();

    expect(await screen.findByTestId("ollama-key-warning")).toHaveTextContent("401");
  });

  it("hides the warning once the key is stored", async () => {
    vi.spyOn(api.aiProviders, "getSettings").mockResolvedValue({
      ...DEFAULTS,
      state: { ...DEFAULT_STATE, ollama_api_key_set: true, ollama_key_required: true },
    });
    renderPage();

    await screen.findByTestId("ai_embeddings_provider");
    expect(screen.queryByTestId("ollama-key-warning")).not.toBeInTheDocument();
  });
});
