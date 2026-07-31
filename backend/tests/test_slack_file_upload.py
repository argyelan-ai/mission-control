"""The two-stage Slack upload — MC's first non-JSON Slack call.

files.upload (classic) is deprecated; the modern flow is
getUploadURLExternal -> raw byte POST -> completeUploadExternal. The byte hop
talks to a presigned URL with no Slack auth, so the transport must keep the
three hops straight and never leak the token into the wrong request. All
network is faked at the httpx layer; no test talks to Slack.
"""
import json

import httpx
import pytest

from app.services import slack_client
from app.services.slack_client import upload_file


class _FakeSlack:
    """Answers the three hops; records what each one received."""

    def __init__(self, *, url_ok=True, complete_ok=True, byte_status=200):
        self.calls: list[tuple[str, dict]] = []
        self.url_ok = url_ok
        self.complete_ok = complete_ok
        self.byte_status = byte_status

    async def handler(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "getUploadURLExternal" in url:
            self.calls.append(("geturl", dict(request.url.params)))
            if not self.url_ok:
                return httpx.Response(200, json={"ok": False, "error": "file_uploads_disabled"})
            return httpx.Response(200, json={
                "ok": True, "upload_url": "https://upload.test/hop", "file_id": "F0NEW",
            })
        if "upload.test" in url:
            self.calls.append(("bytes", {"auth": request.headers.get("authorization", "")}))
            return httpx.Response(self.byte_status)
        if "completeUploadExternal" in url:
            self.calls.append(("complete", json.loads(request.content)))
            if not self.complete_ok:
                return httpx.Response(200, json={"ok": False, "error": "not_in_channel"})
            return httpx.Response(200, json={"ok": True})
        raise AssertionError(f"unexpected call: {url}")


@pytest.fixture
def report_file(tmp_path):
    f = tmp_path / "report.pdf"
    f.write_bytes(b"%PDF-1.4 fake bytes")
    return str(f)


def _patch_transport(monkeypatch, fake):
    real_client = httpx.AsyncClient

    def _client(**kw):
        kw["transport"] = httpx.MockTransport(fake.handler)
        return real_client(**kw)

    monkeypatch.setattr(slack_client.httpx, "AsyncClient", _client)
    monkeypatch.setattr(
        slack_client, "get_bot_token",
        _async_return("xoxb-test"),
    )


def _async_return(value):
    async def _inner(*a, **kw):
        return value
    return _inner


@pytest.mark.asyncio
async def test_the_three_hops_run_in_order_and_the_byte_hop_carries_no_token(
    monkeypatch, report_file
):
    fake = _FakeSlack()
    _patch_transport(monkeypatch, fake)

    result = await upload_file(
        channel="C0REPORTS", path=report_file, initial_comment="der Bericht",
    )

    assert result.ok is True
    assert result.file_id == "F0NEW"
    assert [c[0] for c in fake.calls] == ["geturl", "bytes", "complete"]
    # The presigned URL must NOT receive the bot token.
    assert fake.calls[1][1]["auth"] == ""
    complete = fake.calls[2][1]
    assert complete["channel_id"] == "C0REPORTS"
    assert complete["initial_comment"] == "der Bericht"
    assert complete["files"][0]["id"] == "F0NEW"


@pytest.mark.asyncio
async def test_a_workspace_with_uploads_disabled_says_so(monkeypatch, report_file):
    fake = _FakeSlack(url_ok=False)
    _patch_transport(monkeypatch, fake)

    result = await upload_file(channel="C0REPORTS", path=report_file)

    assert result.ok is False
    assert result.code == "file_uploads_disabled"
    assert "workspace admin" in (result.error or "")
    # Stage 1 failed -> no bytes were sent anywhere.
    assert [c[0] for c in fake.calls] == ["geturl"]


@pytest.mark.asyncio
async def test_a_failed_byte_hop_never_reaches_complete(monkeypatch, report_file):
    fake = _FakeSlack(byte_status=500)
    _patch_transport(monkeypatch, fake)

    result = await upload_file(channel="C0REPORTS", path=report_file)

    assert result.ok is False
    assert result.code == "upload_hop"
    assert [c[0] for c in fake.calls] == ["geturl", "bytes"]


@pytest.mark.asyncio
async def test_a_missing_local_file_fails_before_any_network(monkeypatch):
    fake = _FakeSlack()
    _patch_transport(monkeypatch, fake)

    result = await upload_file(channel="C0REPORTS", path="/does/not/exist.pdf")

    assert result.ok is False
    assert result.code == "local_file"
    assert fake.calls == []


@pytest.mark.asyncio
async def test_the_ram_cap_refuses_oversized_files_before_any_network(
    monkeypatch, tmp_path
):
    """The byte hop buffers in RAM; a 1 GB deliverable next to Postgres in a
    5 GB VM is an outage, not an upload. Refused locally, with advice."""
    import os as _os

    big = tmp_path / "huge.zip"
    big.write_bytes(b"x")
    real_getsize = _os.path.getsize
    monkeypatch.setattr(
        slack_client.os.path, "getsize",
        lambda p: 101 * 1024 * 1024 if str(p).endswith("huge.zip") else real_getsize(p),
    )
    fake = _FakeSlack()
    _patch_transport(monkeypatch, fake)

    result = await upload_file(channel="C0REPORTS", path=str(big))

    assert result.ok is False
    assert result.code == "file_too_large"
    assert fake.calls == []
