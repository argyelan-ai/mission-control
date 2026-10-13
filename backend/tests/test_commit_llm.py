"""commit_llm — the commit sheet's LLM-suggested message (local model only).

Covers the module in isolation: redaction (sabotage probe — no secret, no
home path survives into the prompt), sanitisation of the model's raw
answer (junk never becomes a commit message), and ``suggest_commit_message``
against a fake Spark client — success, timeout, an unreachable box, and a
junk answer all resolve to ``None`` (the router's cue to fall back), never
raise. Router-level wiring (draft endpoint picks it up, falls back) is in
``test_chat_actions.py``."""
from __future__ import annotations

import asyncio

import pytest

from app.services import commit_llm
from app.services.spark_client import SparkUnreachableError


class _FakeSpark:
    """A ``SparkClient`` stand-in: records the call, answers (or raises/
    stalls) exactly as the test wants — no network, no asyncio.sleep in
    production code paths."""

    def __init__(self, answer: str | None = None, *, raises: Exception | None = None,
                 delay: float | None = None):
        self.answer = answer
        self.raises = raises
        self.delay = delay
        self.calls: list[dict] = []

    async def complete(self, *, prompt: str, system: str | None = None,
                       max_tokens: int = 256, temperature: float = 0.2, model=None) -> str:
        self.calls.append({"prompt": prompt, "system": system})
        if self.delay is not None:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.answer or ""


FILES = [{"path": "app/prices.py", "status": "modified", "additions": 4, "deletions": 2}]


# ══════════════════════════════════════════════════════════════════════════
# redact — sabotage probe: no secret, no home path survives
# ══════════════════════════════════════════════════════════════════════════


# Built at runtime so no key-shaped literal sits in the public repo.
_FAKE_AWS = "AKIA" + "ABCDEFGHIJKLMNOP"


@pytest.mark.parametrize("secret_line", [
    "api_key = sk-abcdefghijklmnopqrstuvwxyz0123456789",
    "API_KEY: 'super-secret-value-123'",
    "Authorization: Bearer abcdefghijklmnop0123456789",
    "AWS_ACCESS_KEY_ID=" + _FAKE_AWS,
    "token: ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789",
    "-----BEGIN PRIVATE KEY-----\nMIIBogIBAAJ...\n-----END PRIVATE KEY-----",
])
def test_redact_strips_every_known_secret_shape(secret_line):
    out = commit_llm.redact(f"context line\n{secret_line}\nmore context")
    assert "sk-abcdefghijklmnopqrstuvwxyz0123456789" not in out
    assert "super-secret-value-123" not in out
    assert "abcdefghijklmnop0123456789" not in out
    assert _FAKE_AWS not in out
    assert "ghp_AbCdEfGhIjKlMnOpQrStUvWxYz0123456789" not in out
    assert "MIIBogIBAAJ" not in out
    assert "context line" in out and "more context" in out


def test_redact_anonymises_home_paths():
    out = commit_llm.redact("see /Users/" + "someone/secret-notes/plan.md and /home/" + "otheruser/.env")
    assert "someone" not in out and "otheruser" not in out
    assert "/Users/«redacted»/secret-notes/plan.md" in out
    assert "/home/«redacted»/.env" in out


def test_build_prompt_never_leaks_a_secret_from_diff_or_chat():
    """Sabotage probe at the integration seam: a secret sitting in the diff
    OR in a chat message must not reach the string actually sent to the
    model."""
    diff_text = "+API_KEY=sk-leaked00000000000000000000\n+print('hi')\n"
    user_messages = ["please commit this, token=ghp_leakedtoken000000000000"]
    prompt = commit_llm.build_prompt(
        branch="feat/x", files=FILES, diff_text=diff_text, user_messages=user_messages,
    )
    assert "sk-leaked00000000000000000000" not in prompt
    assert "ghp_leakedtoken000000000000" not in prompt
    assert "app/prices.py" in prompt  # the harmless parts still make it through


# ══════════════════════════════════════════════════════════════════════════
# _sanitize — junk never becomes a commit message
# ══════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("raw,expected_subject", [
    ("feat: add retry to the spark client", "feat: add retry to the spark client"),
    ("```\nfix: handle empty diff\n```", "fix: handle empty diff"),
    ('"chore(chat): wire the commit sheet"', "chore(chat): wire the commit sheet"),
    ("\n\nfix: trim leading blank lines\n", "fix: trim leading blank lines"),
])
def test_sanitize_accepts_well_formed_answers(raw, expected_subject):
    assert commit_llm._sanitize(raw).splitlines()[0] == expected_subject


def test_sanitize_keeps_a_short_bullet_body():
    raw = "feat: add commit suggestions\n\n- calls the local model\n- falls back on timeout\n- a third\n- a fourth"
    out = commit_llm._sanitize(raw)
    lines = out.splitlines()
    assert lines[0] == "feat: add commit suggestions"
    bullets = [l for l in lines if l.startswith("-")]
    assert len(bullets) == commit_llm.MAX_BODY_BULLETS


@pytest.mark.parametrize("raw", [
    "",
    "   \n  \n",
    "I updated prices.py to fix the rounding bug.",  # no conventional-commit shape
    "FEAT: uppercase type is not accepted",
    "feat:no space after colon",
    "feat: " + "x" * 80,  # over MAX_SUBJECT_CHARS
    None,
])
def test_sanitize_rejects_junk(raw):
    if raw is None:
        assert commit_llm._sanitize("") is None
    else:
        assert commit_llm._sanitize(raw) is None


# ══════════════════════════════════════════════════════════════════════════
# extract_user_messages
# ══════════════════════════════════════════════════════════════════════════


def test_extract_user_messages_keeps_last_n_user_turns_in_order():
    events = [
        {"kind": "message", "role": "user", "text": "first"},
        {"kind": "message", "role": "assistant", "text": "ignored"},
        {"kind": "tool", "role": "user", "text": "not a message"},
        {"kind": "message", "role": "user", "text": "second"},
        {"kind": "message", "role": "user", "text": "third"},
        {"kind": "message", "role": "user", "text": "fourth"},
    ]
    assert commit_llm.extract_user_messages(events) == ["second", "third", "fourth"]


def test_extract_user_messages_never_raises_on_odd_input():
    assert commit_llm.extract_user_messages(None) == []
    assert commit_llm.extract_user_messages([None, 1, "nope", {"kind": "message"}]) == []


# ══════════════════════════════════════════════════════════════════════════
# suggest_commit_message — success, timeout, unreachable, junk -> fallback
# ══════════════════════════════════════════════════════════════════════════


async def test_suggest_commit_message_success():
    fake = _FakeSpark(answer="feat(prices): round to two decimals\n\n- app/prices.py")
    out = await commit_llm.suggest_commit_message(
        branch="feat/x", files=FILES, diff_text="+x", user_messages=["round the prices please"], spark=fake,
    )
    assert out.splitlines()[0] == "feat(prices): round to two decimals"
    assert fake.calls and "app/prices.py" in fake.calls[0]["prompt"]
    assert "round the prices please" in fake.calls[0]["prompt"]


async def test_suggest_commit_message_timeout_falls_back(monkeypatch):
    monkeypatch.setattr(commit_llm, "LLM_TIMEOUT_SECONDS", 0.02)
    fake = _FakeSpark(answer="feat: too slow", delay=1.0)
    out = await commit_llm.suggest_commit_message(
        branch="feat/x", files=FILES, diff_text="+x", user_messages=[], spark=fake,
    )
    assert out is None


async def test_suggest_commit_message_unreachable_falls_back():
    fake = _FakeSpark(raises=SparkUnreachableError("no box"))
    out = await commit_llm.suggest_commit_message(
        branch="feat/x", files=FILES, diff_text="+x", user_messages=[], spark=fake,
    )
    assert out is None


async def test_suggest_commit_message_junk_falls_back():
    fake = _FakeSpark(answer="I'm not sure what changed here, sorry!")
    out = await commit_llm.suggest_commit_message(
        branch="feat/x", files=FILES, diff_text="+x", user_messages=[], spark=fake,
    )
    assert out is None


async def test_suggest_commit_message_skips_the_call_with_no_files():
    fake = _FakeSpark(answer="feat: should never be asked")
    out = await commit_llm.suggest_commit_message(
        branch="feat/x", files=[], diff_text="", user_messages=[], spark=fake,
    )
    assert out is None
    assert fake.calls == []
