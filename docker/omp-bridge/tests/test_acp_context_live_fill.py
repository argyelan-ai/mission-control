#!/usr/bin/env python3
"""The ACP task path reports the context fill WHILE a turn runs.

Live finding 30.10.2026: the omp agent worked one bench task for over half an
hour — one ACP prompt turn, dozens of model calls. Its agent page said 0 %
the whole time while omp's own session file showed ~150k of a 393k window
(38 %). Root cause: omp 18.10.1 sends ``usage_update`` only at
``agent_end`` (once per prompt turn — verified in the shipped binary,
``AcpAgent`` calls its usage emitter only in the ``agent_end`` branch). The
bridge resets its context holder to 0.0 at every pickup and waited for that
one event, so for the whole turn the heartbeat reported the reset value.

Fix: on every tool event (each one follows a finished model call) the bridge
reads the prompt size of the session's LAST model call from omp's own
session file and stamps ``used / window`` into the same holder — throttled,
best-effort. The window is the last ``usage_update`` size of this run, else
the one MC rendered into omp's ``models.yml``. The real ``usage_update`` at
turn end still has the last word.

Run:  python3 test_acp_context_live_fill.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(HERE))

import acp_chat_events  # noqa: E402
import bridge  # noqa: E402
import test_acp_adapter as taa  # noqa: E402

#: Session id of rpc/acp-permission-tool.ndjson (one tool_call, three
#: tool_call_updates, then a usage_update size=500000 used=17462 at the end).
PERM_SID = "01a0814f-b9b6-7387-8de4-4e9509085239"
WINDOW = 393_216


def _assistant_line(usage: dict, snapshot: dict | None = None) -> str:
    msg: dict = {"role": "assistant", "content": [{"type": "text", "text": "x"}], "usage": usage}
    if snapshot is not None:
        msg["contextSnapshot"] = snapshot
    return json.dumps({"type": "message", "id": "a1", "message": msg})


def _agent_dir(tmp: Path, *, window: int | None = WINDOW) -> Path:
    agent_dir = tmp / "agent"
    (agent_dir / "sessions").mkdir(parents=True)
    if window is not None:
        (agent_dir / "models.yml").write_text(
            "providers:\n  mc-openai:\n    models:\n      - id: m\n"
            f"        contextWindow: {window}\n        input: [text]\n",
            encoding="utf-8",
        )
    return agent_dir


def _write_omp_session(agent_dir: Path, cwd: str, sid: str, lines: list[str]) -> Path:
    folder = acp_chat_events.session_dir(agent_dir_env=str(agent_dir), cwd=cwd)
    assert folder is not None
    path = folder / f"2026-10-30T08-02-21-810Z_{sid}.jsonl"
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _fixture_without_usage_update(tmp: Path) -> Path:
    src = taa.FIXTURES["permission"].read_text(encoding="utf-8").splitlines()
    kept = [ln for ln in src if '"usage_update"' not in ln]
    assert len(kept) < len(src), "fixture lost its usage_update — re-check the probe"
    out = tmp / "acp-permission-no-usage.ndjson"
    out.write_text("\n".join(kept) + "\n", encoding="utf-8")
    return out


class _Env:
    def __init__(self, **values: str | None) -> None:
        self.values = values
        self.saved: dict = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _isolated_env(agent_dir: Path) -> _Env:
    # session_dir()/configured_context_window() resolve the profile tree
    # first; tests point them at the temp agent dir only.
    return _Env(OMP_PROFILE=None, OMP_HOME=None, PI_CODING_AGENT_DIR=str(agent_dir))


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def test_prompt_tokens_uses_omps_anchor_order():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.jsonl"
        # snapshot wins (minus rewritten history) over usage numbers
        f.write_text(_assistant_line(
            {"input": 1, "cacheRead": 2, "cacheWrite": 0, "output": 9, "totalTokens": 12},
            {"promptTokens": 151_089, "historyRewriteTokensRemoved": 89},
        ) + "\n", encoding="utf-8")
        assert acp_chat_events.omp_prompt_tokens(f) == 151_000
        # no snapshot: contextTokens
        f.write_text(_assistant_line({"input": 1, "cacheRead": 2, "contextTokens": 5000}) + "\n")
        assert acp_chat_events.omp_prompt_tokens(f) == 5000
        # neither: input + cacheRead + cacheWrite (output is not prompt)
        f.write_text(_assistant_line(
            {"input": 1629, "cacheRead": 147_456, "cacheWrite": 0, "output": 221}) + "\n")
        assert acp_chat_events.omp_prompt_tokens(f) == 149_085


def test_prompt_tokens_takes_the_last_call_and_skips_garbage():
    with tempfile.TemporaryDirectory() as d:
        f = Path(d) / "s.jsonl"
        f.write_text("\n".join([
            _assistant_line({"input": 10, "cacheRead": 0, "cacheWrite": 0}),
            _assistant_line({"input": 20_000, "cacheRead": 0, "cacheWrite": 0}),
            json.dumps({"type": "message", "message": {"role": "toolResult", "content": []}}),
            _assistant_line({"input": 0, "cacheRead": 0, "cacheWrite": 0, "output": 0}),
            '{"type":"message","message":{"role":"assistant","usage":',  # being written
        ]) + "\n", encoding="utf-8")
        assert acp_chat_events.omp_prompt_tokens(f) == 20_000
        assert acp_chat_events.omp_prompt_tokens(Path(d) / "missing.jsonl") is None
        assert acp_chat_events.omp_prompt_tokens(None) is None


def test_own_session_file_never_returns_mcs_copy():
    with tempfile.TemporaryDirectory() as d:
        folder = Path(d)
        own = folder / f"2026-10-30T08-02-21-810Z_{PERM_SID}.jsonl"
        own.write_text(_assistant_line({"input": 5}) + "\n")
        mc_old = folder / f"2026-10-30T08-02-22_{PERM_SID}.jsonl"
        mc_old.write_text(json.dumps({"type": "session", "id": PERM_SID, "bridge": "acp"}) + "\n")
        os.utime(mc_old, (own.stat().st_mtime + 10,) * 2)
        assert acp_chat_events.omp_own_session_file(folder, PERM_SID) == own
        assert acp_chat_events.omp_own_session_file(folder, "other") is None
        assert acp_chat_events.omp_own_session_file(None, PERM_SID) is None


def test_configured_window_reads_models_yml():
    with tempfile.TemporaryDirectory() as d:
        agent_dir = _agent_dir(Path(d))
        assert acp_chat_events.configured_context_window(str(agent_dir)) == WINDOW
        (agent_dir / "models.yml").write_text("providers: {}\n")
        assert acp_chat_events.configured_context_window(str(agent_dir)) is None


# --------------------------------------------------------------------------
# the run: a figure while the turn runs
# --------------------------------------------------------------------------

def test_tool_events_stamp_the_live_fill_without_any_usage_update():
    """The red case of 30.10.2026: no usage_update during the turn. The
    holder must still leave the 0.0 reset — read from omp's own file."""
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        agent_dir = _agent_dir(tmp)
        _write_omp_session(agent_dir, str(HERE), PERM_SID, [
            _assistant_line({"input": 1629, "cacheRead": 147_456, "cacheWrite": 0, "output": 221}),
        ])
        fixture = _fixture_without_usage_update(tmp)
        holder = bridge.ACPContextPct(0.0)  # serve_loop's per-pickup reset
        with _isolated_env(agent_dir):
            taa.run_adapter(fixture, context_pct=holder)
        assert holder.get() == round(149_085 / WINDOW * 100, 1) == 37.9
        payload = bridge._build_heartbeat_payload("working", None, holder.get)
        assert payload["context_pct"] == 37.9


def test_turn_end_usage_update_still_has_the_last_word():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        agent_dir = _agent_dir(tmp)
        _write_omp_session(agent_dir, str(HERE), PERM_SID, [
            _assistant_line({"input": 300_000, "cacheRead": 0, "cacheWrite": 0}),
        ])
        holder = bridge.ACPContextPct(0.0)
        with _isolated_env(agent_dir):
            taa.run_adapter(taa.FIXTURES["permission"], context_pct=holder)
        # acp-permission-tool.ndjson: usage_update size=500000 used=17462
        assert holder.get() == round(17_462 / 500_000 * 100, 1)


def test_no_session_file_or_no_window_keeps_the_reset_value():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fixture = _fixture_without_usage_update(tmp)
        # no omp file at all
        agent_dir = _agent_dir(tmp)
        holder = bridge.ACPContextPct(0.0)
        with _isolated_env(agent_dir):
            taa.run_adapter(fixture, context_pct=holder)
        assert holder.get() == 0.0
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        fixture = _fixture_without_usage_update(tmp)
        # a file, but no window anywhere -> no invented percent
        agent_dir = _agent_dir(tmp, window=None)
        _write_omp_session(agent_dir, str(HERE), PERM_SID, [_assistant_line({"input": 9000})])
        holder = bridge.ACPContextPct(0.0)
        with _isolated_env(agent_dir):
            taa.run_adapter(fixture, context_pct=holder)
        assert holder.get() == 0.0


def test_fill_above_the_window_is_capped_at_the_receivers_bound():
    with tempfile.TemporaryDirectory() as d:
        tmp = Path(d)
        agent_dir = _agent_dir(tmp, window=1000)
        _write_omp_session(agent_dir, str(HERE), PERM_SID, [_assistant_line({"input": 5000})])
        fixture = _fixture_without_usage_update(tmp)
        holder = bridge.ACPContextPct(0.0)
        with _isolated_env(agent_dir):
            taa.run_adapter(fixture, context_pct=holder)
        assert holder.get() == 100.0  # backend Field(ge=0, le=100)


if __name__ == "__main__":
    failures = 0
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print(f"PASS {name}")
            except Exception as e:  # noqa: BLE001
                failures += 1
                print(f"FAIL {name}: {e!r}")
    sys.exit(1 if failures else 0)
