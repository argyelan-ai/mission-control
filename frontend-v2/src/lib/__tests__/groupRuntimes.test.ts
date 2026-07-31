import { describe, it, expect } from "vitest";
import { groupRuntimesByProvider } from "@/lib/groupRuntimes";
import type { Runtime } from "@/lib/types";

const rt = (slug: string, provider_label: string | null): Runtime =>
  ({ id: slug, slug, display_name: slug, provider_label } as unknown as Runtime);

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
