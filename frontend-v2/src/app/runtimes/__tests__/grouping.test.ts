import { describe, it, expect } from "vitest";
import { groupRuntimes, CLOUD_TYPES, panelCapabilities, pickServing } from "../grouping";
import type { Runtime, Host, RuntimeLiveStatus } from "@/lib/types";

function makeRuntime(over: Partial<Runtime>): Runtime {
  return {
    id: over.slug ?? "rt", slug: "rt", display_name: "RT",
    runtime_type: "vllm_docker", provider: "vllm",
    endpoint: "http://192.0.2.10:8001/v1", healthcheck_path: "/health",
    container_name: null, role_tags: [], supports_tools: true,
    supports_reasoning: false, supports_streaming: true,
    preferred_context_len: 8192, max_context_len: 32768,
    gpu_profile: "default", memory_notes: "", startup_notes: "",
    ui_order: 0, enabled: true, state: "ready",
    ...over,
  };
}

function makeHost(over: Partial<Host>): Host {
  return {
    id: over.slug ?? "h", slug: "h", display_name: "H", kind: "ssh",
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

describe("groupRuntimes", () => {
  const spark = makeHost({ slug: "spark", display_name: "Spark", ui_order: 1 });
  const local = makeHost({ slug: "local", display_name: "Mac Mini", ui_order: 2 });

  it("puts host-bound runtimes into their host group, ordered by ui_order", () => {
    const a = makeRuntime({ slug: "a", host: { id: "local", slug: "local", display_name: "Mac Mini" } });
    const b = makeRuntime({ slug: "b", host: { id: "spark", slug: "spark", display_name: "Spark" } });
    const g = groupRuntimes([a, b], [local, spark]);
    expect(g.hosts.map((h) => h.host.slug)).toEqual(["spark", "local"]);
    expect(g.hosts[0].runtimes.map((r) => r.slug)).toEqual(["b"]);
  });

  it("routes hostless cloud kinds to cloud, others to unassigned", () => {
    const anthropic = makeRuntime({ slug: "anthropic-claude-opus", runtime_type: "cloud", host: null });
    const grok = makeRuntime({ slug: "grok-cloud", runtime_type: "grok", host: null });
    const hermes = makeRuntime({ slug: "hermes-vllm", runtime_type: "hermes", host: null });
    const g = groupRuntimes([anthropic, grok, hermes], []);
    expect(g.cloud.map((r) => r.slug)).toEqual(["anthropic-claude-opus", "grok-cloud"]);
    expect(g.unassigned.map((r) => r.slug)).toEqual(["hermes-vllm"]);
  });

  it("routes a hostless openai_compatible runtime to cloud, not unassigned (spec §3)", () => {
    const openaiCompat = makeRuntime({ slug: "openai-compat-api", runtime_type: "openai_compatible", host: null });
    const g = groupRuntimes([openaiCompat], []);
    expect(g.cloud.map((r) => r.slug)).toEqual(["openai-compat-api"]);
    expect(g.unassigned).toEqual([]);
  });

  it("keeps a host group even when the host has no runtimes", () => {
    const g = groupRuntimes([], [spark]);
    expect(g.hosts).toHaveLength(1);
    expect(g.hosts[0].runtimes).toEqual([]);
  });

  it("sorts within a group: active states before stopped, then ui_order", () => {
    const stopped = makeRuntime({ slug: "s", state: "stopped", ui_order: 0, host: { id: "spark", slug: "spark", display_name: "Spark" } });
    const ready = makeRuntime({ slug: "r", state: "ready", ui_order: 5, host: { id: "spark", slug: "spark", display_name: "Spark" } });
    const g = groupRuntimes([stopped, ready], [spark]);
    expect(g.hosts[0].runtimes.map((r) => r.slug)).toEqual(["r", "s"]);
  });

  it("buckets host-bound runtimes into unassigned when their host isn't in hosts[] (loading/error/orphaned host_id)", () => {
    const orphan = makeRuntime({ slug: "orphan", host: { id: "missing-host", slug: "missing-host", display_name: "Ghost" } });
    const g = groupRuntimes([orphan], []);
    expect(g.hosts).toEqual([]);
    expect(g.unassigned.map((r) => r.slug)).toEqual(["orphan"]);
    expect(g.cloud).toEqual([]);
  });

  // Verbund-UI Phase 1b (30.08.2026)
  it("marks a member_hosts entry's host as workerOf its verbund runtime", () => {
    const beta = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const verbund = makeRuntime({
      slug: "glm-verbund", display_name: "GLM Verbund",
      host: { id: "spark", slug: "spark", display_name: "Spark" },
      member_hosts: [
        { host_id: "beta", slug: "beta", display_name: "Beta", role: "worker", node_rank: 1 },
      ],
    });
    const g = groupRuntimes([verbund], [spark, beta]);

    const betaGroup = g.hosts.find((hg) => hg.host.slug === "beta");
    expect(betaGroup?.workerOf).toEqual({
      runtimeId: "glm-verbund",
      runtimeDisplayName: "GLM Verbund",
      headSlug: "spark",
      role: "worker",
      nodeRank: 1,
    });

    // The head itself is never marked workerOf its own runtime.
    const sparkGroup = g.hosts.find((hg) => hg.host.slug === "spark");
    expect(sparkGroup?.workerOf).toBeUndefined();
  });

  it("a host with no member_hosts entry anywhere has no workerOf", () => {
    const g = groupRuntimes([], [spark]);
    expect(g.hosts[0].workerOf).toBeUndefined();
  });

  it("workerOf has no headSlug when the runtime's own host binding didn't resolve to a registry host", () => {
    const beta = makeHost({ slug: "beta", display_name: "Beta", kind: "agent" });
    const verbund = makeRuntime({
      slug: "glm-verbund", display_name: "GLM Verbund",
      host: null, // legacy string/settings fallback — _host_ref returns null server-side
      member_hosts: [
        { host_id: "beta", slug: "beta", display_name: "Beta", role: "worker", node_rank: 1 },
      ],
    });
    const g = groupRuntimes([verbund], [beta]);
    expect(g.hosts.find((hg) => hg.host.slug === "beta")?.workerOf?.headSlug).toBeNull();
  });
});

describe("CLOUD_TYPES", () => {
  it("contains exactly the hosted-API kinds", () => {
    expect([...CLOUD_TYPES].sort()).toEqual(["cloud", "grok", "kimi", "openai_compatible"]);
  });
});

describe("panelCapabilities", () => {
  it("vllm_docker on a box: lifecycle+probe+recipe, no editor/context/wake", () => {
    const host = { id: "box-a", slug: "box-a", display_name: "Box A" };
    expect(panelCapabilities(makeRuntime({ runtime_type: "vllm_docker", host }))).toEqual({
      lifecycle: true, wake: false, probe: true,
      modelEditor: false, recipeSwitcher: true, contextSettings: false, autostart: false,
    });
  });

  // Rezept-Umschalter (Vertrag 02.09.2026): das Gate ist „hat eine Box",
  // nicht „ist ein bestimmter runtime_type" — sonst wäre es Hardcodierung.
  it("recipeSwitcher follows the host binding, not the runtime_type", () => {
    const host = { id: "box-a", slug: "box-a", display_name: "Box A" };
    expect(panelCapabilities(makeRuntime({ runtime_type: "lmstudio", host })).recipeSwitcher).toBe(true);
    expect(panelCapabilities(makeRuntime({ runtime_type: "ssh_process", host })).recipeSwitcher).toBe(true);
    expect(panelCapabilities(makeRuntime({ runtime_type: "vllm_docker", host: null })).recipeSwitcher).toBe(false);
  });

  it("lmstudio: lifecycle+probe+contextSettings", () => {
    const c = panelCapabilities(makeRuntime({ runtime_type: "lmstudio" }));
    expect(c.contextSettings).toBe(true);
    expect(c.lifecycle).toBe(true);
    expect(c.recipeSwitcher).toBe(false);
  });

  it("cloud: only the model editor", () => {
    expect(panelCapabilities(makeRuntime({ runtime_type: "cloud" }))).toEqual({
      lifecycle: false, wake: false, probe: false,
      modelEditor: true, recipeSwitcher: false, contextSettings: false, autostart: false,
    });
  });

  it("power-managed runtime gets wake; autostart follows autostart_supported", () => {
    const c = panelCapabilities(makeRuntime({ runtime_type: "unsloth_porsche", power_managed: true, autostart_supported: true }));
    expect(c.wake).toBe(true);
    expect(c.autostart).toBe(true);
  });

  it("omp and llamacpp_docker have no lifecycle support (backend returns 400 on start/stop/restart)", () => {
    expect(panelCapabilities(makeRuntime({ runtime_type: "omp" })).lifecycle).toBe(false);
    expect(panelCapabilities(makeRuntime({ runtime_type: "llamacpp_docker" })).lifecycle).toBe(false);
  });
});

describe("pickServing", () => {
  const live = (over: Partial<RuntimeLiveStatus>): RuntimeLiveStatus => ({
    reachable: false, served_model: null, latency_ms: null,
    last_probe_at: "", consecutive_failures: 0, drift: false, ...over,
  });
  it("prefers a switching runtime over a ready one", () => {
    const a = makeRuntime({ slug: "a", state: "ready" });
    const b = makeRuntime({ slug: "b", state: "stopped" });
    const res = pickServing({ host: makeHost({}), runtimes: [a, b] },
      { a: live({ reachable: true }), b: live({ status: "switching", phase: "loading" }) });
    expect(res?.slug).toBe("b");
  });
  it("falls back to active state, then live reachability", () => {
    const warm = makeRuntime({ slug: "w", state: "warming" });
    const cold = makeRuntime({ slug: "c", state: "stopped" });
    expect(pickServing({ host: makeHost({}), runtimes: [cold, warm] })?.slug).toBe("w");
    expect(pickServing({ host: makeHost({}), runtimes: [cold] },
      { c: live({ reachable: true }) })?.slug).toBe("c");
  });
  it("returns null for an idle host", () => {
    expect(pickServing({ host: makeHost({}), runtimes: [makeRuntime({ state: "stopped" })] })).toBeNull();
  });
});
