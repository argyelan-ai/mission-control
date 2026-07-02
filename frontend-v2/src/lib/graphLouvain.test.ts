import { describe, it, expect } from "vitest";
import { computeCommunities } from "./graphLouvain";

describe("computeCommunities", () => {
  it("assigns community ids to all nodes", () => {
    const nodes = [{ id: "a" }, { id: "b" }, { id: "c" }, { id: "d" }];
    const edges = [
      { source: "a", target: "b" },
      { source: "c", target: "d" },
    ];
    const communities = computeCommunities(nodes, edges);
    expect(Object.keys(communities)).toHaveLength(4);
    expect(communities["a"]).toBe(communities["b"]);
    expect(communities["c"]).toBe(communities["d"]);
    expect(communities["a"]).not.toBe(communities["c"]);
  });

  it("handles isolated nodes", () => {
    const nodes = [{ id: "x" }];
    const edges: { source: string; target: string }[] = [];
    const communities = computeCommunities(nodes, edges);
    expect(communities["x"]).toBeDefined();
  });

  it("deduplicates parallel edges", () => {
    const nodes = [{ id: "a" }, { id: "b" }];
    const edges = [
      { source: "a", target: "b", weight: 1 },
      { source: "a", target: "b", weight: 2 }, // dup — second is dropped
    ];
    const communities = computeCommunities(nodes, edges);
    expect(communities["a"]).toBe(communities["b"]);
  });

  it("ignores edges referencing unknown nodes", () => {
    const nodes = [{ id: "a" }];
    const edges = [{ source: "a", target: "ghost" }];
    expect(() => computeCommunities(nodes, edges)).not.toThrow();
  });
});
