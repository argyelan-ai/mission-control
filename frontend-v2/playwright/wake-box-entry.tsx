/**
 * Mount fixture for `wake-box-shots.mjs` (round-5 re-check L1).
 *
 * Mounts the REAL `FleetStage` with two boxes: a free always-on box and a
 * power-managed box that sleeps — its host carries the flag, none of its
 * runtime rows does (the case the re-check found). Every API call the cards
 * make is stubbed here: nothing leaves the page, no box is woken.
 * Round-7 re-check L5: also mounts the REAL `RuntimeDetailPanel` of a
 * sleeping power-managed runtime — the second wake path — so its "Wake"
 * answers can be seen (helper down / sent), same stubs.
 * Not a test: real layout only exists in a real browser.
 */
import React from "react";
import { createRoot } from "react-dom/client";
import { NextIntlClientProvider } from "next-intl";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Host, HostRecipe, Runtime } from "@/lib/types";
import { FleetStage } from "@/app/runtimes/stage/FleetStage";
import { RuntimeDetailPanel } from "@/app/runtimes/RuntimeDetailPanel";
import en from "../messages/en.json";
import de from "../messages/de.json";

declare global {
  interface Window {
    __mountWakeBox?: (opts: { locale: "en" | "de"; helperDown?: boolean }) => void;
    __mountRuntimeDetail?: (opts: { locale: "en" | "de"; helperDown?: boolean }) => void;
    __wakeHelperDown?: boolean;
  }
}

function host(over: Partial<Host>): Host {
  return {
    id: "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  } as Host;
}

function runtime(over: Partial<Runtime>): Runtime {
  return {
    id: "rt", slug: "rt", display_name: "RT",
    runtime_type: "lmstudio", provider: "lmstudio",
    endpoint: "http://192.0.2.20:1234/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "stopped",
    ...over,
  };
}

function recipe(over: Partial<HostRecipe>): HostRecipe {
  return {
    slug: "r", display_name: "R", engine: "vllm", topology: { nodes: 1 }, port: 8000,
    instance_runtime_id: null, running: false, startable: true, fit: "solo", reason: null,
    busy_hosts: [], candidate_workers: [],
    ...over,
  };
}

const gpuBox = host({ id: "gpu", slug: "gpu", display_name: "GPU Box", ssh_host: "192.0.2.10", ui_order: 0 });
const workstation = host({
  id: "ws", slug: "ws", display_name: "Workstation", kind: "flask_wol", power_managed: true,
  control_url: "http://192.0.2.20:5555", wol_mac_address: "00:00:5E:00:53:01", ui_order: 1,
});
const gpuRows = [runtime({ id: "gpu-slot", slug: "gpu-slot", is_slot: true, runtime_type: "vllm_docker", endpoint: "http://192.0.2.10:8000/v1", host: { id: "gpu", slug: "gpu", display_name: "GPU Box" } })];
const wsRows = [
  runtime({ id: "ws-lm", slug: "ws-lm", power_managed: false, host: { id: "ws", slug: "ws", display_name: "Workstation" } }),
  runtime({ id: "ws-slot", slug: "ws-slot", is_slot: true, power_managed: false, host: { id: "ws", slug: "ws", display_name: "Workstation" } }),
];

// ── stubs: nothing leaves the page ───────────────────────────────────────────
const a = api as unknown as Record<string, Record<string, unknown>>;
a.hosts.metrics = async (id: string) =>
  id === "ws"
    ? { kind: "flask_wol", reachable: false, awake: false, status: "asleep" }
    : { reachable: true, gpu_util_pct: 3, vram_used_mb: 9 * 1024, vram_total_mb: 120 * 1024, gpu_temp_c: 41, ram_used_mb: 14000, ram_total_mb: 119000 };
a.hosts.recipes = async () => [
  recipe({ slug: "solo", display_name: "Solo model", capacity: { ok: true, warnings: [] } as never }),
  recipe({ slug: "duo", display_name: "Two-box model", topology: { nodes: 2 }, fit: "duo", capacity: { ok: true, warnings: [] } as never }),
];
// `helperDown`: what the backend answers when the Mac's wake helper sends no
// heartbeat (503) — PR #976 review MUST 1, round-7 L5. Both wake paths.
const fakeWake = () =>
  new Promise((resolve, reject) =>
    setTimeout(
      () =>
        window.__wakeHelperDown
          ? reject(new Error('API 503: {"detail":"Weck-Helfer auf dem Mac läuft nicht (kein Lebenszeichen). Workstation wurde nicht geweckt."}'))
          : resolve({ ok: true, message: "Wake-on-LAN ausgelöst für Workstation. Box braucht ~1-2 Min bis erreichbar." }),
      400,
    ),
  );
a.hosts.wake = fakeWake;
a.runtimes.wake = fakeWake;
a.hosts.metricsHistory = async () => ({ points: [], window: 3600, sample_seconds: 5 });
a.hosts.pulse = async () => ({ points: [], now_tps: null, idle_seconds: null, available: false });
a.hosts.autostart = async (id: string) => ({
  host_id: id, enabled: false, recipe_slug: null, recipe_display_name: null,
  role: null, via_head: null, last_attempt_at: null, last_result: null,
});
a.nodes.devices = async () => [];
(a.runtimes.db as Record<string, unknown>).agents = async (slug: string) => ({ runtime_slug: slug, count: slug === "ws-slot" ? 1 : 2, agents: [] });
a.localRegistry.list = async () => ({ recipes: [], total: 0, sources: [] });

function App({ locale }: { locale: "en" | "de"; helperDown?: boolean }) {
  const [qc] = React.useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <NextIntlClientProvider locale={locale} messages={(locale === "de" ? de : en) as never}>
      <QueryClientProvider client={qc}>
        <div className="px-4 py-6 max-w-4xl mx-auto">
          <FleetStage
            stageGroups={[{ host: gpuBox, runtimes: gpuRows }]}
            sleepingGroups={[{ host: workstation, runtimes: wsRows }]}
            onOpen={() => {}}
          />
        </div>
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
}

// The power-managed runtime of the sleeping box, as the runtime detail sees it.
const sleepingRuntime = runtime({
  id: "ws-unsloth", slug: "ws-unsloth", display_name: "Unsloth (Workstation)",
  runtime_type: "flask_wol", provider: "unsloth", power_managed: true,
  container_status: "asleep", control_url: "http://192.0.2.20:5555",
  wol_mac_address: "00:00:5E:00:53:01", endpoint: "http://192.0.2.20:8000/v1",
});

function Detail({ locale }: { locale: "en" | "de"; helperDown?: boolean }) {
  const [qc] = React.useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  return (
    <NextIntlClientProvider locale={locale} messages={(locale === "de" ? de : en) as never}>
      <QueryClientProvider client={qc}>
        <RuntimeDetailPanel open runtime={sleepingRuntime} onClose={() => {}} />
      </QueryClientProvider>
    </NextIntlClientProvider>
  );
}

export function mount() {
  const root = createRoot(document.getElementById("root")!);
  window.__mountWakeBox = (opts) => root.render(<App key={JSON.stringify(opts)} {...opts} />);
  window.__mountRuntimeDetail = (opts) => root.render(<Detail key={"d" + JSON.stringify(opts)} {...opts} />);
}
