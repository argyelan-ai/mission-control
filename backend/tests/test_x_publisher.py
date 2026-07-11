"""
Tests for app.services.x_publisher — draft validation + tweepy-backed posting.

Covers:
- validate_draft(): length limit, empty draft, link cost hint
- post_text(): missing secrets -> clean error (no crash)
- post_text(): success -> tweet_id + url
- post_text(): tweepy errors (429/403/duplicate) -> classified clean Result, no raise
"""

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import tweepy
from sqlmodel.ext.asyncio.session import AsyncSession

from app.services import x_publisher
from tests.conftest import test_engine


# ── validate_draft ───────────────────────────────────────────────────────────


def test_validate_draft_ok():
    result = x_publisher.validate_draft("Hello world, MC now posts to X.")
    assert result.ok is True
    assert result.errors == []
    assert result.has_link is False


def test_validate_draft_empty():
    result = x_publisher.validate_draft("   ")
    assert result.ok is False
    assert "leer" in result.errors[0].lower()


def test_validate_draft_too_long():
    result = x_publisher.validate_draft("x" * 281)
    assert result.ok is False
    assert "281" in result.errors[0]


def test_validate_draft_at_limit_ok():
    result = x_publisher.validate_draft("x" * 280)
    assert result.ok is True


def test_validate_draft_link_cost_hint():
    result = x_publisher.validate_draft("Check this out: https://example.com/post")
    assert result.ok is True
    assert result.has_link is True
    assert any("Kosten" in w for w in result.warnings)


# ── validate_media ───────────────────────────────────────────────────────────


def _touch(root, rel: str):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def test_validate_media_empty_list_fails(tmp_path):
    result = x_publisher.validate_media([], root=tmp_path)
    assert result.ok is False
    assert "leer" in result.errors[0].lower()


def test_validate_media_single_video_ok(tmp_path):
    video = _touch(tmp_path, "bench-1/grid.mp4")
    result = x_publisher.validate_media([str(video)], root=tmp_path)
    assert result.ok is True
    assert result.errors == []


def test_validate_media_four_images_ok(tmp_path):
    paths = [str(_touch(tmp_path, f"bench-1/shot-{i}.png")) for i in range(4)]
    result = x_publisher.validate_media(paths, root=tmp_path)
    assert result.ok is True


def test_validate_media_five_images_fails(tmp_path):
    paths = [str(_touch(tmp_path, f"bench-1/shot-{i}.jpg")) for i in range(5)]
    result = x_publisher.validate_media(paths, root=tmp_path)
    assert result.ok is False
    assert any("4" in e for e in result.errors)


def test_validate_media_two_videos_fails(tmp_path):
    paths = [str(_touch(tmp_path, f"bench-1/v{i}.mp4")) for i in range(2)]
    result = x_publisher.validate_media(paths, root=tmp_path)
    assert result.ok is False
    assert any("1 Video" in e for e in result.errors)


def test_validate_media_mixed_video_and_image_fails(tmp_path):
    video = _touch(tmp_path, "bench-1/grid.mp4")
    image = _touch(tmp_path, "bench-1/shot.png")
    result = x_publisher.validate_media([str(video), str(image)], root=tmp_path)
    assert result.ok is False
    assert any("nicht erlaubt" in e for e in result.errors)


def test_validate_media_unsupported_extension_fails(tmp_path):
    gif = _touch(tmp_path, "bench-1/anim.gif")
    result = x_publisher.validate_media([str(gif)], root=tmp_path)
    assert result.ok is False
    assert any(".gif" in e for e in result.errors)


def test_validate_media_missing_file_fails(tmp_path):
    result = x_publisher.validate_media([str(tmp_path / "bench-1/nope.png")], root=tmp_path)
    assert result.ok is False
    assert any("existiert nicht" in e for e in result.errors)


def test_validate_media_escaping_containment_fails(tmp_path):
    inside = tmp_path / "deliverables"
    inside.mkdir()
    outside = _touch(tmp_path, "outside/evil.png")
    result = x_publisher.validate_media(
        [str(inside / ".." / "outside" / "evil.png")], root=inside
    )
    assert result.ok is False
    assert any("nicht unter" in e for e in result.errors)
    # sanity: the file actually exists, only containment rejects it
    assert outside.is_file()


def test_validate_media_relative_path_fails(tmp_path):
    result = x_publisher.validate_media(["bench-1/shot.png"], root=tmp_path)
    assert result.ok is False
    assert any("absolut" in e for e in result.errors)


def test_validate_media_default_root_is_shared_deliverables():
    # default root: a path clearly outside /shared-deliverables is rejected
    result = x_publisher.validate_media(["/etc/passwd.png"])
    assert result.ok is False
    assert any("/shared-deliverables" in e for e in result.errors)


def test_validate_media_missing_files_do_not_count_toward_limits(tmp_path):
    # Non-existent .mp4 files should not be counted toward the "Nur 1 Video" limit.
    # Only the "existiert nicht" error should appear, not a limit error.
    video1 = str(tmp_path / "bench-1/v1.mp4")
    video2 = str(tmp_path / "bench-1/v2.mp4")
    result = x_publisher.validate_media([video1, video2], root=tmp_path)
    assert result.ok is False
    # Both files should produce "existiert nicht" errors
    assert sum(1 for e in result.errors if "existiert nicht" in e) == 2
    # But should NOT produce a "Nur 1 Video" error (since non-existent files aren't counted)
    assert not any("Nur 1 Video" in e for e in result.errors)


# ── post_text: missing secrets ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_post_text_missing_secrets_returns_clean_error():
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(return_value=None),
        ):
            result = await x_publisher.post_text(session, "Hello world")

    assert result["ok"] is False
    assert result["error_type"] == "missing_secrets"
    assert "x_api_key" in result["error"]


@pytest.mark.asyncio
async def test_post_text_invalid_draft_short_circuits_before_secrets():
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=AssertionError("should not be called")),
        ):
            result = await x_publisher.post_text(session, "x" * 500)

    assert result["ok"] is False
    assert result["error_type"] == "invalid_draft"


# ── post_text: success + error paths (tweepy mocked) ────────────────────────


def _fake_secret_lookup(_session, key: str):
    return {
        "x_api_key": "ck",
        "x_api_secret": "cs",
        "x_access_token": "at",
        "x_access_token_secret": "ats",
    }.get(key)


@pytest.mark.asyncio
async def test_post_text_success():
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.data = {"id": "1234567890"}
    mock_client.create_tweet.return_value = mock_response

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client):
            result = await x_publisher.post_text(session, "Hello world")

    assert result["ok"] is True
    assert result["tweet_id"] == "1234567890"
    assert result["url"] == "https://x.com/i/status/1234567890"
    mock_client.create_tweet.assert_called_once_with(text="Hello world")


@pytest.mark.asyncio
async def test_post_text_rate_limited():
    mock_client = MagicMock()
    mock_response = MagicMock(status_code=429, reason="Too Many Requests")
    mock_client.create_tweet.side_effect = tweepy.TooManyRequests(
        mock_response, response_json={"errors": []}
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client):
            result = await x_publisher.post_text(session, "Hello world")

    assert result["ok"] is False
    assert result["error_type"] == "rate_limited"


@pytest.mark.asyncio
async def test_post_text_forbidden_duplicate():
    mock_client = MagicMock()
    mock_response = MagicMock(status_code=403, reason="Forbidden")
    mock_client.create_tweet.side_effect = tweepy.Forbidden(
        mock_response,
        response_json={"errors": [{"message": "Status is a duplicate."}]},
    )

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client):
            result = await x_publisher.post_text(session, "Hello world")

    assert result["ok"] is False
    assert result["error_type"] == "duplicate"


@pytest.mark.asyncio
async def test_post_text_unexpected_exception_does_not_crash():
    mock_client = MagicMock()
    mock_client.create_tweet.side_effect = RuntimeError("network exploded")

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client):
            result = await x_publisher.post_text(session, "Hello world")

    assert result["ok"] is False
    assert result["error_type"] == "unknown_error"


# ── _load_api (tweepy v1.1 API for media upload) ────────────────────────────


@pytest.mark.asyncio
async def test_load_api_builds_oauth1_api():
    mock_auth = MagicMock()
    mock_api = MagicMock()

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.OAuth1UserHandler", return_value=mock_auth) as mock_handler, patch(
            "tweepy.API", return_value=mock_api
        ) as mock_api_cls:
            api = await x_publisher._load_api(session)

    assert api is mock_api
    mock_handler.assert_called_once_with("ck", "cs", "at", "ats")
    mock_api_cls.assert_called_once_with(mock_auth)


@pytest.mark.asyncio
async def test_load_api_missing_secrets_raises():
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(return_value=None),
        ):
            with pytest.raises(x_publisher.XPublisherError):
                await x_publisher._load_api(session)


# ── post_media: happy paths ──────────────────────────────────────────────────


def _processing_status(state: str | None, check_after: int = 0):
    status = MagicMock()
    if state is None:
        status.processing_info = None
    else:
        status.processing_info = {"state": state, "check_after_secs": check_after}
    return status


def _mock_tweet_client(tweet_id: str = "555"):
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.data = {"id": tweet_id}
    mock_client.create_tweet.return_value = mock_response
    return mock_client


@pytest.mark.asyncio
async def test_post_media_images_success(tmp_path, monkeypatch):
    monkeypatch.setattr(x_publisher, "MEDIA_ROOT", tmp_path)
    img1 = _touch(tmp_path, "bench-1/shot-1.png")
    img2 = _touch(tmp_path, "bench-1/shot-2.jpg")

    mock_api = MagicMock()
    media1, media2 = MagicMock(), MagicMock()
    media1.media_id, media1.processing_info = 111, None
    media2.media_id, media2.processing_info = 222, None
    mock_api.media_upload.side_effect = [media1, media2]
    mock_client = _mock_tweet_client("555")

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client), patch(
            "tweepy.OAuth1UserHandler", return_value=MagicMock()
        ), patch("tweepy.API", return_value=mock_api):
            result = await x_publisher.post_media(
                session, "Two screenshots", [str(img1), str(img2)]
            )

    assert result["ok"] is True
    assert result["tweet_id"] == "555"
    assert result["url"] == "https://x.com/i/status/555"
    assert result["media_ids"] == ["111", "222"]
    mock_api.media_upload.assert_any_call(
        str(img1), media_category="tweet_image", chunked=False
    )
    mock_api.media_upload.assert_any_call(
        str(img2), media_category="tweet_image", chunked=False
    )
    mock_api.get_media_upload_status.assert_not_called()  # images: no processing wait
    mock_client.create_tweet.assert_called_once_with(
        text="Two screenshots", media_ids=["111", "222"]
    )


@pytest.mark.asyncio
async def test_post_media_video_waits_for_processing(tmp_path, monkeypatch):
    monkeypatch.setattr(x_publisher, "MEDIA_ROOT", tmp_path)
    video = _touch(tmp_path, "bench-1/grid.mp4")

    mock_api = MagicMock()
    media = MagicMock()
    media.media_id = 4242
    mock_api.media_upload.return_value = media
    mock_api.get_media_upload_status.side_effect = [
        _processing_status("in_progress", check_after=0),
        _processing_status("succeeded"),
    ]
    mock_client = _mock_tweet_client("777")

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client), patch(
            "tweepy.OAuth1UserHandler", return_value=MagicMock()
        ), patch("tweepy.API", return_value=mock_api):
            result = await x_publisher.post_media(session, "Grid video", [str(video)])

    assert result["ok"] is True
    assert result["tweet_id"] == "777"
    assert result["media_ids"] == ["4242"]
    mock_api.media_upload.assert_called_once_with(
        str(video), media_category="tweet_video", chunked=True
    )
    assert mock_api.get_media_upload_status.call_count == 2
    mock_client.create_tweet.assert_called_once_with(
        text="Grid video", media_ids=["4242"]
    )


@pytest.mark.asyncio
async def test_post_media_video_no_processing_info_returns_immediately(tmp_path, monkeypatch):
    # tweepy's chunked upload may already have waited for async finalize —
    # a status without processing_info means "done", no further polling.
    monkeypatch.setattr(x_publisher, "MEDIA_ROOT", tmp_path)
    video = _touch(tmp_path, "bench-1/grid.mp4")

    mock_api = MagicMock()
    media = MagicMock()
    media.media_id = 4242
    mock_api.media_upload.return_value = media
    mock_api.get_media_upload_status.return_value = _processing_status(None)
    mock_client = _mock_tweet_client()

    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        with patch(
            "app.services.x_publisher.get_secret_plaintext_by_key",
            new=AsyncMock(side_effect=_fake_secret_lookup),
        ), patch("tweepy.Client", return_value=mock_client), patch(
            "tweepy.OAuth1UserHandler", return_value=MagicMock()
        ), patch("tweepy.API", return_value=mock_api):
            result = await x_publisher.post_media(session, "Grid video", [str(video)])

    assert result["ok"] is True
    assert mock_api.get_media_upload_status.call_count == 1
