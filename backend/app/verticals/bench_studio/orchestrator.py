"""Benchmark Studio orchestrator — the production state machine.

Challenge lifecycle:  generating -> rendering -> composing -> review
                      (-> drafted -> published via drafts.py + core hooks)
Entry lifecycle:      pending -> generating -> generated -> rendered | failed

Design rules (spec §7):
  - Every step writes status + error to the DB — nothing hangs silently.
  - Partial failures never block: the grid is composed from surviving entries.
  - Spark entries generate inline (one Spark GPU — sequential); agent entries
    go through the normal fleet dispatch (auto_dispatch_task) and are
    collected by the task_done hook. NO new dispatch mechanism.
"""
from __future__ import annotations

import logging
import os
import re
import time
import uuid
from pathlib import Path

import httpx
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry

logger = logging.getLogger("mc.bench_studio")

# Same volume + env convention as services/visual_verifier.py and the
# mc-playwright sidecar (docker/mc-playwright/service.py).
SHARED_DELIVERABLES = Path(os.environ.get("SHARED_DELIVERABLES", "/shared-deliverables"))
PLAYWRIGHT_BASE = os.environ.get("MC_PLAYWRIGHT_URL", "http://mc-playwright:8790")

RECORD_DURATION_S = 10       # spec §4: default 10 s
RECORD_VIEWPORT = "desktop"  # 1440x900 (mc-playwright VIEWPORTS)
RECORD_TIMEOUT_S = 180.0
COMPOSE_TIMEOUT_S = 300.0
SPARK_TIMEOUT_S = 300.0
SPARK_MAX_TOKENS = 16384

GENERATION_SYSTEM_PROMPT = (
    "You are a one-shot frontend generator. Output a single complete "
    "index.html document and nothing else — no explanations, no markdown "
    "prose around it. Inline all CSS and JavaScript. The page must work "
    "offline (no external network requests)."
)

_FENCE_RE = re.compile(r"```(?:html)?\s*(.*?)```", re.DOTALL | re.IGNORECASE)


def _safe_label(label: str) -> str:
    """Filesystem-safe directory name for a model label."""
    return re.sub(r"[^a-zA-Z0-9_-]", "-", label)[:60] or "model"


def challenge_dir(challenge_id: uuid.UUID) -> Path:
    """Artifact root for one challenge: /shared-deliverables/bench-<id>/."""
    return SHARED_DELIVERABLES / f"bench-{challenge_id}"


def extract_html(raw: str | None) -> str:
    """Extract the HTML document from a model response.

    Strips markdown code fences; falls back to cutting leading prose before
    <!doctype / <html; otherwise returns the trimmed raw text.
    """
    text = (raw or "").strip()
    m = _FENCE_RE.search(text)
    if m:
        # Fence found: the content inside is already the HTML document.
        return m.group(1).strip()
    # No fence: trim leading prose before the first <!doctype or <html tag.
    lower = text.lower()
    for marker in ("<!doctype", "<html"):
        idx = lower.find(marker)
        if idx > 0:
            text = text[idx:]
            break
    return text.strip()


async def _spark_generate(prompt: str, model_override: str | None) -> tuple[str, dict]:
    """One-shot HTML generation against the Spark vLLM endpoint.

    Reuses SparkClient for URL + active-model resolution, but calls
    /chat/completions directly because SparkClient.complete() drops the
    usage block — the studio needs tokens/tok_per_s metrics (spec §3).

    Returns (content, metrics). Raises on HTTP errors — the caller converts
    that into entry.status = "failed".
    """
    from app.services.spark_client import SparkClient

    spark = SparkClient(timeout=SPARK_TIMEOUT_S)
    model = model_override or await spark._resolve_llm_model()
    started = time.monotonic()
    async with httpx.AsyncClient(timeout=spark.timeout) as cli:
        resp = await cli.post(
            f"{spark.llm_url}/chat/completions",
            json={
                "model": model,
                "messages": [
                    {"role": "system", "content": GENERATION_SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": SPARK_MAX_TOKENS,
                "temperature": 0.7,
                # Qwen3 thinking mode returns content=null; disable it
                # (same guard as SparkClient.complete).
                "chat_template_kwargs": {"enable_thinking": False},
            },
        )
        resp.raise_for_status()
    duration_ms = int((time.monotonic() - started) * 1000)
    data = resp.json()
    content = data["choices"][0]["message"]["content"] or ""

    metrics: dict = {"duration_ms": duration_ms}
    usage = data.get("usage") or {}
    if usage:
        tokens_out = usage.get("completion_tokens")
        metrics["tokens_in"] = usage.get("prompt_tokens")
        metrics["tokens_out"] = tokens_out
        if tokens_out and duration_ms:
            metrics["tok_per_s"] = round(tokens_out / (duration_ms / 1000), 1)
    return content, metrics


async def generate_spark_entry(
    session: AsyncSession, entry: BenchEntry, prompt: str
) -> None:
    """Spark path: direct one-shot call, write index.html, capture metrics.

    Never raises — failures land as entry.status='failed' + error text.
    """
    entry.status = "generating"
    session.add(entry)
    await session.commit()
    try:
        content, metrics = await _spark_generate(prompt, entry.spark_model)
        html = extract_html(content)
        if not html:
            raise ValueError("model returned no HTML content")
        out_dir = challenge_dir(entry.challenge_id) / _safe_label(entry.model_label)
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / "index.html"
        out_path.write_text(html, encoding="utf-8")
        entry.artifact_path = str(out_path)
        entry.metrics = {**(entry.metrics or {}), **metrics}
        entry.status = "generated"
        entry.error = None
    except Exception as exc:  # noqa: BLE001 — every failure must land in the DB
        entry.status = "failed"
        entry.error = f"generation failed: {exc}"[:2000]
        logger.warning("bench entry %s spark generation failed: %s", entry.id, exc)
    session.add(entry)
    await session.commit()


async def on_task_done(session: AsyncSession, task) -> None:  # pragma: no cover
    """task_done_hook — implemented in the state-machine part (Task 4)."""
    return None
