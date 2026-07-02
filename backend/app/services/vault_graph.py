"""Vault Graph builder — assembles a JSON graph representation of the
Markdown vault for the M.4 Three.js 3D visualization.

Composition:
- **Nodes**: one per vault note from VaultIndex.list_all(); id = vault-relative
  path, label = file stem (filename without .md). Carries type/agent/tags +
  viewCount from VaultActivity and optional cluster_id.
- **Edges**: wikilinks `[[name]]` extracted from each note's content (FTS5
  stored `content` column — no disk read needed). Resolved against
  nodes by label (file stem). First match wins on collisions; self-edges
  dropped; duplicate edges deduplicated and counted as `weight`.
- **Clusters**: optional k-means over vault embeddings from Qdrant
  `memory_vault` collection. Auto-k via silhouette (3, 5, 8). Skipped
  gracefully when `vault_embeddings is None`, the Qdrant scroll fails,
  fewer than 6 vectors exist, or silhouette score never crosses 0.1.

Performance budget (881 nodes target):
- list_all() over SQLite — milliseconds
- wikilink regex per content — millisecond range
- O(n) build for edges + dedup map
- Qdrant scroll (1 round-trip per batch) + k-means (sklearn) — sub-second
  for ~1k vectors at 768d on a Mac Mini M4

Memory: all `content` columns are loaded into memory at once (one
`list_all()` call). For 881 notes × ~3 KB this is ~2.5 MB — fine.

Read-only service: never writes to vault, Qdrant, or activity.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger("mc.vault_graph")

# Wikilink syntax: [[Name]] or [[Name|Alias]] — we capture the target name
# only and ignore the alias for graph resolution.
WIKILINK_RE = re.compile(r"\[\[([^\[\]|#]+?)(?:\|[^\[\]]+?)?(?:#[^\[\]]+?)?\]\]")

# Silhouette score threshold below which clustering output is considered
# noise — fall back to a single cluster spanning all nodes.
SILHOUETTE_MIN = 0.1

# Candidate cluster counts. Order matters only for ties — silhouette picks
# the best. We keep this small because k-means cost is O(n·k·iters).
KMEANS_K_CANDIDATES = (3, 5, 8)


def _stem(rel_path: str) -> str:
    """Return file stem (basename without .md extension).

    Used for wikilink target matching. Wikilinks like `[[my-note]]` resolve
    to a node whose label is `my-note`.
    """
    name = rel_path.rsplit("/", 1)[-1]
    if name.endswith(".md"):
        return name[:-3]
    return name


# Heading regex: matches any level heading, captures the heading text.
_HEADING_RE = re.compile(r"^#+\s+(.+?)\s*$", re.MULTILINE)


def resolve_label(*, frontmatter: dict, content: str, filename: str) -> str:
    """Three-step fallback for a note's display label.

    1. Frontmatter ``title`` field (non-empty after strip)
    2. First Markdown heading (any level)
    3. Filename stem
    """
    title = (frontmatter or {}).get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    if content:
        m = _HEADING_RE.search(content)
        if m:
            return m.group(1).strip()
    stem = filename.rsplit("/", 1)[-1].rsplit(".md", 1)[0]
    return stem


def _parse_tags(raw: str) -> list[str]:
    """VaultIndex stores tags as space-joined string. Reverse that here."""
    if not raw:
        return []
    return [t for t in raw.split(" ") if t]


def _extract_wikilinks(content: str) -> list[str]:
    """Return list of unique link targets (stripped, dedup, in order)."""
    if not content:
        return []
    seen: dict[str, None] = {}
    for m in WIKILINK_RE.finditer(content):
        target = m.group(1).strip()
        if target and target not in seen:
            seen[target] = None
    return list(seen.keys())


async def _fetch_embeddings(vault_embeddings: Any, paths: set[str]) -> dict[str, list[float]]:
    """Scroll Qdrant `memory_vault` collection and collect vectors keyed by
    payload['path']. Returns {} on any failure or empty collection.

    Fail-soft: every exception path returns {} so callers gracefully skip
    clustering instead of bubbling an error to the HTTP layer.
    """
    if vault_embeddings is None:
        return {}
    qdrant = getattr(vault_embeddings, "qdrant", None) or getattr(vault_embeddings, "qdrant_client", None)
    collection = getattr(vault_embeddings, "collection", "memory_vault")
    if qdrant is None:
        return {}

    out: dict[str, list[float]] = {}
    offset: Any = None
    try:
        while True:
            records, next_offset = await qdrant.scroll(
                collection_name=collection,
                limit=256,
                offset=offset,
                with_payload=True,
                with_vectors=True,
            )
            if not records:
                break
            for r in records:
                payload = getattr(r, "payload", None) or {}
                path = payload.get("path")
                if not path or path not in paths:
                    continue
                vec = getattr(r, "vector", None)
                if vec is None:
                    continue
                # Qdrant may return vector as dict keyed by name when named
                # vectors are configured; memory_vault uses a single unnamed
                # vector, so we expect a plain list/tuple of floats.
                if isinstance(vec, dict):
                    # Pick first vector regardless of name.
                    try:
                        vec = next(iter(vec.values()))
                    except StopIteration:
                        continue
                out[path] = list(vec)
            if next_offset is None:
                break
            offset = next_offset
    except Exception as e:  # fail-soft
        logger.warning("vault_graph: qdrant scroll failed, skipping clusters: %s", e)
        return {}

    return out


def _kmeans_cluster(paths: list[str], vectors: list[list[float]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """Run k-means with auto-k via silhouette. Returns (cluster_id_by_path,
    clusters_list). On any failure or low silhouette, returns single-cluster
    fallback (all nodes → cluster_id=0).
    """
    n = len(paths)
    if n < 6:
        # Too few samples for meaningful clustering — single cluster fallback.
        return _single_cluster_fallback(paths, vectors)

    try:
        import numpy as np
        from sklearn.cluster import KMeans
        from sklearn.metrics import silhouette_score
    except Exception as e:
        logger.warning("vault_graph: sklearn unavailable, single-cluster fallback: %s", e)
        return _single_cluster_fallback(paths, vectors)

    X = np.asarray(vectors, dtype=np.float32)
    best_k: int | None = None
    best_score = -1.0
    best_labels: Any = None

    for k in KMEANS_K_CANDIDATES:
        if k >= n:
            continue
        try:
            km = KMeans(n_clusters=k, random_state=42, n_init=4)
            labels = km.fit_predict(X)
            score = float(silhouette_score(X, labels))
        except Exception as e:
            logger.warning("vault_graph: k=%d failed: %s", k, e)
            continue
        if score > best_score:
            best_score = score
            best_k = k
            best_labels = labels

    if best_labels is None or best_score < SILHOUETTE_MIN:
        logger.info("vault_graph: silhouette %.3f below %.2f — single-cluster fallback", best_score, SILHOUETTE_MIN)
        return _single_cluster_fallback(paths, vectors)

    cluster_by_path = {paths[i]: int(best_labels[i]) for i in range(n)}
    # Build clusters list with centroids (mean of member vectors).
    clusters: list[dict[str, Any]] = []
    import numpy as np
    for cid in range(int(best_k or 0)):
        members = [paths[i] for i in range(n) if int(best_labels[i]) == cid]
        if not members:
            continue
        member_vecs = np.asarray(
            [vectors[i] for i in range(n) if int(best_labels[i]) == cid],
            dtype=np.float32,
        )
        centroid = member_vecs.mean(axis=0).tolist()
        clusters.append({"cluster_id": cid, "member_paths": members, "centroid": centroid})
    return cluster_by_path, clusters


def _single_cluster_fallback(paths: list[str], vectors: list[list[float]]) -> tuple[dict[str, int], list[dict[str, Any]]]:
    """All nodes in cluster 0. Centroid = mean if we have vectors, else empty."""
    cluster_by_path = {p: 0 for p in paths}
    if vectors:
        try:
            import numpy as np
            centroid = np.asarray(vectors, dtype=np.float32).mean(axis=0).tolist()
        except Exception:
            centroid = []
    else:
        centroid = []
    return cluster_by_path, [{"cluster_id": 0, "member_paths": list(paths), "centroid": centroid}]


async def build_graph(
    vault_index: Any,
    vault_embeddings: Any,
    vault_activity: Any,
    *,
    cluster: bool = True,
    heatmap: str = "30d",
    similarity_edges: bool = True,
) -> dict[str, Any]:
    """Build the vault graph as JSON.

    Returns:
        {
          "nodes": [{id, label, type, agent, tags, viewCount, cluster_id}],
          "edges": [{source, target, weight, kind?}],
          "clusters": [{cluster_id, member_paths, centroid}],
          "built_at": "...Z",
          "stats": {nodes, edges, clusters, build_ms}
        }

    When ``similarity_edges=True`` (default), ghost edges (kind="similarity")
    from Qdrant top-K nearest neighbours are appended after wikilink edges.
    Requires Qdrant embeddings; silently skipped if unavailable (W3-A).
    """
    t0 = time.perf_counter()

    # 1) Nodes from VaultIndex.list_all()
    rows = list(vault_index.list_all())
    # Build label-to-path map for wikilink resolution. First match wins on
    # collisions (M.4-T1 limitation — frontend renders these as-is).
    label_to_path: dict[str, str] = {}
    nodes: list[dict[str, Any]] = []
    path_set: set[str] = set()
    content_by_path: dict[str, str] = {}
    for row in rows:
        path = row.get("path") or ""
        if not path:
            continue
        path_set.add(path)
        content = row.get("content") or ""
        label = resolve_label(
            frontmatter={"title": row.get("title") or ""},
            content=content,
            filename=path,
        )
        # Wikilink resolution still uses the file stem so that [[my-note]]
        # continues to resolve regardless of whether the note has a heading.
        stem_key = _stem(path)
        if stem_key not in label_to_path:
            label_to_path[stem_key] = path
        content_by_path[path] = content
        nodes.append(
            {
                "id": path,
                "label": label,
                "type": row.get("type") or "",
                "agent": row.get("agent") or "",
                "tags": _parse_tags(row.get("tags") or ""),
                "viewCount": 0,
                "cluster_id": None,
            }
        )

    # 2) Heatmap — fill viewCount from VaultActivity.
    if vault_activity is not None:
        try:
            top = await vault_activity.top_n_views(limit=1000, window=heatmap)
        except Exception as e:
            logger.warning("vault_graph: top_n_views failed, viewCount stays 0: %s", e)
            top = []
        view_by_path = {entry["path"]: int(entry["score"]) for entry in top}
        for node in nodes:
            score = view_by_path.get(node["id"])
            if score is not None:
                node["viewCount"] = score

    # 3) Edges — wikilink extraction + dedup + self-edge drop.
    edge_counter: dict[tuple[str, str], int] = {}
    for path, content in content_by_path.items():
        for target in _extract_wikilinks(content):
            resolved = label_to_path.get(target)
            if resolved is None:
                continue
            if resolved == path:
                continue  # drop self-edges
            key = (path, resolved)
            edge_counter[key] = edge_counter.get(key, 0) + 1
    edges = [
        {"source": src, "target": tgt, "weight": w}
        for (src, tgt), w in edge_counter.items()
    ]

    # 4) Embeddings — fetch once, reuse for clustering + similarity edges.
    clusters: list[dict[str, Any]] = []
    vectors_by_path: dict[str, list[float]] = {}
    need_embeddings = (cluster or similarity_edges) and bool(nodes)
    if need_embeddings:
        vectors_by_path = await _fetch_embeddings(vault_embeddings, path_set)
        if not vectors_by_path:
            logger.info("vault_graph: no embeddings available — skipping clustering + similarity edges")

    # 4a) Clusters (optional, fail-soft).
    if cluster and nodes and vectors_by_path:
        ordered_paths = [p for p in path_set if p in vectors_by_path]
        ordered_vecs = [vectors_by_path[p] for p in ordered_paths]
        cluster_by_path, clusters = _kmeans_cluster(ordered_paths, ordered_vecs)
        for node in nodes:
            cid = cluster_by_path.get(node["id"])
            if cid is not None:
                node["cluster_id"] = cid

    # 4b) Ghost edges via Qdrant similarity (W3-A, optional, fail-soft).
    if similarity_edges and nodes and vectors_by_path:
        try:
            from app.services.vault_similarity_edges import build_similarity_edges_async

            qdrant = getattr(vault_embeddings, "qdrant", None) or getattr(
                vault_embeddings, "qdrant_client", None
            )
            collection = getattr(vault_embeddings, "collection", "memory_vault")
            if qdrant is not None:
                nodes_with_emb = [
                    {"id": path, "embedding": vec}
                    for path, vec in vectors_by_path.items()
                ]
                sim_edges = await build_similarity_edges_async(
                    qdrant,
                    nodes_with_emb,
                    top_k=3,
                    min_score=0.72,
                    collection=collection,
                )
                # Filter: drop edges pointing at notes that no longer exist
                # in the live vault (Qdrant may still hold embeddings for
                # W1-archived notes — unresolved refs crash d3-force on the
                # frontend with "Cannot create property 'vx' on string").
                live_ids = {n["id"] for n in nodes}
                sim_edges = [
                    e for e in sim_edges
                    if e["source"] in live_ids and e["target"] in live_ids
                ]
                edges.extend(sim_edges)
                logger.debug(
                    "vault_graph: added %d similarity ghost-edges (W3-A)", len(sim_edges)
                )
        except Exception as e:  # fail-soft — never block graph build
            logger.warning("vault_graph: similarity edges failed, skipping: %s", e)

    build_ms = int((time.perf_counter() - t0) * 1000)
    return {
        "nodes": nodes,
        "edges": edges,
        "clusters": clusters,
        "built_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "stats": {
            "nodes": len(nodes),
            "edges": len(edges),
            "clusters": len(clusters),
            "build_ms": build_ms,
        },
    }
