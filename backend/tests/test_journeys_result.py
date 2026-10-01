"""Home line for the nightly journey tests (docs/journeys.md): the backend
reads the runner's ``~/.mc/journeys/last.json`` and hands Home a small,
path-free summary."""
import json

import pytest

from app.services import journeys_result


@pytest.fixture
def mc_home(tmp_path, monkeypatch):
    monkeypatch.setattr(journeys_result, "mc_home", lambda: tmp_path)
    (tmp_path / "journeys").mkdir()
    return tmp_path


def write(mc_home, data) -> None:
    (mc_home / "journeys" / "last.json").write_text(json.dumps(data))


GREEN = {
    "status": "green",
    "summary": "all journeys passed",
    "run": "20260930-051500",
    "commit": "abc1234",
    "finished_at": "2026-09-30T05:16:10+0200",
    "report_dir": "/home/someone/.mc/journeys/runs/20260930-051500",
    "journeys": [
        {"file": "J-phone-needs-you.spec.ts", "title": "unblock", "status": "passed"},
        {"file": "J-phone-needs-you.spec.ts", "title": "answer [known gap: x]", "status": "passed"},
        {"file": "J-usage.spec.ts", "title": "usage", "status": "passed"},
    ],
    "counts": {"passed": 3},
    "known_gaps": ["answer [known gap: x]"],
}


def test_no_file_means_no_result(mc_home):
    assert journeys_result.load() is None


def test_unreadable_file_means_no_result(mc_home):
    (mc_home / "journeys" / "last.json").write_text("{not json")
    assert journeys_result.load() is None


def test_green_summary(mc_home):
    write(mc_home, GREEN)
    out = journeys_result.load()
    assert out == {
        "status": "green",
        "finished_at": "2026-09-30T03:16:10+00:00",
        "commit": "abc1234",
        "total": 3,
        "passed": 3,
        "failed": [],
        "known_gaps": 1,
        "reason": None,
    }


def test_red_names_the_failed_journeys_once(mc_home):
    data = dict(GREEN, status="red", summary="journeys failed — see /home/someone/.mc/journeys/runs/x/html")
    data["journeys"] = [
        {"file": "J-phone-needs-you.spec.ts", "title": "unblock", "status": "failed"},
        {"file": "J-phone-needs-you.spec.ts", "title": "answer", "status": "timedOut"},
        {"file": "J-usage.spec.ts", "title": "usage", "status": "passed"},
    ]
    write(mc_home, data)
    out = journeys_result.load()
    assert out["status"] == "red"
    assert out["failed"] == ["J-phone-needs-you"]
    assert out["passed"] == 1 and out["total"] == 3
    # never a host path in the answer
    assert "/home/" not in json.dumps(out)


def test_skipped_and_error_carry_a_short_reason_without_paths(mc_home):
    write(mc_home, {"status": "skipped", "summary": "head running (heartbeat in the last 3 min)",
                    "finished_at": "2026-09-30T05:15:02+0200", "journeys": []})
    out = journeys_result.load()
    assert out["status"] == "skipped"
    assert out["reason"] == "head running (heartbeat in the last 3 min)"
    assert out["total"] == 0

    write(mc_home, {"status": "error", "summary": "aborted (exit 128) — see run.log",
                    "finished_at": "2026-09-30T05:15:02+0200"})
    assert journeys_result.load()["reason"] == "aborted (exit 128)"


def test_unknown_status_is_ignored(mc_home):
    write(mc_home, dict(GREEN, status="purple"))
    assert journeys_result.load() is None


async def test_endpoint(auth_client, monkeypatch, tmp_path):
    monkeypatch.setattr(journeys_result, "mc_home", lambda: tmp_path)
    res = await auth_client.get("/api/v1/system/journeys")
    assert res.status_code == 200
    assert res.json() == {"result": None}
    (tmp_path / "journeys").mkdir()
    (tmp_path / "journeys" / "last.json").write_text(json.dumps(GREEN))
    res = await auth_client.get("/api/v1/system/journeys")
    assert res.json()["result"]["status"] == "green"


async def test_endpoint_needs_login(client):
    assert (await client.get("/api/v1/system/journeys")).status_code in (401, 403)
