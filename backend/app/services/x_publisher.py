"""X (Twitter) Publisher — draft validation + tweepy-backed posting.

Part of the generic Draft -> Approve -> Post flow (Approval action_type
"x_post", see routers/x_posts.py + the hook in routers/approvals.py).

Auth: OAuth 1.0a user context (tweepy.Client with consumer key/secret +
access token/secret) — required for POST /2/tweets on behalf of a user
account. Tokens are System-Tokens (ADR-033: "wie MC selbst mit der Welt
redet") and therefore live in the `secrets` table, not `credentials`.

Secret keys (set once by the operator via Settings -> Secrets, Admin-only):
    x_api_key             (Consumer/API Key)
    x_api_secret           (Consumer/API Key Secret)
    x_access_token          (Access Token)
    x_access_token_secret   (Access Token Secret)

Note on `publish_adapters.py`: the news-studio vertical (maintainer-private,
stripped from the public OSS repo — see ADR "extract news-studio into a
strippable vertical module") already has a `publish_twitter()` used for
storyboard threads, keyed on a single `twitter_bearer_token` secret. That
adapter is thread-shaped (multi-tweet, split on blank lines) and specific to
storyboards; it doesn't apply outside that vertical and a bearer token alone
cannot authenticate user-context POSTs against X API v2. This module is the
generic, always-available counterpart for single-post drafts approved via the
core Approval flow, using the correct OAuth 1.0a user-context credentials.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

from app.services.secrets_helper import get_secret_plaintext_by_key

log = logging.getLogger("mc.x_publisher")

MAX_TWEET_LENGTH = 280
LINK_COST_HINT = (
    "Link erkannt — X berechnet fuer Posts mit Link zusaetzlich Kosten "
    "(ca. $0.015-0.20 je nach Volumen, Stand Juli 2026)."
)

_URL_RE = re.compile(r"https?://\S+")

_SECRET_KEYS = {
    "api_key": "x_api_key",
    "api_secret": "x_api_secret",
    "access_token": "x_access_token",
    "access_token_secret": "x_access_token_secret",
}


@dataclass
class DraftValidation:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    has_link: bool = False


def validate_draft(text: str) -> DraftValidation:
    """Validates a tweet draft: length <= 280, non-empty, link cost hint."""
    errors: list[str] = []
    warnings: list[str] = []

    if not text or not text.strip():
        errors.append("Draft ist leer")
        return DraftValidation(ok=False, errors=errors)

    length = len(text)
    if length > MAX_TWEET_LENGTH:
        errors.append(f"Draft hat {length} Zeichen (max {MAX_TWEET_LENGTH})")

    has_link = bool(_URL_RE.search(text))
    if has_link:
        warnings.append(LINK_COST_HINT)

    return DraftValidation(ok=not errors, errors=errors, warnings=warnings, has_link=has_link)


# ── Media validation ────────────────────────────────────────────────────────

MEDIA_ROOT = Path("/shared-deliverables")  # backend mounts this shared volume read-only
VIDEO_EXTENSIONS = {".mp4"}
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg"}
MAX_IMAGES_PER_TWEET = 4


def validate_media(media_paths: list[str], *, root: Path | None = None) -> DraftValidation:
    """Validates a tweet media set: 1 video (mp4) OR up to 4 images (png/jpg),
    never mixed; files must exist and live under `root` (default:
    /shared-deliverables — the volume shared with mc-playwright)."""
    effective_root = (root or MEDIA_ROOT).resolve()
    errors: list[str] = []

    if not media_paths:
        errors.append("media_paths ist leer")
        return DraftValidation(ok=False, errors=errors)

    videos: list[Path] = []
    images: list[Path] = []
    for raw in media_paths:
        path = Path(raw)
        if not path.is_absolute():
            errors.append(f"Pfad muss absolut sein: {raw}")
            continue
        resolved = path.resolve()
        if not resolved.is_relative_to(effective_root):
            errors.append(f"Pfad liegt nicht unter {effective_root}: {raw}")
            continue
        if not resolved.is_file():
            errors.append(f"Datei existiert nicht: {raw}")
            continue
        suffix = resolved.suffix.lower()
        if suffix in VIDEO_EXTENSIONS:
            videos.append(resolved)
        elif suffix in IMAGE_EXTENSIONS:
            images.append(resolved)
        else:
            errors.append(f"Nicht unterstuetzter Medientyp '{suffix}': {raw}")
            continue

    if videos and images:
        errors.append(
            "Video und Bilder im selben Tweet sind nicht erlaubt "
            "(1 Video ODER bis zu 4 Bilder)"
        )
    if len(videos) > 1:
        errors.append(f"Nur 1 Video pro Tweet erlaubt ({len(videos)} uebergeben)")
    if len(images) > MAX_IMAGES_PER_TWEET:
        errors.append(
            f"Maximal {MAX_IMAGES_PER_TWEET} Bilder pro Tweet ({len(images)} uebergeben)"
        )

    return DraftValidation(ok=not errors, errors=errors)


class XPublisherError(Exception):
    """Raised for configuration errors (missing secrets) — never for API-side failures."""


async def _load_secret_values(session: AsyncSession) -> dict[str, str]:
    """Loads the 4 X secrets from the vault. Raises XPublisherError if any is missing."""
    values: dict[str, str] = {}
    missing: list[str] = []
    for arg_name, secret_key in _SECRET_KEYS.items():
        value = await get_secret_plaintext_by_key(session, secret_key)
        if not value:
            missing.append(secret_key)
        else:
            values[arg_name] = value

    if missing:
        raise XPublisherError(
            "X-Secrets fehlen in der Vault (Settings -> Secrets, Admin): "
            + ", ".join(missing)
        )
    return values


async def _load_client(session: AsyncSession):
    """Builds a tweepy.Client from the 4 secrets. Raises XPublisherError if any is missing."""
    import tweepy  # local import: keeps tweepy optional for code paths that never post

    values = await _load_secret_values(session)
    return tweepy.Client(
        consumer_key=values["api_key"],
        consumer_secret=values["api_secret"],
        access_token=values["access_token"],
        access_token_secret=values["access_token_secret"],
    )


async def _load_api(session: AsyncSession):
    """Builds a tweepy.API (v1.1) from the same 4 secrets — required for
    media_upload: tweepy 4.14's v2 Client has no media upload, only the v1.1
    API with OAuth1UserHandler supports INIT/APPEND/FINALIZE chunked uploads.
    Raises XPublisherError if any secret is missing."""
    import tweepy

    values = await _load_secret_values(session)
    auth = tweepy.OAuth1UserHandler(
        values["api_key"],
        values["api_secret"],
        values["access_token"],
        values["access_token_secret"],
    )
    return tweepy.API(auth)


def _classify_tweepy_error(exc: Exception) -> tuple[str, str]:
    """Maps a tweepy exception to (error_type, message) for a clean Result — never lets
    the caller crash on rate-limit/auth/duplicate errors."""
    import tweepy

    if isinstance(exc, tweepy.TooManyRequests):
        return "rate_limited", "X API Rate-Limit erreicht (429) — spaeter erneut versuchen."
    if isinstance(exc, tweepy.Forbidden):
        text = str(exc)
        if "duplicate" in text.lower():
            return "duplicate", "X lehnt den Post als Duplikat ab (403, duplicate content)."
        return "forbidden", f"X API verweigert den Post (403): {text[:300]}"
    if isinstance(exc, tweepy.Unauthorized):
        return "unauthorized", "X API Auth fehlgeschlagen (401) — Secrets pruefen/erneuern."
    if isinstance(exc, tweepy.BadRequest):
        return "bad_request", f"X API lehnt den Request ab (400): {str(exc)[:300]}"
    if isinstance(exc, tweepy.TweepyException):
        return "api_error", f"X API Fehler: {str(exc)[:300]}"
    return "unknown_error", f"Unerwarteter Fehler: {exc}"


async def post_text(session: AsyncSession, text: str) -> dict[str, Any]:
    """Posts a single tweet via tweepy. Returns a Result dict, never raises for
    API-side failures (rate-limit/403/duplicate/etc) — only for missing config.

    Returns:
        {"ok": True, "tweet_id": str, "url": str}
        {"ok": False, "error_type": str, "error": str}
    """
    validation = validate_draft(text)
    if not validation.ok:
        return {"ok": False, "error_type": "invalid_draft", "error": "; ".join(validation.errors)}

    try:
        client = await _load_client(session)
    except XPublisherError as e:
        return {"ok": False, "error_type": "missing_secrets", "error": str(e)}

    try:
        # tweepy.Client is sync (requests under the hood) — run off the event loop.
        response = await asyncio.to_thread(client.create_tweet, text=text)
    except Exception as exc:  # noqa: BLE001 — deliberately broad, classified below
        error_type, message = _classify_tweepy_error(exc)
        log.warning("X post failed (%s): %s", error_type, message)
        return {"ok": False, "error_type": error_type, "error": message}

    tweet_id = str(response.data["id"])
    return {
        "ok": True,
        "tweet_id": tweet_id,
        "url": f"https://x.com/i/status/{tweet_id}",
    }


# ── Media posting ────────────────────────────────────────────────────────────

VIDEO_PROCESSING_TIMEOUT_S = 300  # X video processing rarely exceeds ~1 min for <2 min clips


class MediaUploadError(Exception):
    """Internal: media upload/processing failed — converted to a clean Result
    ("media_upload_failed") by post_media, never propagated to callers."""


async def _wait_for_video_processing(api, media_id: int | str) -> None:
    """Polls the v1.1 STATUS endpoint until X finished processing the video.

    Chunked video uploads are async on X's side: FINALIZE returns
    processing_info with state pending/in_progress; the media_id is only
    usable in create_tweet once state == succeeded. tweepy 4.x may already
    wait during media_upload (wait_for_async_finalize) — in that case the
    first STATUS poll returns succeeded/no processing_info and we return
    immediately. Raises MediaUploadError on state == failed or timeout.
    """
    deadline = time.monotonic() + VIDEO_PROCESSING_TIMEOUT_S
    while True:
        status = await asyncio.to_thread(api.get_media_upload_status, media_id)
        info = getattr(status, "processing_info", None) or {}
        state = info.get("state")
        if not info or state == "succeeded":
            return
        if state == "failed":
            error = info.get("error") or {}
            raise MediaUploadError(
                "X-Video-Processing fehlgeschlagen: "
                f"{error.get('message', state)} (media_id={media_id})"
            )
        if time.monotonic() > deadline:
            raise MediaUploadError(
                f"X-Video-Processing Timeout nach {VIDEO_PROCESSING_TIMEOUT_S}s "
                f"(media_id={media_id}, state={state})"
            )
        await asyncio.sleep(info.get("check_after_secs", 2))


async def _upload_media(api, media_paths: list[str]) -> list[str]:
    """Uploads each file via the v1.1 media endpoint. Videos use chunked
    upload + processing wait; images a simple upload. Returns media_ids
    in input order. Raises MediaUploadError / tweepy exceptions on failure."""
    media_ids: list[str] = []
    for raw in media_paths:
        is_video = raw.lower().endswith(".mp4")
        media = await asyncio.to_thread(
            api.media_upload,
            raw,
            media_category="tweet_video" if is_video else "tweet_image",
            chunked=is_video,
        )
        if is_video:
            await _wait_for_video_processing(api, media.media_id)
        media_ids.append(str(media.media_id))
    return media_ids


async def post_media(
    session: AsyncSession, text: str, media_paths: list[str]
) -> dict[str, Any]:
    """Posts a tweet with media (1 video OR up to 4 images) via tweepy.
    Returns a Result dict, never raises for API-side failures — mirrors
    post_text.

    Returns:
        {"ok": True, "tweet_id": str, "url": str, "media_ids": list[str]}
        {"ok": False, "error_type": str, "error": str}
    """
    validation = validate_draft(text)
    if not validation.ok:
        return {"ok": False, "error_type": "invalid_draft", "error": "; ".join(validation.errors)}

    media_validation = validate_media(media_paths)
    if not media_validation.ok:
        return {"ok": False, "error_type": "invalid_media", "error": "; ".join(media_validation.errors)}

    try:
        client = await _load_client(session)
        api = await _load_api(session)
    except XPublisherError as e:
        return {"ok": False, "error_type": "missing_secrets", "error": str(e)}

    try:
        media_ids = await _upload_media(api, media_paths)
    except MediaUploadError as exc:
        log.warning("X media upload failed: %s", exc)
        return {"ok": False, "error_type": "media_upload_failed", "error": str(exc)}
    except Exception as exc:  # noqa: BLE001 — deliberately broad, classified below
        _, message = _classify_tweepy_error(exc)
        log.warning("X media upload failed (media_upload_failed): %s", message)
        return {"ok": False, "error_type": "media_upload_failed", "error": message}

    try:
        response = await asyncio.to_thread(client.create_tweet, text=text, media_ids=media_ids)
    except Exception as exc:  # noqa: BLE001 — deliberately broad, classified below
        error_type, message = _classify_tweepy_error(exc)
        log.warning("X media post failed (%s): %s", error_type, message)
        return {"ok": False, "error_type": error_type, "error": message}

    tweet_id = str(response.data["id"])
    return {
        "ok": True,
        "tweet_id": tweet_id,
        "url": f"https://x.com/i/status/{tweet_id}",
        "media_ids": media_ids,
    }
