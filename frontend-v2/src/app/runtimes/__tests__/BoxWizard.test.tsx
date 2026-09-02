/**
 * BoxWizard — vitest.
 *
 * Abdeckung:
 *   1. Reine Logik: arch-Mapping, nutzbarer Speicher, Passt-Check, Bootstrap-Bedarf
 *   2. Step-Gating (canProceed) inkl. „Probe-Fehler blockiert Schritt 2"
 *   3. Übergang 1→2 legt die Host-Zeile an (POST /hosts) — erst nach dem Probe
 *   4. Ampel-Logik in Schritt 2 aus dem echten Inventar
 *   5. Schritt 3: arch-Filter am Backend, sparkrun raus, VRAM-Warnung gegen
 *      die ECHTEN Werte der Box
 *   6. Abschluss ruft POST /runtimes + start mit den korrekten Argumenten
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  BoxWizard,
  archFilter,
  canProceed,
  initialBoxWizardState,
  needsBootstrap,
  recipeFits,
  slugify,
  usableMemoryGb,
  type BoxWizardState,
} from "../BoxWizard";
import { api } from "@/lib/api";
import type {
  Host,
  HostProbeResult,
  LocalRecipe,
  LocalRegistryResponse,
  Runtime,
} from "@/lib/types";

// ── Fixtures ─────────────────────────────────────────────────────────────────

const mkProbe = (overrides: Partial<HostProbeResult> = {}): HostProbeResult => ({
  reachable: true,
  reason: null,
  arch: "aarch64",
  os: "Linux",
  kernel: "6.11.0",
  user: "mcuser",
  gpus: [{ name: "NVIDIA GB10", vram_gb: 128 }],
  nvidia_smi: true,
  docker: {
    installed: true,
    version: "Docker version 27.3.1",
    nvidia_runtime: true,
    runtimes: "map[nvidia runc]",
    toolkit_installed: true,
  },
  disk_free_gb: 800,
  ram_gb: 120,
  in_docker_group: true,
  sudo_nopasswd: true,
  pkg_manager: "/usr/bin/apt-get",
  raw: "",
  ...overrides,
});

const mkRecipe = (overrides: Partial<LocalRecipe> = {}): LocalRecipe => ({
  slug: "qwen3-8b-gguf-q4",
  display_name: "Qwen3 8B GGUF",
  description: null,
  engine: "llamacpp_docker",
  model_identifier: "Qwen/Qwen3-8B-GGUF",
  quant: "q4_k_m",
  est_weights_gb: 5,
  min_vram_gb: 8,
  context_len: 32768,
  arch: "any",
  gb10_validated: false,
  recipe_ref: null,
  launch_template: null,
  install_template: null,
  stop_template: null,
  process_name: null,
  author: null,
  author_url: null,
  source_registry: "builtin",
  source_url: null,
  env: null,
  tags: [],
  notes: null,
  enabled: true,
  first_seen_at: null,
  updated_at: null,
  running: false,
  ...overrides,
});

const mkRegistry = (recipes: LocalRecipe[]): LocalRegistryResponse => ({
  recipes,
  total: recipes.length,
  sources: [],
});

const mkHost = (): Host =>
  ({
    id: "host-1",
    slug: "dgx-spark",
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
  }) as Host;

const mkState = (overrides: Partial<BoxWizardState> = {}): BoxWizardState => ({
  ...initialBoxWizardState(),
  ...overrides,
});

function renderWizard() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <BoxWizard onClose={() => {}} />
    </QueryClientProvider>,
  );
}

/** Schritt 1 ausfüllen, Probe auslösen, auf „Weiter" klicken. */
async function walkToStep2(user: ReturnType<typeof userEvent.setup>) {
  await user.type(screen.getByLabelText("Display name"), "DGX Spark");
  await user.type(screen.getByLabelText("SSH host"), "192.0.2.10");
  await user.click(screen.getByTestId("box-wizard-probe"));
  await waitFor(() =>
    expect(screen.getByTestId("box-wizard-inventory")).toBeInTheDocument(),
  );
  await user.click(screen.getByTestId("box-wizard-next"));
}

// ── 1. Reine Logik ───────────────────────────────────────────────────────────

describe("BoxWizard — pure logic", () => {
  it("maps uname -m to the registry's arch vocabulary", () => {
    expect(archFilter("aarch64")).toBe("arm64");
    expect(archFilter("x86_64")).toBe("x86_64");
    expect(archFilter("amd64")).toBe("x86_64");
    // Unbekannt → kein Filter, lieber alles zeigen als fälschlich filtern.
    expect(archFilter("riscv64")).toBeUndefined();
    expect(archFilter(null)).toBeUndefined();
  });

  it("uses the real GPU memory, falling back to RAM", () => {
    expect(usableMemoryGb(mkProbe())).toBe(128);
    // Zwei Karten werden addiert.
    expect(
      usableMemoryGb(
        mkProbe({
          gpus: [
            { name: "A", vram_gb: 48 },
            { name: "B", vram_gb: 48 },
          ],
        }),
      ),
    ).toBe(96);
    // Keine GPU-Grösse → RAM ist die ehrlichere Zahl als eine Konstante.
    expect(usableMemoryGb(mkProbe({ gpus: [], ram_gb: 64 }))).toBe(64);
    expect(usableMemoryGb(null)).toBeNull();
  });

  it("only rejects a recipe when both numbers are known", () => {
    expect(recipeFits(mkRecipe({ min_vram_gb: 90 }), 128)).toBe(true);
    expect(recipeFits(mkRecipe({ min_vram_gb: 200 }), 128)).toBe(false);
    // Unbekannter Bedarf oder unbekannte Kapazität → keine Warnung erfinden.
    expect(recipeFits(mkRecipe({ min_vram_gb: null }), 8)).toBe(true);
    expect(recipeFits(mkRecipe({ min_vram_gb: 200 }), null)).toBe(true);
  });

  it("needs a bootstrap only for a real gap", () => {
    expect(needsBootstrap(mkProbe())).toBe(false);
    expect(
      needsBootstrap(mkProbe({ docker: { ...mkProbe().docker, installed: false } })),
    ).toBe(true);
    // GPU da, aber Docker kommt nicht dran.
    expect(
      needsBootstrap(
        mkProbe({ docker: { ...mkProbe().docker, nvidia_runtime: false } }),
      ),
    ).toBe(true);
    // Ohne GPU ist die fehlende NVIDIA-Runtime kein Mangel.
    expect(
      needsBootstrap(
        mkProbe({ gpus: [], docker: { ...mkProbe().docker, nvidia_runtime: false } }),
      ),
    ).toBe(false);
    expect(needsBootstrap(null)).toBe(false);
  });

  it("derives a slug from the display name", () => {
    expect(slugify("DGX Spark")).toBe("dgx-spark");
    expect(slugify("  Box #2  ")).toBe("box-2");
  });
});

// ── 2. Step-Gating ───────────────────────────────────────────────────────────

describe("BoxWizard — step gating", () => {
  it("step 1 needs a reachable probe AND the identifying fields", () => {
    const filled = { slug: "b", displayName: "B", sshHost: "192.0.2.10" };
    expect(canProceed(mkState(filled))).toBe(false); // kein Probe
    expect(
      canProceed(mkState({ ...filled, probe: mkProbe({ reachable: false }) })),
    ).toBe(false);
    expect(canProceed(mkState({ ...filled, probe: mkProbe() }))).toBe(true);
    // Erreichbar, aber kein Name → weiterhin blockiert.
    expect(
      canProceed(mkState({ ...filled, displayName: "", probe: mkProbe() })),
    ).toBe(false);
  });

  it("step 2 blocks without docker — an engine needs one", () => {
    const base = { step: 1 };
    expect(canProceed(mkState({ ...base, probe: mkProbe() }))).toBe(true);
    expect(
      canProceed(
        mkState({
          ...base,
          probe: mkProbe({ docker: { ...mkProbe().docker, installed: false } }),
        }),
      ),
    ).toBe(false);
  });

  it("step 3 needs a recipe, a slug and a port", () => {
    const recipe = mkRecipe();
    expect(canProceed(mkState({ step: 2, recipe, runtimeSlug: "q", port: 8080 }))).toBe(true);
    expect(canProceed(mkState({ step: 2, recipe: null, runtimeSlug: "q", port: 8080 }))).toBe(false);
    expect(canProceed(mkState({ step: 2, recipe, runtimeSlug: "", port: 8080 }))).toBe(false);
    expect(canProceed(mkState({ step: 2, recipe, runtimeSlug: "q", port: 0 }))).toBe(false);
  });
});

// ── 3.–6. Komponente ─────────────────────────────────────────────────────────

describe("BoxWizard — component", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("keeps step 2 unreachable when the probe fails, and creates no host row", async () => {
    const probe = vi
      .spyOn(api.hosts, "probe")
      .mockResolvedValue(mkProbe({ reachable: false, reason: "SSH fehlgeschlagen: timeout" }));
    const create = vi.spyOn(api.hosts, "create");
    const user = userEvent.setup();
    renderWizard();

    await user.type(screen.getByLabelText("Display name"), "Tote Box");
    await user.type(screen.getByLabelText("SSH host"), "192.0.2.99");
    await user.click(screen.getByTestId("box-wizard-probe"));

    await waitFor(() =>
      expect(screen.getByTestId("box-wizard-unreachable")).toBeInTheDocument(),
    );
    expect(screen.getByText(/SSH fehlgeschlagen: timeout/)).toBeInTheDocument();
    expect(screen.getByTestId("box-wizard-next")).toBeDisabled();
    // Kein Probe-Erfolg → keine Host-Zeile. Sonst bleibt jeder Tippfehler liegen.
    expect(create).not.toHaveBeenCalled();
    expect(probe).toHaveBeenCalledWith({
      ssh_host: "192.0.2.99",
      ssh_user: null,
      ssh_key_path: null,
    });
  });

  it("creates the host row on the 1 → 2 transition", async () => {
    vi.spyOn(api.hosts, "probe").mockResolvedValue(mkProbe());
    const create = vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    const user = userEvent.setup();
    renderWizard();

    await walkToStep2(user);

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith({
      slug: "dgx-spark",
      display_name: "DGX Spark",
      kind: "ssh",
      // P2: kein Bestand (hosts.list nicht gemockt → keine Liste) → erste Box = Head
      role: "head",
      ssh_host: "192.0.2.10",
      ssh_user: null,
      ssh_key_path: null,
    });
  });

  // ── P2: Geräterolle ────────────────────────────────────────────────────────

  it("P2: suggests 'worker' when a box already exists, and the suggestion is visible", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.hosts, "probe").mockResolvedValue(mkProbe());
    const create = vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    const user = userEvent.setup();
    renderWizard();

    // Vorbelegung kommt asynchron aus der Host-Liste.
    await waitFor(() =>
      expect(screen.getByTestId("box-wizard-role-worker")).toHaveAttribute("aria-checked", "true"),
    );
    expect(screen.getByTestId("box-wizard-role-head")).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("box-wizard-role-suggested")).toBeInTheDocument();
    expect(screen.getByText(/Single-box recipes ignore the role/)).toBeInTheDocument();

    await walkToStep2(user);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create.mock.calls[0][0]).toMatchObject({ role: "worker" });
  });

  it("P2: the suggestion is only a suggestion — one click flips it to 'head' and that is what gets sent", async () => {
    vi.spyOn(api.hosts, "list").mockResolvedValue([mkHost()]);
    vi.spyOn(api.hosts, "probe").mockResolvedValue(mkProbe());
    const create = vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    const user = userEvent.setup();
    renderWizard();

    await waitFor(() =>
      expect(screen.getByTestId("box-wizard-role-worker")).toHaveAttribute("aria-checked", "true"),
    );
    await user.click(screen.getByTestId("box-wizard-role-head"));
    expect(screen.getByTestId("box-wizard-role-head")).toHaveAttribute("aria-checked", "true");
    // Nach dem Klick ist es kein Vorschlag mehr — der Satz verschwindet …
    expect(screen.queryByTestId("box-wizard-role-suggested")).toBeNull();

    await walkToStep2(user);
    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    // … und die Liste darf die Handwahl nicht mehr überschreiben.
    expect(create.mock.calls[0][0]).toMatchObject({ role: "head" });
  });

  it("shows green lights for a prepared box and offers no bootstrap", async () => {
    vi.spyOn(api.hosts, "probe").mockResolvedValue(mkProbe());
    vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    const user = userEvent.setup();
    renderWizard();

    await walkToStep2(user);

    await waitFor(() => expect(screen.getByTestId("light-docker")).toBeInTheDocument());
    expect(screen.getByTestId("light-docker")).toHaveAttribute("data-state", "ok");
    expect(screen.getByTestId("light-nvidia")).toHaveAttribute("data-state", "ok");
    expect(screen.getByTestId("light-gpu")).toHaveAttribute("data-state", "ok");
    expect(screen.getByTestId("light-disk")).toHaveAttribute("data-state", "ok");
    expect(screen.queryByTestId("box-wizard-bootstrap")).not.toBeInTheDocument();
  });

  it("flags the gaps and offers the bootstrap on a bare box", async () => {
    vi.spyOn(api.hosts, "probe").mockResolvedValue(
      mkProbe({
        gpus: [],
        nvidia_smi: false,
        docker: {
          installed: false,
          version: null,
          nvidia_runtime: false,
          runtimes: null,
          toolkit_installed: false,
        },
        disk_free_gb: 12,
      }),
    );
    vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    const bootstrap = vi
      .spyOn(api.hosts, "bootstrap")
      .mockResolvedValue({ status: "started", host_id: "host-1" });
    const user = userEvent.setup();
    renderWizard();

    await walkToStep2(user);

    await waitFor(() => expect(screen.getByTestId("light-docker")).toBeInTheDocument());
    expect(screen.getByTestId("light-docker")).toHaveAttribute("data-state", "bad");
    // Ohne GPU ist die NVIDIA-Runtime kein Fehler, nur ein Hinweis.
    expect(screen.getByTestId("light-nvidia")).toHaveAttribute("data-state", "warn");
    expect(screen.getByTestId("light-gpu")).toHaveAttribute("data-state", "warn");
    expect(screen.getByTestId("light-disk")).toHaveAttribute("data-state", "warn");
    // Ohne Docker geht es nicht weiter.
    expect(screen.getByTestId("box-wizard-next")).toBeDisabled();

    await user.click(screen.getByTestId("box-wizard-bootstrap"));
    await waitFor(() => expect(bootstrap).toHaveBeenCalledWith("host-1"));
  });

  it("filters recipes by the box's arch and warns against its real memory", async () => {
    vi.spyOn(api.hosts, "probe").mockResolvedValue(
      // 24-GB-Karte: das 90-GB-Rezept passt hier NICHT, obwohl es unter der
      // alten 121-GB-Konstante durchgegangen wäre.
      mkProbe({ arch: "x86_64", gpus: [{ name: "RTX 4090", vram_gb: 24 }] }),
    );
    vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    const list = vi.spyOn(api.localRegistry, "list").mockResolvedValue(
      mkRegistry([
        mkRecipe(),
        mkRecipe({ slug: "big-one", display_name: "Big One", min_vram_gb: 90 }),
        mkRecipe({ slug: "spark-only", display_name: "Spark Only", engine: "sparkrun" }),
      ]),
    );
    const user = userEvent.setup();
    renderWizard();

    await walkToStep2(user);
    await waitFor(() => expect(screen.getByTestId("light-docker")).toBeInTheDocument());
    await user.click(screen.getByTestId("box-wizard-next"));

    await waitFor(() =>
      expect(list).toHaveBeenCalledWith({ enabled: true, arch: "x86_64" }),
    );
    const cards = await screen.findAllByTestId("box-wizard-recipe");
    // sparkrun fliegt raus — dafür gibt es switch-recipe, nicht diesen Weg.
    expect(cards).toHaveLength(2);
    expect(screen.queryByText("Spark Only")).not.toBeInTheDocument();

    const bySlug = Object.fromEntries(
      cards.map((c) => [c.getAttribute("data-slug"), c]),
    );
    expect(bySlug["qwen3-8b-gguf-q4"].getAttribute("data-fits")).toBe("yes");
    expect(bySlug["big-one"].getAttribute("data-fits")).toBe("no");
    expect(screen.getAllByTestId("box-wizard-fit-warning")).toHaveLength(1);
    expect(screen.getByText("Needs 90 GB, box has 24 GB")).toBeInTheDocument();
  });

  it("creates and starts the runtime with the rendered launch command", async () => {
    vi.spyOn(api.hosts, "probe").mockResolvedValue(mkProbe());
    vi.spyOn(api.hosts, "create").mockResolvedValue(mkHost());
    vi.spyOn(api.localRegistry, "list").mockResolvedValue(mkRegistry([mkRecipe()]));
    const launchCommand = vi.spyOn(api.hosts, "launchCommand").mockResolvedValue({
      launch_command:
        "docker run -d --rm --name mc-qwen3-8b-gguf --label mc.runtime.slug=qwen3-8b-gguf -p 8080:8080 ghcr.io/ggml-org/llama.cpp:server-cuda -hf Qwen/Qwen3-8B-GGUF --host 0.0.0.0 --port 8080 --jinja",
      stop_command: null,
    });
    const create = vi
      .spyOn(api.runtimes, "create")
      .mockResolvedValue({ id: "rt-9" } as Runtime);
    const start = vi
      .spyOn(api.runtimes, "start")
      .mockResolvedValue({ ok: true, message: "" } as never);
    vi.spyOn(api.runtimes, "health").mockResolvedValue({ state: "warming" } as never);

    const user = userEvent.setup();
    renderWizard();

    await walkToStep2(user);
    await waitFor(() => expect(screen.getByTestId("light-docker")).toBeInTheDocument());
    await user.click(screen.getByTestId("box-wizard-next"));

    const card = await screen.findByTestId("box-wizard-recipe");
    await user.click(card);
    await user.click(screen.getByTestId("box-wizard-next"));

    // Das Kommando kommt vom Backend, nicht aus dem Frontend.
    await waitFor(() =>
      expect(launchCommand).toHaveBeenCalledWith({
        engine: "llamacpp_docker",
        model_identifier: "Qwen/Qwen3-8B-GGUF",
        slug: "qwen3-8b-gguf",
        port: 8080,
        launch_template: null,
      }),
    );
    await waitFor(() =>
      expect(screen.getByTestId("box-wizard-launch-command")).toHaveTextContent(
        "mc.runtime.slug=qwen3-8b-gguf",
      ),
    );

    await user.click(screen.getByTestId("box-wizard-create"));

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith({
      slug: "qwen3-8b-gguf",
      display_name: "Qwen3 8B GGUF",
      runtime_type: "llamacpp_docker",
      endpoint: "http://192.0.2.10:8080/v1",
      healthcheck_path: null,
      model_identifier: "Qwen/Qwen3-8B-GGUF",
      container_name: "mc-qwen3-8b-gguf",
      launch_command: expect.stringContaining("--label mc.runtime.slug=qwen3-8b-gguf"),
      host_id: "host-1",
    });
    // Bestehender Start-Endpoint mit der frisch angelegten Runtime.
    await waitFor(() => expect(start).toHaveBeenCalledWith("rt-9"));
    expect(await screen.findByTestId("box-wizard-result-state")).toHaveTextContent(
      "Status: warming",
    );
  });
});
