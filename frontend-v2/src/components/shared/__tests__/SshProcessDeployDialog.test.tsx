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
  slugify,
} from "../SshProcessDeployDialog";
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
});
