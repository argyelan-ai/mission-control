#!/usr/bin/env python3
"""Qdrant Memory-Search Latency Probe (MEM-04).

Manual benchmark for memory-search filtered-query latency. Run before AND
after the agent_id + board_id payload index lands; capture both numbers in
.planning/phases/02-performance-diagnosis-quick-wins/02-03-SUMMARY.md.

Roadmap Success Criterion 3: latency drops by an order of magnitude on a
500+-entry collection.

Run inside the backend venv (imports app.config.Settings):
  cd $HOME/Workspace/Projects/mission-control
  source backend/.venv/bin/activate
  python3 tools/qdrant-latency-probe.py [board_id]

Env overrides (default targets the docker-compose host-published port):
  QDRANT_HOST   default: 127.0.0.1   (Settings.qdrant_host says "qdrant"
                                      which only resolves inside the
                                      backend container — useless on host)
  QDRANT_PORT   default: 6333

If [board_id] is omitted, the probe auto-discovers the board_id with the
highest point count in memory_semantic.
"""
from __future__ import annotations

import asyncio
import os
import statistics
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# Allow `import app.*` when run from repo root
sys.path.insert(0, str(PROJECT_ROOT / "backend"))

from qdrant_client import AsyncQdrantClient  # noqa: E402
from qdrant_client.http import models as qmodels  # noqa: E402

COLLECTION = "memory_semantic"
N_RUNS = 5
TOP_K = 10
EMBED_DIM = 768


def _resolve_host_port() -> tuple[str, int]:
    """Prefer explicit env, fall back to localhost (host-side default).

    Settings.qdrant_host=`qdrant` only works inside the docker network;
    running this probe from the host needs 127.0.0.1.
    """
    host = os.environ.get("QDRANT_HOST")
    port = os.environ.get("QDRANT_PORT")
    if host and port:
        return host, int(port)
    # Try to read app.config.Settings, but rewrite "qdrant" to "127.0.0.1"
    # for host-side use.
    try:
        from app.config import Settings  # noqa: WPS433 — lazy
        s = Settings()
        cfg_host = s.qdrant_host
        cfg_port = s.qdrant_port
        if cfg_host == "qdrant":  # docker-DNS only — not reachable from host
            cfg_host = "127.0.0.1"
        return host or cfg_host, int(port) if port else cfg_port
    except Exception:
        return host or "127.0.0.1", int(port) if port else 6333


async def discover_busiest_board(client: AsyncQdrantClient) -> str | None:
    """Scroll memory_semantic and find the board_id with the most points."""
    counts: dict[str, int] = {}
    next_offset = None
    while True:
        records, next_offset = await client.scroll(
            collection_name=COLLECTION,
            with_payload=True,
            with_vectors=False,
            limit=512,
            offset=next_offset,
        )
        for r in records:
            bid = (r.payload or {}).get("board_id")
            if bid:
                counts[bid] = counts.get(bid, 0) + 1
        if next_offset is None:
            break
    if not counts:
        return None
    busiest = max(counts.items(), key=lambda x: x[1])
    print(f"Auto-discovered busiest board: {busiest[0]} ({busiest[1]} entries)", flush=True)
    return busiest[0]


async def probe(board_id: str | None) -> int:
    host, port = _resolve_host_port()
    print(f"Connecting to Qdrant at {host}:{port}", flush=True)
    client = AsyncQdrantClient(host=host, port=port, timeout=10)
    try:
        if board_id is None:
            board_id = await discover_busiest_board(client)
        if board_id is None:
            print("No board_id with points in memory_semantic — cannot probe.", flush=True)
            return 1

        # Random unit vector for the query — content doesn't matter, we're
        # measuring filter latency, not relevance.
        import random
        random.seed(42)
        query_vec = [random.gauss(0, 1) for _ in range(EMBED_DIM)]

        flt = qmodels.Filter(must=[qmodels.FieldCondition(
            key="board_id",
            match=qmodels.MatchValue(value=board_id),
        )])

        # Warm-up
        await client.query_points(
            collection_name=COLLECTION,
            query=query_vec,
            query_filter=flt,
            limit=TOP_K,
            with_payload=False,
        )

        timings_ms: list[float] = []
        for i in range(N_RUNS):
            t0 = time.perf_counter()
            res = await client.query_points(
                collection_name=COLLECTION,
                query=query_vec,
                query_filter=flt,
                limit=TOP_K,
                with_payload=False,
            )
            dt = (time.perf_counter() - t0) * 1000
            timings_ms.append(dt)
            print(f"  run {i+1}/{N_RUNS}: {dt:.2f} ms ({len(res.points)} hits)", flush=True)

        print()
        print(f"## Latency probe — collection={COLLECTION}, board_id={board_id}")
        print()
        print("| metric | ms |")
        print("|---|---|")
        print(f"| min    | {min(timings_ms):.2f} |")
        print(f"| median | {statistics.median(timings_ms):.2f} |")
        print(f"| max    | {max(timings_ms):.2f} |")
        print(f"| runs   | {N_RUNS} |")
        return 0
    finally:
        await client.close()


def main(argv: list[str]) -> int:
    board_id = argv[0] if argv else None
    return asyncio.run(probe(board_id))


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
