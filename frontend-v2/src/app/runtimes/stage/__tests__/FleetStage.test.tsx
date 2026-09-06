/**
 * buildStages — die Modell-Ebenen-Gruppierung der Bühne v2 (Spec §2/§3):
 * ein laufendes Modell = eine Karte, mit Head + Worker als Mitgliedern.
 * Reine Funktion (keine Query/Hook-Abhängigkeit) — Fixtures aus
 * grouping.test.ts (gleiches Muster wie SlotStage.test.tsx).
 */
import { describe, it, expect } from "vitest";
import { buildStages } from "../FleetStage";
import { groupRuntimes } from "../../grouping";
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
    ssh_host: null, ssh_user: null, ssh_key_path: null, ssh_credential_id: null, role: null, fabric_ip: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true,
    ui_order: 0, created_at: "", updated_at: "",
    ...over,
  };
}

const headHost = makeHost({ slug: "spark", display_name: "DGX Spark", ui_order: 1, role: "head" });
const workerHost = makeHost({ slug: "gx10", display_name: "GX10", ui_order: 2, role: "worker" });

describe("buildStages", () => {
  it("groups a duo runtime (member_hosts) into ONE stage with two host ids", () => {
    const duo = makeRuntime({
      slug: "qwen-duo",
      host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
      member_hosts: [{ host_id: "gx10", slug: "gx10", display_name: "GX10", role: "worker", node_rank: 1 }],
    });
    const groups = groupRuntimes([duo], [headHost, workerHost]);
    const { stages, freeHostIds } = buildStages(groups.hosts);

    expect(stages).toHaveLength(1);
    expect(stages[0].runtime.slug).toBe("qwen-duo");
    expect(stages[0].hostIds.sort()).toEqual(["gx10", "spark"].sort());
    // Both boxes are claimed by the duo stage — neither is free.
    expect(freeHostIds.size).toBe(0);
  });

  it("solo: one stage (head only) + the OTHER box stays free", () => {
    const solo = makeRuntime({ slug: "qwen-solo", host: { id: "spark", slug: "spark", display_name: "DGX Spark" } });
    const groups = groupRuntimes([solo], [headHost, workerHost]);
    const { stages, freeHostIds } = buildStages(groups.hosts);

    expect(stages).toHaveLength(1);
    expect(stages[0].hostIds).toEqual(["spark"]);
    expect(freeHostIds.has("gx10")).toBe(true);
    expect(freeHostIds.has("spark")).toBe(false);
  });

  it("nothing running: no stages, every enabled box is free", () => {
    const groups = groupRuntimes([], [headHost, workerHost]);
    const { stages, freeHostIds } = buildStages(groups.hosts);

    expect(stages).toHaveLength(0);
    expect(freeHostIds.size).toBe(2);
  });

  it("a switching runtime still owns its box (pickServing's switching tier)", () => {
    const rt = makeRuntime({ slug: "switching-rt", host: { id: "spark", slug: "spark", display_name: "DGX Spark" }, state: "unknown" });
    const groups = groupRuntimes([rt], [headHost, workerHost]);
    const live: Record<string, RuntimeLiveStatus> = {
      "switching-rt": { reachable: false, served_model: null, latency_ms: null, last_probe_at: "", consecutive_failures: 0, drift: false, status: "switching", phase: "launching" },
    };
    const { stages, freeHostIds } = buildStages(groups.hosts, live);
    expect(stages).toHaveLength(1);
    expect(stages[0].runtime.slug).toBe("switching-rt");
    expect(freeHostIds.has("gx10")).toBe(true);
  });

  // Review #438 Fund 3 (06.09.2026): a worker box that is BOTH a member of
  // some other host's active duo runtime AND carries its own bound, serving
  // solo runtime used to tear the duo card into two — a left-to-right scan
  // hit the worker's OWN group before the head's group (its ui_order is
  // lower here, reproducing the exact order from the review's repro) and
  // spun up a second, standalone stage for the worker's solo runtime.
  it("a worker with its OWN serving runtime AND duo membership: the duo wins, no second card", () => {
    const workerFirst = makeHost({ slug: "gx10", display_name: "GX10", ui_order: 1, role: "worker" });
    const headSecond = makeHost({ slug: "spark", display_name: "DGX Spark", ui_order: 2, role: "head" });

    const duo = makeRuntime({
      slug: "qwen-duo",
      host: { id: "spark", slug: "spark", display_name: "DGX Spark" },
      member_hosts: [{ host_id: "gx10", slug: "gx10", display_name: "GX10", role: "worker", node_rank: 1 }],
    });
    // gx10 ALSO carries its own directly-bound, currently-serving runtime —
    // the exact combination the review reproduced.
    const gx10Own = makeRuntime({
      slug: "gx10-own-solo",
      host: { id: "gx10", slug: "gx10", display_name: "GX10" },
    });

    const groups = groupRuntimes([gx10Own, duo], [workerFirst, headSecond]);
    // Sanity: groupRuntimes really does put the worker's group before the
    // head's group here (ui_order 1 < 2) — otherwise this test wouldn't
    // exercise the bug at all.
    expect(groups.hosts.map((g) => g.host.slug)).toEqual(["gx10", "spark"]);

    const { stages, freeHostIds } = buildStages(groups.hosts);

    expect(stages).toHaveLength(1);
    expect(stages[0].runtime.slug).toBe("qwen-duo");
    expect(stages[0].hostIds.sort()).toEqual(["gx10", "spark"]);
    expect(freeHostIds.size).toBe(0);
  });
});
