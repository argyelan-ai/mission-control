"""Arbitrary-URL endpoint probing for the add-runtime wizard (ADR-054).

Fingerprints the engine behind an OpenAI-compatible base URL:
  - LM Studio exposes a native REST API at ``/api/v0/models``
  - vLLM exposes ``/version``
  - anything else answering ``/v1/models`` is generic openai_compatible
"""

from __future__ import annotations

import logging

import httpx

logger = logging.getLogger(__name__)

_TIMEOUT = 5.0


async def probe_endpoint_url(url: str) -> dict:
    base = url.rstrip("/")
    models: list[str] = []
    reachable = False
    error: str | None = None

    candidates = (
        [f"{base}/models"] if base.endswith("/v1")
        else [f"{base}/v1/models", f"{base}/models"]
    )

    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for candidate in candidates:
            try:
                resp = await client.get(candidate)
                if resp.status_code != 200:
                    error = f"HTTP {resp.status_code} from {candidate}"
                    continue
                data = resp.json()
                items = data.get("data") if isinstance(data, dict) else None
                if isinstance(items, list):
                    models = [
                        m["id"] for m in items
                        if isinstance(m, dict) and isinstance(m.get("id"), str)
                    ]
                    reachable = True
                    break
            except (httpx.HTTPError, ValueError) as exc:
                # ValueError covers resp.json() raising on a 200 response with a
                # non-JSON body (e.g. an HTML landing page) — treat the same as
                # an unreachable candidate instead of bubbling up as a 500.
                error = str(exc)

        if not reachable:
            return {
                "reachable": False, "models": [], "detected_type": None,
                "suggested_model": None,
                "error": error or "no OpenAI-compatible /models endpoint answered",
            }

        root = base[: -len("/v1")] if base.endswith("/v1") else base
        detected = "openai_compatible"
        try:
            r = await client.get(f"{root}/api/v0/models")  # LM Studio REST API
            if r.status_code == 200:
                detected = "lmstudio"
        except httpx.HTTPError as exc:
            logger.debug("lmstudio fingerprint probe failed for %s: %s", root, exc)
        if detected != "lmstudio":
            try:
                r = await client.get(f"{root}/version")  # vLLM version endpoint
                if r.status_code == 200 and "version" in r.text:
                    detected = "vllm_docker"
            except httpx.HTTPError as exc:
                logger.debug("vllm fingerprint probe failed for %s: %s", root, exc)

    return {
        "reachable": True,
        "models": models,
        "detected_type": detected,
        "suggested_model": models[0] if models else None,
        "error": None,
    }
