/**
 * ssh_process-Deploy (PR 6) — vitest.
 *
 * Coverage:
 *   1. Credits: „by {author}" mit Link auf jeder Karte, auch ohne Link-URL
 *   2. ssh_process-Karte ist deploybar und öffnet den Installations-Dialog
 *      statt des Rezept-Starts auf einer Box
 *   3. Exklusivitäts-Warnung nennt genau die Runtimes, die der Start stoppt —
 *      und nur die auf DERSELBEN Box
 *   4. „Installieren" ruft den Install-Endpoint und pollt das Live-Log
 *   5. „Runtime anlegen & starten" rendert das Kommando im Backend, legt die
 *      Runtime mit process_name/stop_command/exclusive_memory an und startet
 *      sie über den BESTEHENDEN Start-Endpoint
 *   6. Ohne SSH-Box ist der Weg gesperrt statt scheiternd
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { LocalModelBrowser } from "../LocalModelBrowser";
import {
  SshProcessDeployDialog,
  defaultPortFor,
  exclusiveNeighbours,
  pausedRuntimeDetail,
  slugify,
  splitRecipeNotes,
  sshCapable,
} from "../SshProcessDeployDialog";
import { recipeStartStage } from "../RecipeStartProgress";
import { api } from "@/lib/api";
import type {
  Host,
  LocalRecipe,
  LocalRegistryResponse,
  RecipeInstallLog,
  Runtime,
  RuntimesResponse,
} from "@/lib/types";

const mkRecipe = (overrides: Partial<LocalRecipe> = {}): LocalRecipe => ({
  slug: "deepseek-v4-flash-ds4",
  display_name: "DeepSeek V4 Flash (ds4-server, Spark)",
  description: "DeepSeek V4 Flash on the DwarfStar 4 engine.",
  engine: "ssh_process",
  model_identifier: "DeepSeek-V4-Flash",
  quant: "gguf-asymmetric",
  est_weights_gb: 110,
  min_vram_gb: 120,
  context_len: 262144,
  arch: "arm64",
  gb10_validated: false,
  recipe_ref: null,
  launch_template: "cd {src_dir}/repo && PORT={port} CTX={ctx} ./start.sh",
  install_template: "git clone REPO {src_dir}/repo && cd {src_dir}/repo && ./start.sh",
  stop_template: "cd {src_dir}/repo && PORT={port} ./stop.sh",
  process_name: "ds4-server",
  author: "MiaAI-Lab (engine: DwarfStar 4 by Salvatore Sanfilippo)",
  author_url: "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-One-DGX-Spark",
  source_registry: "builtin",
  source_url: null,
  env: null,
  tags: ["solo"],
  notes: null,
  enabled: true,
  first_seen_at: new Date().toISOString(),
  updated_at: new Date().toISOString(),
  running: false,
  ...overrides,
});

const mkHost = (overrides: Partial<Host> = {}): Host =>
  ({
    id: "host-1",
    slug: "spark",
    display_name: "DGX Spark",
    kind: "ssh",
    ssh_host: "192.0.2.10",
    ssh_user: "mcuser",
    ssh_key_path: null,
    control_url: null,
    wol_mac_address: null,
    power_managed: false,
    notes: null,
    enabled: true,
    ui_order: 0,
    created_at: "",
    updated_at: "",
    ...overrides,
  }) as Host;

const mkRuntime = (overrides: Partial<Runtime> = {}): Runtime =>
  ({
    id: "rt-1",
    slug: "qwen-general",
    display_name: "Qwen General",
    runtime_type: "vllm_docker",
    provider: "vllm",
    endpoint: "http://192.0.2.10:8000/v1",
    healthcheck_path: "/v1/models",
    container_name: null,
    role_tags: [],
    supports_tools: true,
    supports_reasoning: true,
    supports_streaming: true,
    preferred_context_len: 32768,
    max_context_len: 131072,
    gpu_profile: "gb10",
    memory_notes: "",
    startup_notes: "",
    ui_order: 1,
    enabled: true,
    // Default: a genuinely running row — individual tests override `state`
    // to exercise the "old, dead runtime row" case (Befund 5).
    state: "ready",
    ...overrides,
  }) as Runtime;

const mkList = (recipes: LocalRecipe[]): LocalRegistryResponse => ({
  recipes,
  total: recipes.length,
  sources: [],
});
const mkRuntimes = (runtimes: Runtime[]): RuntimesResponse => ({ runtimes });
const mkLog = (overrides: Partial<RecipeInstallLog> = {}): RecipeInstallLog => ({
  host_id: "host-1",
  slug: "deepseek-v4-flash-ds4",
  status: "idle",
  phase: null,
  message: null,
  running: false,
  lines: [],
  cursor: 0,
  ...overrides,
});

function renderDialog(recipe = mkRecipe(), onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <SshProcessDeployDialog recipe={recipe} onClose={onClose} />
    </QueryClientProvider>,
  );
}

function renderBrowser() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <LocalModelBrowser />
    </QueryClientProvider>,
  );
}

describe("ssh_process deploy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  // ── Pure Logik ────────────────────────────────────────────────────────────

  it("lists only the exclusive runtimes on the same box", () => {
    const runtimes = [
      mkRuntime({ id: "a", display_name: "A", exclusive_memory: true, host: { id: "host-1" } as Runtime["host"] }),
      mkRuntime({ id: "b", display_name: "B", exclusive_memory: true, host: { id: "host-2" } as Runtime["host"] }),
      mkRuntime({ id: "c", display_name: "C", exclusive_memory: false, host: { id: "host-1" } as Runtime["host"] }),
      mkRuntime({ id: "d", display_name: "D", exclusive_memory: true, enabled: false, host: { id: "host-1" } as Runtime["host"] }),
    ];
    expect(exclusiveNeighbours(runtimes, "host-1").map((r) => r.display_name)).toEqual(["A"]);
    expect(exclusiveNeighbours(runtimes, null)).toEqual([]);
  });

  // Befund 5 (18.10.2026): the live GLM-5.3-Flash-TensorFold deploy showed 5
  // old, inactive runtime rows in this warning — enabled rows left over from
  // earlier setups that are not actually running any more.
  it("excludes an enabled-but-not-running row, and never a slot row", () => {
    const runtimes = [
      mkRuntime({
        id: "live",
        display_name: "Qwen General",
        exclusive_memory: true,
        state: "ready",
        host: { id: "host-1" } as Runtime["host"],
      }),
      mkRuntime({
        id: "stale",
        display_name: "Old DeepSeek Setup",
        exclusive_memory: true,
        enabled: true,
        state: "stopped",
        host: { id: "host-1" } as Runtime["host"],
      }),
      mkRuntime({
        id: "unknown-state",
        display_name: "Never Probed",
        exclusive_memory: true,
        enabled: true,
        state: "unknown",
        host: { id: "host-1" } as Runtime["host"],
      }),
      mkRuntime({
        id: "slot",
        display_name: "Box Slot",
        exclusive_memory: true,
        enabled: true,
        state: "ready",
        is_slot: true,
        host: { id: "host-1" } as Runtime["host"],
      }),
    ];
    expect(exclusiveNeighbours(runtimes, "host-1").map((r) => r.display_name)).toEqual([
      "Qwen General",
    ]);
  });

  it("slugifies a display name", () => {
    expect(slugify("DeepSeek V4 Flash (ds4)")).toBe("deepseek-v4-flash-ds4");
  });

  // ── Credits ───────────────────────────────────────────────────────────────

  it("shows the author with a link on the card", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(mkList([mkRecipe()]));
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    renderBrowser();

    const credit = await screen.findByTestId("local-registry-author");
    expect(credit.textContent).toContain("MiaAI-Lab");
    const link = credit.querySelector("a");
    expect(link).toHaveAttribute(
      "href",
      "https://github.com/MiaAI-Lab/DeepSeek-v4-Flash-One-DGX-Spark",
    );
    expect(link).toHaveAttribute("target", "_blank");
  });

  it("shows the author as plain text when no link is known", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(
      mkList([mkRecipe({ author: "Some Lab", author_url: null })]),
    );
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    renderBrowser();

    const credit = await screen.findByTestId("local-registry-author");
    expect(credit.textContent).toContain("Some Lab");
    expect(credit.querySelector("a")).toBeNull();
  });

  it("renders no credit line for an entry without an author", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(
      mkList([mkRecipe({ author: null, author_url: null })]),
    );
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    renderBrowser();

    await screen.findByTestId("local-recipe-card");
    expect(screen.queryByTestId("local-registry-author")).toBeNull();
  });

  // ── Routing zum richtigen Dialog ──────────────────────────────────────────

  it("opens the install dialog instead of the box recipe start", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(mkList([mkRecipe()]));
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    const startRecipe = vi.spyOn(api.hosts, "startRecipe");
    renderBrowser();

    await userEvent.click(await screen.findByTestId("local-registry-deploy"));

    await screen.findByTestId("ssh-deploy-install");
    expect(startRecipe).not.toHaveBeenCalled();
  });

  it("keeps an ssh_process entry without a launch template undeployable", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(
      mkList([mkRecipe({ launch_template: null })]),
    );
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    renderBrowser();

    expect(await screen.findByTestId("local-registry-deploy")).toBeDisabled();
  });

  // ── Dialog ────────────────────────────────────────────────────────────────

  it("warns about what the start will stop on that box", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    vi.spyOn(api.runtimes, "list").mockResolvedValue(
      mkRuntimes([
        mkRuntime({
          display_name: "Qwen General",
          exclusive_memory: true,
          host: { id: "host-1" } as Runtime["host"],
        }),
        mkRuntime({
          id: "rt-2",
          display_name: "Other Box Model",
          exclusive_memory: true,
          host: { id: "host-9" } as Runtime["host"],
        }),
      ]),
    );
    renderDialog();

    const warning = await screen.findByTestId("ssh-deploy-exclusive-warning");
    expect(warning.textContent).toContain("Qwen General");
    expect(warning.textContent).not.toContain("Other Box Model");
  });

  it("starts the install job and follows the log", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    const install = vi
      .spyOn(api.localRegistry, "install")
      .mockResolvedValue({ status: "started", host_id: "host-1", slug: "deepseek-v4-flash-ds4" });
    // Erst „idle" (nichts lief bisher), ab dem Klick der laufende Job — sonst
    // wäre der Knopf schon beim Öffnen als „läuft" gesperrt.
    vi.spyOn(api.localRegistry, "installLog")
      .mockResolvedValueOnce(mkLog())
      .mockResolvedValue(
        mkLog({
          status: "running",
          running: true,
          lines: [{ ts: 1, level: "info", text: "Cloning into 'ds4'..." }],
          cursor: 1,
        }),
      );
    renderDialog();

    // Erst wenn die Box-Liste da ist, ist der Knopf aktiv — vorher klickt der
    // Test ins Leere und die Zusicherung wäre wertlos.
    const button = await screen.findByTestId("ssh-deploy-install");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() =>
      expect(install).toHaveBeenCalledWith("deepseek-v4-flash-ds4", {
        host_id: "host-1",
        port: 8888,
        ctx: 262144,
      }),
    );
    const log = await screen.findByTestId("ssh-deploy-install-log");
    expect(log.textContent).toContain("Cloning into");
  });

  it("surfaces a rejected install (409) instead of pretending it started", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    vi.spyOn(api.localRegistry, "install").mockRejectedValue(
      new Error('409 {"detail":"Für \'ds4\' läuft bereits eine Installation."}'),
    );
    renderDialog();

    const button = await screen.findByTestId("ssh-deploy-install");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    const error = await screen.findByTestId("ssh-deploy-error");
    expect(error.textContent).toContain("läuft bereits eine Installation");
  });

  it("creates the runtime from the backend-rendered command and starts it", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog({ status: "done" }));
    const render_ = vi.spyOn(api.hosts, "launchCommand").mockResolvedValue({
      launch_command: "cd ~/code/mc-engines/repo && PORT=8888 CTX=262144 ./start.sh",
      stop_command: "cd ~/code/mc-engines/repo && PORT=8888 ./stop.sh",
    });
    const create = vi
      .spyOn(api.runtimes, "create")
      .mockResolvedValue({ id: "rt-new" } as Runtime);
    const start = vi
      .spyOn(api.runtimes, "start")
      .mockResolvedValue({ ok: true, message: "started" });

    renderDialog();
    const button = await screen.findByTestId("ssh-deploy-create");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() => expect(create).toHaveBeenCalled());
    // Das Kommando kommt vom Renderer im Backend, nie aus dem Browser.
    expect(render_).toHaveBeenCalledWith(
      expect.objectContaining({ engine: "ssh_process", port: 8888, ctx: 262144 }),
    );
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        runtime_type: "ssh_process",
        process_name: "ds4-server",
        stop_command: "cd ~/code/mc-engines/repo && PORT=8888 ./stop.sh",
        exclusive_memory: true,
        endpoint: "http://192.0.2.10:8888/v1",
        host_id: "host-1",
      }),
    );
    // Bestehender Start-Endpoint — kein zweiter Lifecycle-Pfad.
    expect(start).toHaveBeenCalledWith("rt-new");
    await screen.findByTestId("ssh-deploy-created");
  });

  // ── Two-box recipes (topology.nodes >= 2) ─────────────────────────────────

  it("shows the short description up front, with the licence notes behind Details", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    const install = vi.spyOn(api.localRegistry, "install");
    renderDialog(
      mkRecipe({ notes: "LICENCE - NON-COMMERCIAL USE ONLY (CC BY-NC-ND drafter)" }),
    );

    // The one-sentence description is visible right away — no click needed.
    expect(await screen.findByText(/DwarfStar 4 engine/)).toBeInTheDocument();
    // The long notes wall is not in the document until "Details" is opened.
    expect(screen.queryByText(/NON-COMMERCIAL USE ONLY/)).not.toBeInTheDocument();

    await userEvent.click(screen.getByTestId("ssh-deploy-notes-toggle"));
    expect(await screen.findByText(/NON-COMMERCIAL USE ONLY/)).toBeInTheDocument();
    expect(install).not.toHaveBeenCalled();
  });

  it("splits a wall of recipe notes into paragraphs by section label", () => {
    const tensorfoldNotes =
      "LICENCE - NON-COMMERCIAL USE ONLY (CC BY-NC-ND drafter): the default drafter is " +
      "licensed CC BY-NC-ND 4.0, so this recipe as shipped may only be used non-commercially. " +
      "PREREQUISITES: 2x GB10 with 128 GB; cabled over QSFP. " +
      "INSTALL (hours, once): MC writes the .env on the head and pulls the image. " +
      "START: ./start.sh with PREPARE=0; ready when GET /v1/models answers on port 8000.";
    const paragraphs = splitRecipeNotes(tensorfoldNotes);
    expect(paragraphs).toHaveLength(4);
    expect(paragraphs[0]).toMatch(/^LICENCE/);
    expect(paragraphs[0]).toMatch(/non-commercially\.$/);
    expect(paragraphs[1]).toMatch(/^PREREQUISITES:/);
    expect(paragraphs[2]).toMatch(/^INSTALL/);
    expect(paragraphs[3]).toMatch(/^START:/);
  });

  it("keeps a notes text with no section labels as a single paragraph", () => {
    expect(splitRecipeNotes("Just a short, plain note with no headers.")).toEqual([
      "Just a short, plain note with no headers.",
    ]);
    expect(splitRecipeNotes("")).toEqual([]);
  });

  it("installs a two-box recipe on its own port and starts it through the recipe switcher", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      mkHost(),
      mkHost({ id: "host-2", slug: "spark-b", display_name: "Spark B", ssh_host: "192.0.2.11" }),
    ]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog({ status: "done" }));
    const install = vi.spyOn(api.localRegistry, "install").mockResolvedValue({
      status: "started",
      host_id: "host-1",
      slug: "duo-recipe",
      worker_host_id: "host-2",
      worker_slug: "spark-b",
    });
    const startRecipe = vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({
      ok: true,
      created: true,
      runtime_slug: "duo-recipe-spark",
    } as Awaited<ReturnType<typeof api.hosts.startRecipe>>);
    const create = vi.spyOn(api.runtimes, "create");
    const launch = vi.spyOn(api.hosts, "launchCommand");

    renderDialog(mkRecipe({ slug: "duo-recipe", topology: { nodes: 2 }, port: 8000 }));

    const installButton = await screen.findByTestId("ssh-deploy-install");
    await waitFor(() => expect(installButton).toBeEnabled());
    await userEvent.click(installButton);
    await waitFor(() =>
      expect(install).toHaveBeenCalledWith("duo-recipe", {
        host_id: "host-1",
        port: 8000,
        ctx: 262144,
      }),
    );

    const createButton = await screen.findByTestId("ssh-deploy-create");
    await waitFor(() => expect(createButton).toBeEnabled());
    await userEvent.click(createButton);

    // The duo start goes through the switcher (worker, .env, membership) with
    // the worker the install used — never through a bare POST /runtimes.
    await waitFor(() =>
      expect(startRecipe).toHaveBeenCalledWith("host-1", "duo-recipe", { worker_host_id: "host-2" }),
    );
    expect(create).not.toHaveBeenCalled();
    expect(launch).not.toHaveBeenCalled();
    await screen.findByTestId("ssh-deploy-created");
  });

  // ── Engine-Tuning (PR 8) ──────────────────────────────────────────────────

  it("shows the recipe env before anything is deployed", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    renderDialog(
      mkRecipe({ env: { GPU_MEMORY_UTILIZATION: "0.895", MAX_NUM_SEQS: "2" } }),
    );

    const block = await screen.findByTestId("ssh-deploy-env");
    // Genau die Werte, die auf der Box landen — sortiert wie im Override.
    expect(block.textContent).toContain("GPU_MEMORY_UTILIZATION=0.895");
    expect(block.textContent).toContain("MAX_NUM_SEQS=2");
  });

  it("hides the tuning block for a recipe without env", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    renderDialog(mkRecipe({ env: null }));

    await screen.findByTestId("ssh-deploy-install");
    expect(screen.queryByTestId("ssh-deploy-env")).toBeNull();
  });

  it("passes the env to the backend renderer", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog({ status: "done" }));
    const render_ = vi.spyOn(api.hosts, "launchCommand").mockResolvedValue({
      launch_command: "cd repo && docker compose $ARGS up -d",
      stop_command: null,
    });
    vi.spyOn(api.runtimes, "create").mockResolvedValue({ id: "rt-new" } as Runtime);
    vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "started" });

    const env = { MODE: "mtp0" };
    renderDialog(mkRecipe({ env }));
    const button = await screen.findByTestId("ssh-deploy-create");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    // Ohne diese Zeile stünde das Tuning in der DB und nicht auf der Box.
    await waitFor(() =>
      expect(render_).toHaveBeenCalledWith(expect.objectContaining({ env })),
    );
  });

  it("reports a failed start instead of claiming success", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    vi.spyOn(api.hosts, "launchCommand").mockResolvedValue({
      launch_command: "cd x && ./start.sh",
      stop_command: null,
    });
    vi.spyOn(api.runtimes, "create").mockResolvedValue({ id: "rt-new" } as Runtime);
    vi.spyOn(api.runtimes, "start").mockRejectedValue(
      new Error('400 {"detail":"Qwen General läuft noch und konnte nicht gestoppt werden."}'),
    );

    renderDialog();
    const button = await screen.findByTestId("ssh-deploy-create");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    const error = await screen.findByTestId("ssh-deploy-error");
    expect(error.textContent).toContain("konnte nicht gestoppt werden");
    expect(screen.queryByTestId("ssh-deploy-created")).toBeNull();
  });

  // P2: gepaarte Boxen (kind=agent) mit SSH-Adresse sind Startziele —
  // dieselbe Regel wie im Backend. Ohne Adresse melden sie nur.
  it("sshCapable — ssh or agent WITH an ssh_host; everything else is no start target", () => {
    expect(sshCapable(mkHost({ kind: "ssh", ssh_host: "192.0.2.10" }))).toBe(true);
    expect(sshCapable(mkHost({ kind: "agent", ssh_host: "192.0.2.22" }))).toBe(true);
    expect(sshCapable(mkHost({ kind: "agent", ssh_host: null }))).toBe(false);
    expect(sshCapable(mkHost({ kind: "ssh", ssh_host: null }))).toBe(false);
    expect(sshCapable(mkHost({ kind: "local", ssh_host: "192.0.2.10" }))).toBe(false);
    expect(sshCapable(mkHost({ kind: "flask_wol", ssh_host: null }))).toBe(false);
  });

  it("offers a paired (agent) box with an SSH address as start target, but not one without", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([
      mkHost({ id: "host-b", slug: "box-b", display_name: "Box B", kind: "agent", ssh_host: "192.0.2.22" }),
      mkHost({ id: "host-c", slug: "box-c", display_name: "Box C", kind: "agent", ssh_host: null }),
    ]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue({ runtimes: [] } as never);
    renderDialog();
    expect(await screen.findByText("Box B")).toBeInTheDocument();
    expect(screen.queryByText("Box C")).toBeNull();
  });

  it("blocks both actions when no SSH box is connected", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost({ kind: "local" })]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    renderDialog();

    await screen.findByTestId("ssh-deploy-no-hosts");
    await waitFor(() => expect(screen.getByTestId("ssh-deploy-install")).toBeDisabled());
    expect(screen.getByTestId("ssh-deploy-create")).toBeDisabled();
  });
});

/**
 * Compose-basierte Rezepte (PR 7) — derselbe Dialog, andere Engine.
 *
 * Der sparkinfer-Stack ist `vllm_docker`, kommt aber wie ds4 über den
 * Install-Dialog: er bringt install_template UND launch_template mit. Was hier
 * abgesichert wird, ist genau das, was auf einer echten Box schiefgehen würde:
 *   1. die Karte ist überhaupt deploybar und landet im richtigen Dialog
 *   2. der Port steht auf 8000 — der Stack kann wegen `network_mode: host`
 *      nirgendwo anders hören, und ein falscher Port ergibt einen Endpoint,
 *      der nie antwortet
 *   3. die Runtime bekommt container_name UND exclusive_memory — ohne den
 *      Namen liest get_runtime_state dauerhaft „unknown", ohne das Flag
 *      startet der 107-GB-Stack neben einem anderen Modell
 */
const mkComposeRecipe = (overrides: Partial<LocalRecipe> = {}): LocalRecipe =>
  mkRecipe({
    slug: "deepseek-v4-flash-sparkinfer",
    display_name: "DeepSeek V4 Flash (SparkInfer, Spark)",
    engine: "vllm_docker",
    model_identifier: "0xSero/deepseek-v4-flash-0731-spark",
    quant: "exl3",
    est_weights_gb: 107,
    min_vram_gb: 115,
    process_name: null,
    recipe_ref: null,
    install_template:
      "git clone REPO {src_dir}/repo && cd {src_dir}/repo && printf 'labels: mc.runtime.slug={slug}' > compose.override.yaml && docker compose up -d",
    launch_template:
      "cd {src_dir}/repo && printf 'labels: mc.runtime.slug={slug}' > compose.override.yaml && docker compose up -d",
    stop_template: "cd {src_dir}/repo && docker compose stop",
    author: "0xSero + Local Inference Lab (SparkInfer)",
    author_url: "https://github.com/0xSero/deepseek-v4-flash-0731-spark-sparkinfer",
    ...overrides,
  });

describe("compose-based deploy", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("defaults to 8000 for container engines and 8888 for host processes", () => {
    expect(defaultPortFor(mkComposeRecipe())).toBe(8000);
    expect(defaultPortFor(mkRecipe())).toBe(8888);
  });

  it("makes a self-installing vllm_docker entry deployable", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(mkList([mkComposeRecipe()]));
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    renderBrowser();

    expect(await screen.findByTestId("local-registry-deploy")).toBeEnabled();
  });

  it("routes it to the install dialog, not the box recipe start", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(mkList([mkComposeRecipe()]));
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog());
    const startRecipe = vi.spyOn(api.hosts, "startRecipe");
    renderBrowser();

    await userEvent.click(await screen.findByTestId("local-registry-deploy"));

    await screen.findByTestId("ssh-deploy-install");
    expect(startRecipe).not.toHaveBeenCalled();
  });

  it("keeps a vllm_docker entry without templates on the wizard path", async () => {
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(
      mkList([mkComposeRecipe({ install_template: null, launch_template: null })]),
    );
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    renderBrowser();

    expect(await screen.findByTestId("local-registry-deploy")).toBeDisabled();
  });

  it("creates the runtime with a container name and exclusive memory", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog({ status: "done" }));
    const render_ = vi.spyOn(api.hosts, "launchCommand").mockResolvedValue({
      launch_command:
        "cd ~/code/mc-engines/repo && printf '...' > compose.override.yaml && docker compose up -d",
      stop_command: "cd ~/code/mc-engines/repo && docker compose stop",
    });
    const create = vi
      .spyOn(api.runtimes, "create")
      .mockResolvedValue({ id: "rt-new" } as Runtime);
    const start = vi
      .spyOn(api.runtimes, "start")
      .mockResolvedValue({ ok: true, message: "started" });

    renderDialog(mkComposeRecipe());
    const button = await screen.findByTestId("ssh-deploy-create");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(render_).toHaveBeenCalledWith(
      expect.objectContaining({ engine: "vllm_docker", port: 8000 }),
    );
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        runtime_type: "vllm_docker",
        // Muss exakt dem container_name im compose.override.yaml entsprechen.
        container_name: "mc-deepseek-v4-flash-sparkinfer",
        exclusive_memory: true,
        stop_command: "cd ~/code/mc-engines/repo && docker compose stop",
        endpoint: "http://192.0.2.10:8000/v1",
        host_id: "host-1",
      }),
    );
    // Ein Host-Prozess-Feld hat hier nichts verloren.
    expect(create.mock.calls[0][0].process_name).toBeNull();
    expect(start).toHaveBeenCalledWith("rt-new");
  });

  it("sends no container name for a host-process engine", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
    vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(mkLog({ status: "done" }));
    vi.spyOn(api.hosts, "launchCommand").mockResolvedValue({
      launch_command: "cd x && ./start.sh",
      stop_command: null,
    });
    const create = vi
      .spyOn(api.runtimes, "create")
      .mockResolvedValue({ id: "rt-new" } as Runtime);
    vi.spyOn(api.runtimes, "start").mockResolvedValue({ ok: true, message: "started" });

    renderDialog(mkRecipe());
    const button = await screen.findByTestId("ssh-deploy-create");
    await waitFor(() => expect(button).toBeEnabled());
    await userEvent.click(button);

    await waitFor(() => expect(create).toHaveBeenCalled());
    expect(create.mock.calls[0][0].container_name).toBeUndefined();
  });

  // ── Start feedback + paused instance (field findings 2026-10-18) ──────────

  describe("start feedback", () => {
    const duo = () => mkRecipe({ slug: "duo-recipe", topology: { nodes: 2 }, port: 8000 });
    const liveOf = (entry: Record<string, unknown> | null) => ({
      live: entry ? { "duo-recipe-spark": entry } : {},
      watcher_enabled: true,
      interval: 30,
    });

    function mockBase() {
      vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
      vi.spyOn(api.runtimes, "list").mockResolvedValue(mkRuntimes([]));
      vi.spyOn(api.localRegistry, "installLog").mockResolvedValue(
        mkLog({ status: "done", lines: [{ ts: null, level: "info", text: "old install line" }] }),
      );
    }

    it("maps the watcher's live status to one stage", () => {
      expect(recipeStartStage(true, false, undefined)).toBe("sending");
      expect(recipeStartStage(false, true, undefined)).toBe("already");
      expect(recipeStartStage(false, false, undefined)).toBe("waiting");
      const base = { reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 0, drift: false };
      expect(recipeStartStage(false, false, { ...base, status: "switching", phase: "evicting" })).toBe("evicting");
      expect(recipeStartStage(false, false, { ...base, status: "switching", phase: "loading" })).toBe("loading");
      expect(recipeStartStage(false, false, { ...base, status: "switching", phase: "warming" })).toBe("starting");
      expect(recipeStartStage(false, false, { ...base, reachable: true, status: "ok" })).toBe("serving");
      expect(recipeStartStage(false, false, { ...base, status: "unreachable" })).toBe("quiet");
    });

    it("shows the start's progress instead of the old install log after the click", async () => {
      mockBase();
      vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({
        ok: true,
        created: false,
        runtime_slug: "duo-recipe-spark",
      } as Awaited<ReturnType<typeof api.hosts.startRecipe>>);
      const live = vi
        .spyOn(api.runtimes, "liveStatus")
        .mockResolvedValue(liveOf({ reachable: false, status: "switching", phase: "loading" }) as never);

      renderDialog(duo());
      await screen.findByTestId("ssh-deploy-install-log");
      const create = await screen.findByTestId("ssh-deploy-create");
      await waitFor(() => expect(create).toBeEnabled());
      await userEvent.click(create);

      const progress = await screen.findByTestId("recipe-start-progress");
      await waitFor(() => expect(progress.dataset.stage).toBe("loading"));
      expect(progress.textContent).toContain("Loading the weights");
      expect(live).toHaveBeenCalled();
      // The old install log is history now — it no longer hides the start.
      expect(screen.queryByTestId("ssh-deploy-install-log")).toBeNull();
    });

    it("says when the engine answers", async () => {
      mockBase();
      vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({
        ok: true,
        runtime_slug: "duo-recipe-spark",
      } as Awaited<ReturnType<typeof api.hosts.startRecipe>>);
      vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue(
        liveOf({ reachable: true, status: "ok", served_model: "org/duo" }) as never,
      );

      renderDialog(duo());
      const create = await screen.findByTestId("ssh-deploy-create");
      await waitFor(() => expect(create).toBeEnabled());
      await userEvent.click(create);

      const progress = await screen.findByTestId("recipe-start-progress");
      await waitFor(() => expect(progress.dataset.stage).toBe("serving"));
      expect(progress.textContent).toContain("org/duo");
    });

    it("shows a sending state while the start request runs", async () => {
      mockBase();
      let release: (v: unknown) => void = () => undefined;
      vi.spyOn(api.hosts, "startRecipe").mockReturnValue(
        new Promise((resolve) => {
          release = resolve;
        }) as never,
      );
      vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue(liveOf(null) as never);

      renderDialog(duo());
      const create = await screen.findByTestId("ssh-deploy-create");
      await waitFor(() => expect(create).toBeEnabled());
      await userEvent.click(create);

      const progress = await screen.findByTestId("recipe-start-progress");
      expect(progress.dataset.stage).toBe("sending");
      release({ ok: true, runtime_slug: "duo-recipe-spark" });
      await waitFor(() => expect(progress.dataset.stage).toBe("waiting"));
    });

    it("parses the paused 409 and nothing else", () => {
      const err = new Error(
        'API 409: {"detail":{"code":"runtime_paused","runtime_id":"rt-9","runtime_slug":"duo-recipe-spark","message":"paused"}}',
      );
      expect(pausedRuntimeDetail(err)?.runtime_slug).toBe("duo-recipe-spark");
      expect(pausedRuntimeDetail(new Error('API 409: {"detail":"busy"}'))).toBeNull();
      expect(pausedRuntimeDetail(new Error("boom"))).toBeNull();
    });

    it("offers an explicit resume for a paused instance instead of a second runtime", async () => {
      mockBase();
      const start = vi
        .spyOn(api.hosts, "startRecipe")
        .mockRejectedValueOnce(
          new Error(
            'API 409: {"detail":{"code":"runtime_paused","runtime_id":"rt-9","runtime_slug":"duo-recipe-spark","message":"paused"}}',
          ),
        )
        .mockResolvedValueOnce({
          ok: true,
          resumed: true,
          runtime_slug: "duo-recipe-spark",
        } as Awaited<ReturnType<typeof api.hosts.startRecipe>>);
      vi.spyOn(api.runtimes, "liveStatus").mockResolvedValue(liveOf(null) as never);

      renderDialog(duo());
      const create = await screen.findByTestId("ssh-deploy-create");
      await waitFor(() => expect(create).toBeEnabled());
      await userEvent.click(create);

      const notice = await screen.findByTestId("ssh-deploy-paused");
      expect(notice.textContent).toContain("duo-recipe-spark");
      expect(screen.queryByTestId("ssh-deploy-error")).toBeNull();
      expect(screen.queryByTestId("recipe-start-progress")).toBeNull();
      expect(start).toHaveBeenLastCalledWith("host-1", "duo-recipe", undefined);

      await userEvent.click(screen.getByTestId("ssh-deploy-resume"));
      await waitFor(() =>
        expect(start).toHaveBeenLastCalledWith("host-1", "duo-recipe", { resume_paused: true }),
      );
      await screen.findByTestId("ssh-deploy-created");
      expect(screen.queryByTestId("ssh-deploy-paused")).toBeNull();
    });
  });
});
