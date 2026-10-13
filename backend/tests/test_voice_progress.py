"""Live voice — progress context for the voice (voice PR 3, bauplan B4).

While a call is open the browser watches the chat's own live stream and,
on a milestone, asks MC for ONE redacted context line
(``POST /api/v1/voice/live/progress``). The browser hands that line to the
realtime model as a system item. What these tests pin:

- privacy: secrets out, paths cut to their last segment, tool OUTPUT never
  accepted, the final reply shortened to 1–2 sentences without code;
- honesty: only a ``final`` line carries ``agent_final=true``; every other
  line says NOT done; a premature success gets a correction line;
- guards: admin only, a call opened for THIS agent, per-call rate limit,
  unknown kinds refused.
"""
from __future__ import annotations

import logging

import pytest

from app.config import settings
from app.services import voice_live_tools, voice_progress

FAKE_KEY = "sk-test-" + "LONGLIVED-0123456789abcdef"
URL = "/api/v1/voice/live/progress"
SECRET = "ghp_" + "A" * 36


@pytest.fixture(autouse=True)
def _clean_calls():
    voice_live_tools.reset_for_tests()
    yield
    voice_live_tools.reset_for_tests()


@pytest.fixture
def voice_on(monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", True)
    monkeypatch.setattr(settings, "openai_api_key", FAKE_KEY)


async def _call(make_agent):
    agent = await make_agent(name="Voice Target", slug="voice-prog", harness="omp")
    return agent, voice_live_tools.open_call(agent.id)


def _body(agent, ref, **kw):
    return {"call_ref": ref, "agent_id": str(agent.id), **kw}


# ── pure: building the line ──────────────────────────────────────────────


def test_tool_line_has_title_never_output_and_is_redacted():
    item = voice_progress.build_item(
        "tool_started",
        "Rex",
        tool={"name": "Bash", "title": f"cat /home/someone/client-x/.env TOKEN={SECRET}"},
    )
    text = item["text"]
    assert "Bash" in text and ".env" in text
    for leaked in (SECRET, "someone", "client-x"):
        assert leaked not in text, leaked
    assert item["agent_final"] is False
    assert "agent_final=false" in text


def test_numbers_with_slashes_are_not_mistaken_for_paths():
    assert voice_live_tools.redact_short("Tests 12/12 grün, Schritt 3/5") == "Tests 12/12 grün, Schritt 3/5"


def test_step_line():
    item = voice_progress.build_item("step", "Rex", step={"index": 2, "total": 5, "content": "Tests in /srv/x/test_a.py"})
    assert "2/5" in item["text"] and "test_a.py" in item["text"] and "/srv" not in item["text"]
    done = voice_progress.build_item("step", "Rex", step={"index": 5, "total": 5, "content": "", "all_done": True})
    assert "NOT the final report" in done["text"]


def test_only_final_is_final():
    for kind in voice_progress.PROGRESS_KINDS:
        item = voice_progress.build_item(kind, "Rex", text="Alles erledigt.")
        assert item["agent_final"] is (kind == "final"), kind
        flag = "agent_final=true" if kind == "final" else "agent_final=false"
        assert flag in item["text"], kind


def test_final_summary_is_short_without_code_and_redacted():
    reply = (
        "## Ergebnis\n"
        "Die Schleife lief 30 Sekunden und ist durch. Alle 3 Tests sind grün in /home/someone/repo/tests/test_loop.py. "
        "Danach habe ich noch aufgeräumt. Und noch ein vierter Satz.\n"
        "```bash\nexport OPENAI_API_KEY=sk-live-" + "9" * 30 + "\n```\n"
        f"Token: {SECRET}\n"
    )
    summary = voice_progress.summarize_reply(reply)
    assert summary.startswith("Die Schleife lief 30 Sekunden und ist durch.")
    assert "test_loop.py" in summary
    for leaked in ("someone", "sk-live", "export", "vierter", "##", "```", SECRET):
        assert leaked not in summary, leaked
    assert len(summary) <= voice_progress.FINAL_MAX_CHARS


def test_final_without_reply_text_says_so():
    item = voice_progress.build_item("final", "Rex", text="```\nonly code\n```")
    assert "without a text reply" in item["text"]
    assert item["agent_final"] is True


def test_approval_line_says_tap_never_spoken_yes():
    item = voice_progress.build_item("approval", "Rex", question=f"Allow rm -rf /home/someone/x? {SECRET}")
    assert "tap it in the chat" in item["text"]
    assert "spoken yes does not count" in item["text"]
    # voice PR 4: the voice says it and asks for the tap, in so many words
    assert "Bitte im Chat antippen." in item["text"]
    assert "never pass a yes or no on" in item["text"]
    assert SECRET not in item["text"] and "someone" not in item["text"]


def test_error_line_carries_only_a_code():
    item = voice_progress.build_item("error", "Rex", error_code="provider_error; cat /etc/passwd")
    assert "provider_error" in item["text"]
    assert "passwd" not in item["text"]


def test_premature_success_correction():
    item = voice_progress.build_item("premature_success", "Rex")
    assert "NOT" in item["text"] and "correct" in item["text"].lower()
    assert item["agent_final"] is False


def test_still_working_mentions_elapsed():
    item = voice_progress.build_item(
        "still_working", "Rex", tool={"name": "Bash", "title": "for i in 1..30; sleep 1"}, elapsed_s=95
    )
    assert "still working" in item["text"].lower()
    assert "1 min" in item["text"]


# ── endpoint ─────────────────────────────────────────────────────────────


async def test_progress_404_when_switch_off(auth_client, make_agent, monkeypatch):
    monkeypatch.setattr(settings, "voice_live_enabled", False)
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_body(agent, ref, kind="started"))
    assert r.status_code == 404


@pytest.mark.parametrize("role", ["viewer", "operator"])
async def test_progress_admin_only(client, voice_on, make_agent, role):
    import uuid

    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.auth import create_access_token
    from app.models.user import User
    from tests.conftest import test_engine

    uid = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(User(id=uid, email=f"{role}-prog@mc.local", name=role, role=role, is_active=True))
        await s.commit()
    client.headers["Authorization"] = f"Bearer {create_access_token(str(uid), role)}"
    agent, ref = await _call(make_agent)
    r = await client.post(URL, json=_body(agent, ref, kind="started"))
    assert r.status_code == 403


async def test_progress_needs_call_for_this_agent(auth_client, voice_on, make_agent):
    agent, _ref = await _call(make_agent)
    other = await make_agent(name="Other", slug="voice-other", harness="omp")
    ref_other = voice_live_tools.open_call(other.id)
    r = await auth_client.post(URL, json=_body(agent, ref_other, kind="started"))
    assert r.status_code == 403
    assert r.json()["detail"]["reason"] == "wrong_agent"
    r = await auth_client.post(URL, json=_body(agent, "nope", kind="started"))
    assert r.json()["detail"]["reason"] == "call_unknown"


async def test_progress_rejects_unknown_kind_and_tool_output(auth_client, voice_on, make_agent):
    agent, ref = await _call(make_agent)
    r = await auth_client.post(URL, json=_body(agent, ref, kind="dump_everything"))
    assert r.status_code == 422
    # A tool OUTPUT field is not part of the contract: refused, not ignored.
    r = await auth_client.post(
        URL, json=_body(agent, ref, kind="tool_finished", tool={"name": "Bash", "title": "ls", "result": "secret"})
    )
    assert r.status_code == 422


async def test_progress_returns_redacted_item(auth_client, voice_on, make_agent, caplog):
    agent, ref = await _call(make_agent)
    caplog.set_level(logging.INFO, logger="mc.voice_live")
    r = await auth_client.post(
        URL,
        json=_body(agent, ref, kind="final", text=f"Fertig. Der Schlüssel {SECRET} liegt in /home/someone/k.txt."),
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["agent_final"] is True
    assert SECRET not in body["text"] and "someone" not in body["text"]
    assert "Voice Target" in body["text"]
    # Logs carry the kind, never the words.
    assert "Schlüssel" not in caplog.text and SECRET not in caplog.text
    assert "kind=final" in caplog.text


async def test_progress_rate_limited_per_call(auth_client, voice_on, make_agent):
    agent, ref = await _call(make_agent)
    limit = voice_live_tools.RATE_LIMITS["progress"]
    for _ in range(limit):
        assert (await auth_client.post(URL, json=_body(agent, ref, kind="still_working"))).status_code == 200
    r = await auth_client.post(URL, json=_body(agent, ref, kind="still_working"))
    assert r.status_code == 429


async def test_premature_success_is_counted(auth_client, voice_on, make_agent, caplog):
    agent, ref = await _call(make_agent)
    caplog.set_level(logging.INFO, logger="mc.voice_live")
    before = voice_progress.premature_success_count()
    r = await auth_client.post(URL, json=_body(agent, ref, kind="premature_success"))
    assert r.status_code == 200
    assert voice_progress.premature_success_count() == before + 1
    assert "voice_premature_success" in caplog.text
