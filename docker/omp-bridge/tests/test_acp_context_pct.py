#!/usr/bin/env python3
"""G5 (path-parity audit #521) — the ACP path reports context%.

Claude-path TEMPLATE (verbatim): docker/shared/poll.sh heartbeat() builds
`payload['context_pct'] = float(val)` (0-100, float) and POSTs it to
/api/v1/agent/me/heartbeat — the backend receiver is
backend/app/routers/agents.py AgentHeartbeat.context_pct
(`context_pct: float | None = Field(default=None, ge=0, le=100)`).

The ACP path has no TUI pane, so the capture_pane scrape yields nothing there.
Fix: run_acp_once stamps omp's `usage_update` (size = window, used = tokens in
use) into a module holder; _build_heartbeat_payload reports it as the SAME
context_pct field on the SAME heartbeat when the scrape produced nothing
(fallback — a scrape hit still wins, native/Claude behaviour unchanged).
No second field, no second endpoint, no frontend change.

Sabotage probes (both directions, as real mutations of bridge.py run in a
subprocess): killing the usage_update stamp OR the payload fallback each
removes the report — the mutated runs must come out WITHOUT context_pct
while the unmutated control produces it.

Run:  python3 test_acp_context_pct.py   (standalone)   OR   pytest -v
"""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)  # docker/omp-bridge
REPO_ROOT = os.path.dirname(os.path.dirname(ROOT))  # repo root

sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

import bridge  # noqa: E402
import test_acp_adapter as taa  # noqa: E402  (fixture paths + in-process fake)

POLL_SH = os.path.join(REPO_ROOT, "docker", "shared", "poll.sh")
AGENTS_ROUTER = os.path.join(REPO_ROOT, "backend", "app", "routers", "agents.py")

# acp-normal-turn.ndjson: usage_update size=500000 used=17395 -> 3.5 %
EXPECTED_PCT = 3.5


def _payload_with_acp_holder(capture_pane=None) -> dict:
    """Build a heartbeat payload the way the heartbeater does (capture_pane
    None on the ACP path — the scrape reads an empty/foreign pane there)."""
    return bridge._build_heartbeat_payload("working", capture_pane)


def test_acp_message_sequence_reports_context_pct():
    """An ACP message sequence (normal fixture incl. usage_update) produces a
    context% value AT THE RECEIVER FORMAT: the heartbeat payload the bridge
    POSTs to /api/v1/agent/me/heartbeat carries `context_pct` as a float in
    0-100 — same field name, same type, same bounds as poll.sh's report."""
    bridge._set_acp_context_pct(None)
    try:
        outcome, _ = taa.run_adapter(taa.FIXTURES["normal"])
        assert outcome.final_stop_reason == "end_turn"
        payload = _payload_with_acp_holder()
        assert "context_pct" in payload, (
            "ACP sequence with usage_update must yield context_pct in the "
            "heartbeat payload"
        )
        assert payload["context_pct"] == EXPECTED_PCT
        assert isinstance(payload["context_pct"], float)
        assert 0.0 <= payload["context_pct"] <= 100.0
    finally:
        bridge._set_acp_context_pct(None)
    print("PASS test_acp_message_sequence_reports_context_pct")


def test_sender_format_matches_poll_sh_template_and_receiver_field():
    """No second format: the field the ACP path fills is byte-identical to
    (a) the poll.sh template line and (b) the backend receiver field."""
    poll = open(POLL_SH, encoding="utf-8").read()
    assert "payload['context_pct'] = float(val)" in poll, (
        "poll.sh template changed — re-audit the shared report format"
    )
    agents = open(AGENTS_ROUTER, encoding="utf-8").read()
    assert "context_pct: float | None = Field(default=None, ge=0, le=100)" in agents, (
        "AgentHeartbeat.context_pct field changed — re-audit bounds/name"
    )
    print("PASS test_sender_format_matches_poll_sh_template_and_receiver_field")


def test_no_usage_update_means_no_reported_value():
    """Direction 1 of the sabotage probe, event-level: an ACP sequence WITHOUT
    usage_update (acp-silent-cancel) never invents a value — the holder stays
    empty and the payload carries no context_pct."""
    bridge._set_acp_context_pct(None)
    try:
        outcome, _ = taa.run_adapter(taa.FIXTURES["silent"])
        assert outcome.final_stop_reason == "cancelled"
        payload = _payload_with_acp_holder()
        assert "context_pct" not in payload, (
            "no usage_update in the stream -> no context_pct may be reported"
        )
    finally:
        bridge._set_acp_context_pct(None)
    print("PASS test_no_usage_update_means_no_reported_value")


def test_scrape_hit_still_wins_on_the_native_path():
    """Native/Claude behaviour byte-identical: when capture_pane scrape yields
    a value it wins and the ACP holder is not consulted."""
    bridge._set_acp_context_pct(EXPECTED_PCT)
    try:
        payload = _payload_with_acp_holder(lambda: "ctx: 8")
        assert payload == {"status": "working", "context_pct": 8.0}
    finally:
        bridge._set_acp_context_pct(None)
    print("PASS test_scrape_hit_still_wins_on_the_native_path")


def _run_sabotage(mut_from: str, mut_to: str) -> None:
    """Copy the bridge tree to a temp dir, mutate bridge.py in the COPY, and
    replay the ACP message sequence in a SUBPROCESS against the mutated copy:
    it must come out WITHOUT a context_pct report (probe bites). The real
    tree and the live pytest process stay untouched (an in-process module
    swap poisons later tests — seen live: 49 false failures after exec)."""
    import shutil

    tmp = tempfile.mkdtemp(prefix="g5-sabotage-")
    broot = os.path.join(tmp, "bridge-root")
    shutil.copytree(ROOT, broot)
    btests = os.path.join(broot, "tests")
    target = os.path.join(broot, "bridge.py")
    with open(target, encoding="utf-8") as fh:
        src = fh.read()
    assert mut_from in src, "sabotage anchor not found in bridge.py"
    mutated = src.replace(mut_from, mut_to, 1)
    assert mutated != src, "sabotage mutation is a no-op"
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(mutated)
    script = (
        "import json, sys\n"
        "broot, btests = sys.argv[1], sys.argv[2]\n"
        "sys.path[:0] = [btests, broot]\n"
        "import test_acp_adapter as taa\n"
        "import bridge\n"
        "outcome, _ = taa.run_adapter(taa.FIXTURES['normal'])\n"
        "assert outcome.final_stop_reason == 'end_turn', "
        "'sabotage must not break the run itself'\n"
        "payload = bridge._build_heartbeat_payload('working', None)\n"
        "print(json.dumps({'context_pct_in_payload': "
        "'context_pct' in payload}))\n"
        "assert 'context_pct' not in payload, (\n"
        "    'sabotage probe did not bite: context_pct still reported after "
        "removal')\n"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    res = subprocess.run(
        [sys.executable, "-c", script, broot, btests],
        capture_output=True, text=True, env=env, timeout=180,
    )
    assert res.returncode == 0, (
        f"sabotage probe did not behave as expected:\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr}"
    )
    out = json.loads(res.stdout.strip().splitlines()[-1])
    assert out["context_pct_in_payload"] is False


def test_sabotage_usage_update_stamp_removed_makes_report_vanish():
    """Direction 2a — remove the new stamping branch: the sequence runs, but
    no context_pct is reported (the probe bites, i.e. the test suite's green
    path above really depends on the new code)."""
    _run_sabotage('elif su == "usage_update":', 'elif False:  # sabotage G5a')
    print("PASS test_sabotage_usage_update_stamp_removed_makes_report_vanish")


def test_sabotage_payload_fallback_removed_makes_report_vanish():
    """Direction 2b — remove the new payload fallback: same expectation."""
    _run_sabotage(
        'if "context_pct" not in payload:',
        'if False:  # sabotage G5b',
    )
    print("PASS test_sabotage_payload_fallback_removed_makes_report_vanish")


def test_bridge_wiring_pinned_positive():
    """Positive wiring pins (replacement for the former open-gap tripwire):
    serve_loop still owns exactly ONE heartbeater with the native capture_pane
    (scrape keeps priority), and _build_heartbeat_payload consults the ACP
    holder that run_acp_once stamps from usage_update."""
    serve_src = inspect.getsource(bridge.serve_loop)
    tree = ast.parse(serve_src)
    calls = [
        n for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) or getattr(n.func, "attr", None))
        == "start_heartbeater"
    ]
    assert len(calls) == 1, "exactly one heartbeater expected in serve_loop"
    kw = {k.arg: k.value for k in calls[0].keywords if k.arg}
    assert kw.get("_capture_pane") is not None, (
        "heartbeater must keep the native capture_pane (scrape wins)"
    )
    payload_src = ast.dump(ast.parse(
        inspect.getsource(bridge._build_heartbeat_payload)))
    assert "_get_acp_context_pct" in payload_src, (
        "payload builder must consult the ACP holder (G5 report path)"
    )
    adapter_src = inspect.getsource(bridge.run_acp_once)
    assert '"usage_update"' in adapter_src and "_set_acp_context_pct" in adapter_src, (
        "run_acp_once must stamp the holder from usage_update (G5 source)"
    )
    print("PASS test_bridge_wiring_pinned_positive")


def _garbage_cases() -> list[dict]:
    """The ten garbage shapes the guard must reject (W1, Rex review #554)."""
    return [
        {"size": 0, "used": 0},            # size=0: division-by-zero guard
        {"size": 500000, "used": 500001},  # used > size
        {"size": 500000, "used": -5},      # negative used
        {"size": -500000, "used": 5},      # negative size
        {"size": "500000", "used": 5},     # size as string
        {"size": 500000, "used": 5.5},     # used as float
        {"size": None, "used": 5},         # size missing (None)
        {"used": 5},                       # size field absent
        {"size": 500000},                  # used field absent
        {},                                # both absent
    ]


def _fixture_with_usage_update(upd: dict) -> Path:
    """silent-cancel fixture + ONE spliced usage_update line (the same event
    shape the real omp stream carries) -> tmp ndjson Path (run_adapter's
    fake server calls fixture.read_text(), so it must be a Path)."""
    src = open(os.path.join(ROOT, "rpc", "acp-silent-cancel.ndjson"),
               encoding="utf-8").read().rstrip("\n")
    line = json.dumps({
        "jsonrpc": "2.0", "method": "session/update",
        "params": {"sessionId": "01a08150-b7ac-71d0-b036-e8536d952cc3",
                   "update": {"sessionUpdate": "usage_update", **upd}},
    })
    # before the final stop result — the position the real stream uses
    lines = src.split("\n")
    lines.insert(len(lines) - 1, line)
    tmp = tempfile.NamedTemporaryFile(
        "w", suffix=".ndjson", delete=False, encoding="utf-8")
    tmp.write("\n".join(lines) + "\n")
    tmp.close()
    return Path(tmp.name)


def _run_garbage_probe(tree_root: str) -> int:
    """Run the garbage cases through the REAL adapter against tree_root's
    bridge.py in a SUBPROCESS; the subprocess splices ONE garbage
    usage_update per case into its own silent-fixture copy (so each case
    really flows through the production handler). Counts bites: holder
    moved away from the 42.0 preset, or the run raised — garbage reaching
    the stamp logic IS a bite."""
    script = (
        "import json, sys, tempfile, os\n"
        "root, tests = sys.argv[1], sys.argv[2]\n"
        "sys.path[:0] = [tests, root]\n"
        "import bridge\n"
        "import test_acp_adapter as taa\n"
        "from pathlib import Path\n"
        "cases = json.loads(sys.argv[3])\n"
        "SID = '01a08150-b7ac-71d0-b036-e8536d952cc3'\n"
        "silent = open(taa.FIXTURES['silent'], encoding='utf-8').read().rstrip('\\n')\n"
        "bites = 0\n"
        "for upd in cases:\n"
        "    lines = silent.split('\\n')\n"
        "    lines.insert(len(lines) - 1, json.dumps({\n"
        "        'jsonrpc': '2.0', 'method': 'session/update',\n"
        "        'params': {'sessionId': SID,\n"
        "                   'update': {'sessionUpdate': 'usage_update', **upd}}}))\n"
        "    tmp = tempfile.NamedTemporaryFile('w', suffix='.ndjson', delete=False, encoding='utf-8')\n"
        "    tmp.write('\\n'.join(lines) + '\\n')\n"
        "    tmp.close()\n"
        "    bridge._set_acp_context_pct(42.0)\n"
        "    try:\n"
        "        taa.run_adapter(Path(tmp.name))\n"
        "        if bridge._get_acp_context_pct() != 42.0:\n"
        "            bites += 1\n"
        "    except Exception:\n"
        "        bites += 1  # garbage reaching the stamp logic IS the bite\n"
        "    os.unlink(tmp.name)\n"
        "print(json.dumps({'bites': bites, 'cases': len(cases)}))\n"
    )
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    res = subprocess.run(
        [sys.executable, "-c", script, tree_root,
         os.path.join(tree_root, "tests"), json.dumps(_garbage_cases())],
        capture_output=True, text=True, env=env, timeout=300,
    )
    assert res.returncode == 0, (
        f"probe run against {tree_root} crashed:\n"
        f"stdout: {res.stdout}\nstderr: {res.stderr[-800:]}"
    )
    return json.loads(res.stdout.strip().splitlines()[-1])["bites"]


def test_usage_update_garbage_is_rejected_field_by_field():
    """W1 (Rex review #554 + #560 B1): BEHAVIOURAL pin — every garbage
    usage_update goes through the REAL adapter (bridge.run_acp_once, the
    actual production handler, no copied guard expression) and must leave
    the holder at its preset value. This is the test Rex demanded: the
    guard is exercised, not restated."""
    bridge._set_acp_context_pct(None)
    try:
        for upd in _garbage_cases():
            fixture = _fixture_with_usage_update(upd)
            bridge._set_acp_context_pct(42.0)
            outcome, _ = taa.run_adapter(fixture)
            assert outcome.final_stop_reason == "cancelled", (
                f"garbage usage_update {upd!r} broke the run itself "
                f"(stop={outcome.final_stop_reason!r})"
            )
            got = bridge._get_acp_context_pct()
            assert got == 42.0, (
                f"garbage usage_update {upd!r} changed the holder to {got!r} "
                "(reached the stamp logic past the guard)"
            )
            os.unlink(fixture)
    finally:
        bridge._set_acp_context_pct(None)
    print("PASS test_usage_update_garbage_is_rejected_field_by_field")


def test_usage_update_valid_values_still_stamped():
    """W1 positive control over the REAL adapter: valid usage_updates stamp
    the expected value (golden fixture ratio, boundaries 0 and 100)."""
    bridge._set_acp_context_pct(None)
    try:
        for upd, expected in [
            ({"size": 500000, "used": 17395}, 3.5),
            ({"size": 500000, "used": 0}, 0.0),
            ({"size": 500000, "used": 500000}, 100.0),
        ]:
            fixture = _fixture_with_usage_update(upd)
            outcome, _ = taa.run_adapter(fixture)
            assert outcome.final_stop_reason == "cancelled"
            got = bridge._get_acp_context_pct()
            assert got == expected, (
                f"{upd!r} -> holder {got!r}, expected {expected!r} "
                "(valid value did not reach the holder)"
            )
            os.unlink(fixture)
    finally:
        bridge._set_acp_context_pct(None)
    print("PASS test_usage_update_valid_values_still_stamped")


def test_w1_sabotage_probe_bites_on_real_adapter():
    """W1 sabotage (Rex review #560 B1), all three trees, each in its own
    subprocess running the REAL adapter over the garbage cases. Named
    lexicographically AFTER the in-process tests so the subprocess runs
    (and their teardown noise) cannot disturb live fake-server state
    (seen live: a mid-suite subprocess pass broke the next in-process
    run with a broken-pipe ACPError):

    - CONTROL (unmutated bridge.py): 0 bites — proves the probe measures
      behaviour, not its own anchor text (Rex' Kontrollprobe).
    - MUTATED (guard -> `if True:`): >= 1 bite — removing the guard lets
      garbage through (probe bites, red because of GARBAGE, not because of
      a missing anchor string).
    - REGRESSED (anchor block byte-identical, but an UNVALIDATED write
      `_set_acp_context_pct(-999.0)` added right before it): >= 1 bite —
      the behavioural pin catches a bypass that leaves the anchor text
      intact (Rex' Regression; the copied-guard version stayed 12/12 green
      here).
    """
    import shutil

    def make_tree(prefix: str, mutate=None) -> str:
        tmp = tempfile.mkdtemp(prefix=prefix)
        root = os.path.join(tmp, "bridge-root")
        shutil.copytree(ROOT, root)
        if mutate is not None:
            target = os.path.join(root, "bridge.py")
            src = open(target, encoding="utf-8").read()
            src = mutate(src)
            open(target, "w", encoding="utf-8").write(src)
        return root

    def mut_guard(src: str) -> str:
        anchor = (
            '            size, used = upd.get("size"), upd.get("used")\n'
            "            if (isinstance(size, int) and size > 0\n"
            "                    and isinstance(used, int) and 0 <= used <= size):\n"
            "                _set_acp_context_pct(round(used / size * 100.0, 1))"
        )
        assert src.count(anchor) == 1, "guard anchor not found in bridge.py"
        replacement = (
            '            size, used = upd.get("size"), upd.get("used")\n'
            "            if True:  # sabotage W1: validation guard neutralized\n"
            "                _set_acp_context_pct(round(used / size * 100.0, 1))"
        )
        return src.replace(anchor, replacement, 1)

    def mut_regress(src: str) -> str:
        anchor = """            size, used = upd.get("size"), upd.get("used")
            if (isinstance(size, int) and size > 0"""
        assert src.count(anchor) == 1, "regression anchor not found"
        return src.replace(anchor, """            _set_acp_context_pct(-999.0)  # regression: unvalidated write path
            size, used = upd.get("size"), upd.get("used")
            if (isinstance(size, int) and size > 0""", 1)

    control = make_tree("g5-w1-ctrl-")
    mutated = make_tree("g5-w1-mut-", mut_guard)
    regressed = make_tree("g5-w1-reg-", mut_regress)

    control_bites = _run_garbage_probe(control)  # replays ALL garbage cases
    mutated_bites = _run_garbage_probe(mutated)
    regressed_bites = _run_garbage_probe(regressed)

    assert control_bites == 0, (
        f"CONTROL must report 0 bites against unmutated bridge.py, got "
        f"{control_bites} — the probe does not measure behaviour"
    )
    assert mutated_bites >= 1, (
        "MUTATED tree (guard -> if True:) must let garbage through — "
        f"probe reported {mutated_bites} bites"
    )
    assert regressed_bites >= 1, (
        "REGRESSED tree (unvalidated write path, anchor intact) must be "
        f"caught — probe reported {regressed_bites} bites"
    )
    print("PASS test_sabotage_validation_guard_neutralized_garbage_passes "
          f"(control={control_bites}, mutated={mutated_bites}, "
          f"regressed={regressed_bites})")


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items())
           if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
    print(f"ALL PASS ({len(fns)} tests)")
