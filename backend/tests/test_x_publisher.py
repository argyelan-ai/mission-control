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
