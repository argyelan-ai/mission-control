"""Slack file downloads — the one place MC fetches bytes from Slack.

Grew out of ``slack_voice.download_slack_audio`` when the generic file ingest
(the operator shares a PDF → References, ADR-053) needed the same download
with a different size cap. Voice and ingest share every Slack-specific pitfall — the
bot token, ``url_private_download``, the HTML-response-means-missing-scope
trap — so the download lives here ONCE and both callers pass their own cap.

Two improvements over the voice-era code, both about memory: the size is
checked BEFORE any byte is buffered (Slack declares it in the event's file
object and again in ``Content-Length``), and the body is streamed chunk-wise
with a running cap instead of trusting the headers — a lying header aborts
the read, it does not fill the RAM of a 5-GB Docker VM first.
"""
from __future__ import annotations

import logging

import httpx

logger = logging.getLogger("mc.slack_files")

_DOWNLOAD_TIMEOUT = 60.0


def declared_file_size(file: dict) -> int | None:
    """The size Slack claims for this file, or None. Cheap pre-check — the
    event carries it, so an oversized file is refused without any HTTP call."""
    size = file.get("size")
    if isinstance(size, int) and size >= 0:
        return size
    return None


async def download_slack_file(file: dict, *, max_bytes: int) -> bytes | None:
    """The file's bytes, or None with a logged reason. Never raises.

    ``url_private_download`` is served only with the bot token — and only when
    the app holds the ``files:read`` scope. Slack answers a scope problem with
    an HTML login page and status 200, so the content type is checked too:
    HTML here means "no access", not the file.
    """
    from app.services.slack_client import get_bot_token

    url = file.get("url_private_download") or file.get("url_private")
    if not url:
        logger.warning("slack files: file %s has no download url", file.get("id"))
        return None

    declared = declared_file_size(file)
    if declared is not None and declared > max_bytes:
        logger.warning(
            "slack files: file %s declares %d bytes > cap %d — not downloaded",
            file.get("id"), declared, max_bytes,
        )
        return None

    token = await get_bot_token()
    if not token:
        logger.warning("slack files: no bot token — cannot download")
        return None

    try:
        async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT) as client:
            async with client.stream(
                "GET", url, headers={"Authorization": f"Bearer {token}"},
                follow_redirects=True,
            ) as resp:
                if resp.status_code != 200:
                    logger.warning("slack files: download HTTP %s", resp.status_code)
                    return None
                if "text/html" in resp.headers.get("content-type", ""):
                    logger.warning(
                        "slack files: got an HTML page instead of the file — the "
                        "app is probably missing the files:read scope "
                        "(re-install after adding it)"
                    )
                    return None
                header_len = resp.headers.get("content-length")
                if header_len and header_len.isdigit() and int(header_len) > max_bytes:
                    logger.warning(
                        "slack files: Content-Length %s > cap %d — refusing "
                        "before buffering", header_len, max_bytes,
                    )
                    return None

                chunks: list[bytes] = []
                total = 0
                async for chunk in resp.aiter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        # Headers lied (or were absent): stop mid-stream.
                        logger.warning(
                            "slack files: stream for %s exceeded cap %d — aborted",
                            file.get("id"), max_bytes,
                        )
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)
    except httpx.HTTPError as exc:
        logger.warning("slack files: download failed: %s", type(exc).__name__)
        return None
