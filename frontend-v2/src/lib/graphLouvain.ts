import Graph from "graphology";
import louvain from "graphology-communities-louvain";

export interface CommunityNode { id: string }
export interface CommunityEdge { source: string; target: string; weight?: number }

/**
 * Run Louvain community detection on a node/edge list.
 * Returns a map of nodeId → community id (0..N).
 * Edges are treated as undirected; weights default to 1.
 */
export function computeCommunities(
  nodes: CommunityNode[],
  edges: CommunityEdge[],
): Record<string, number> {
  const g = new Graph({ type: "undirected", multi: false });
  for (const n of nodes) g.addNode(n.id);
  for (const e of edges) {
    if (!g.hasNode(e.source) || !g.hasNode(e.target)) continue;
    if (g.hasEdge(e.source, e.target)) continue;
    g.addEdge(e.source, e.target, { weight: e.weight ?? 1 });
  }
  return louvain(g);
}
