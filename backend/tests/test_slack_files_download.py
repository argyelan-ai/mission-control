"""The shared Slack download — streamed, capped, and honest about scopes.

The voice-era download buffered the whole response and checked the size
afterwards; the shared version refuses BEFORE buffering (event size, then
Content-Length) and aborts mid-stream when the headers lied. These tests pin
that order — the cap must hold even against a server that declares nothing.
All network is faked at the httpx layer.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services.slack_files import declared_file_size, download_slack_file

_URL = "https://files.slack.com/files-pri/T0-F0/download/x.bin"


def _file(**over):
    file = {"id": "F0", "url_private_download": _URL}
    file.update(over)
    return file


def _patch_http(monkeypatch, handler):
    real_client = httpx.AsyncClient

    def _client(**kw):
        kw["transport"] = httpx.MockTransport(handler)
        return real_client(**kw)

    monkeypatch.setattr(httpx, "AsyncClient", _client)


def _patch_token():
    return patch(
        "app.services.slack_client.get_bot_token", new_callable=AsyncMock,
        return_value="xoxb-test",
    )


@pytest.mark.asyncio
async def test_bytes_come_home(monkeypatch):
    _patch_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"abc", headers={"content-type": "application/pdf"},
    ))
    with _patch_token():
        assert await download_slack_file(_file(), max_bytes=100) == b"abc"


@pytest.mark.asyncio
async def test_declared_event_size_refuses_without_any_http(monkeypatch):
    def _handler(req):
        raise AssertionError("no HTTP call may happen for a declared-oversize file")

    _patch_http(monkeypatch, _handler)
    with _patch_token():
        assert await download_slack_file(_file(size=101), max_bytes=100) is None


@pytest.mark.asyncio
async def test_content_length_header_refuses_before_buffering(monkeypatch):
    _patch_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"x" * 200,
        headers={"content-type": "application/pdf", "content-length": "200"},
    ))
    with _patch_token():
        assert await download_slack_file(_file(), max_bytes=100) is None


@pytest.mark.asyncio
async def test_a_lying_stream_is_aborted_at_the_cap(monkeypatch):
    """No Content-Length, body larger than the cap: the running counter must
    stop the read — headers are a claim, the cap is the law. The body is an
    async iterator on purpose: bytes-content would make httpx set
    Content-Length itself and the header check would win — this test wants
    the stream path."""

    async def _chunks():
        yield b"x" * 80
        yield b"x" * 80

    _patch_http(monkeypatch, lambda req: httpx.Response(
        200, content=_chunks(), headers={"content-type": "application/pdf"},
    ))
    with _patch_token():
        assert await download_slack_file(_file(), max_bytes=100) is None


@pytest.mark.asyncio
async def test_html_answer_means_missing_scope_not_a_file(monkeypatch):
    _patch_http(monkeypatch, lambda req: httpx.Response(
        200, content=b"<html>login</html>",
        headers={"content-type": "text/html; charset=utf-8"},
    ))
    with _patch_token():
        assert await download_slack_file(_file(), max_bytes=100) is None


def test_declared_file_size_reads_the_event():
    assert declared_file_size(_file(size=42)) == 42
    assert declared_file_size(_file()) is None
    assert declared_file_size(_file(size="42")) is None
