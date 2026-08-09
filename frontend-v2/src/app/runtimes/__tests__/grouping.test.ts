import { describe, it, expect } from "vitest";
import { groupRuntimes, summarizeStates, CLOUD_TYPES } from "../grouping";
import type { Runtime, Host } from "@/lib/types";

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
    ssh_host: null, ssh_user: null, ssh_key_path: null, control_url: null,
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
});

describe("summarizeStates", () => {
  it("counts ready/starting/warming as active, failed separately, rest stopped", () => {
    const rts = [
      makeRuntime({ slug: "1", state: "ready" }),
      makeRuntime({ slug: "2", state: "warming" }),
      makeRuntime({ slug: "3", state: "failed" }),
      makeRuntime({ slug: "4", state: "stopped" }),
      makeRuntime({ slug: "5", state: "unknown" }),
    ];
    expect(summarizeStates(rts)).toEqual({ active: 2, stopped: 2, failed: 1 });
  });
});

describe("CLOUD_TYPES", () => {
  it("contains exactly the hosted-API kinds", () => {
    expect([...CLOUD_TYPES].sort()).toEqual(["cloud", "grok", "kimi"]);
  });
});
