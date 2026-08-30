import { describe, it, expect } from "vitest";
import { groupRuntimesByProvider, isRuntimeBlockedByLocality } from "@/lib/groupRuntimes";
import type { Runtime } from "@/lib/types";

const rt = (
  slug: string,
  provider_label: string | null,
  extra: Partial<Runtime> = {},
): Runtime => ({ id: slug, slug, display_name: slug, provider_label, ...extra } as unknown as Runtime);

describe("groupRuntimesByProvider", () => {
  it("bundles consecutive rows of the same vendor", () => {
    const groups = groupRuntimesByProvider([
      rt("opus", "Anthropic Pro/Max"),
      rt("opus-5", "Anthropic Pro/Max"),
      rt("glm-51", "Ollama Cloud"),
      rt("glm-52", "Ollama Cloud"),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["Anthropic Pro/Max", "Ollama Cloud"]);
    expect(groups[0].runtimes.map((r) => r.slug)).toEqual(["opus", "opus-5"]);
  });

  it("keeps unlabelled rows in their own trailing group", () => {
    const groups = groupRuntimesByProvider([
      rt("opus", "Anthropic Pro/Max"),
      rt("qwen-general", null),
      rt("lmstudio", null),
    ]);
    expect(groups.map((g) => g.label)).toEqual(["Anthropic Pro/Max", null]);
    expect(groups[1].runtimes).toHaveLength(2);
  });

  it("does not reorder — the server decides the order", () => {
    // Same vendor split by another one: the split is preserved rather than
    // silently merged, so the server stays the single source of ordering.
    const groups = groupRuntimesByProvider([
      rt("opus", "Anthropic Pro/Max"),
      rt("glm", "Ollama Cloud"),
      rt("sonnet", "Anthropic Pro/Max"),
    ]);
    expect(groups.map((g) => g.label)).toEqual([
      "Anthropic Pro/Max",
      "Ollama Cloud",
      "Anthropic Pro/Max",
    ]);
  });

  it("returns nothing for an empty list", () => {
    expect(groupRuntimesByProvider([])).toEqual([]);
  });
});

describe("isRuntimeBlockedByLocality", () => {
  it("blocks a cloud runtime for a host-inplace agent", () => {
    const cloudRt = rt("opus", "Anthropic Pro/Max", { locality: "cloud" });
    expect(isRuntimeBlockedByLocality(cloudRt, true)).toBe(true);
  });

  it("never blocks a local runtime, host-inplace or not", () => {
    const localRt = rt("vllm-box", null, { locality: "local" });
    expect(isRuntimeBlockedByLocality(localRt, true)).toBe(false);
    expect(isRuntimeBlockedByLocality(localRt, false)).toBe(false);
  });

  it("never blocks a cloud runtime for a NON-host-inplace agent (cli-bridge reaches the network fine)", () => {
    const cloudRt = rt("opus", "Anthropic Pro/Max", { locality: "cloud" });
    expect(isRuntimeBlockedByLocality(cloudRt, false)).toBe(false);
  });

  it("treats a missing locality field as local (older cached response must not grey out everything)", () => {
    const noLocalityRt = rt("legacy-row", null, { locality: undefined });
    expect(isRuntimeBlockedByLocality(noLocalityRt, true)).toBe(false);
  });
});
