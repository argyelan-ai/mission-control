"""The Operating Card tells omp agents how to check their own web work.

Found live on 2026-10-21: an omp agent built a single-file canvas page, then
searched the container for `node`/`chromium`/`playwright`, found none and
fell back to `pip install --break-system-packages` (and, further on, to a
self-downloaded Chromium). A browser was attached the whole time — omp's
`eval` tool has a global `browser` wired to the shared headless Chrome
(`browser.cdpUrl`, docker/omp-bridge/render-omp-config.sh). The agent just
did not know.

The card is the only MC text an omp agent reads (bridge.py `_identity_block`
injects CARD.md and nothing else), so the hint lives there — and only for
omp: the `browser` global is omp's, other harnesses have other tools. Board
leads do not get it: their card forbids writing or testing code themselves.

Facts the section must keep right (checked against the omp 18.10.1 binary
and a live probe through the shared browser):
- the shared browser runs in its own container, so `file://` and
  `localhost` do not reach the agent's files (net::ERR_FILE_NOT_FOUND /
  ERR_CONNECTION_REFUSED); whether it is shared at all depends on
  `browser.cdpUrl` (the `browser` compose profile is optional);
- a `python3 -m http.server` in the agent container, opened by the
  container's IP, does (HTTP 200, console errors and page errors visible).
  It must serve only the task folder: by default it serves the working
  directory (the whole workspace) to every container on the network;
- `tab.errors()` also reports syntax errors of inline `<script>` blocks, so
  the check needs no tool the image may lack (`node` is not guaranteed);
- a named `bash` service is stopped by writing to `proc://<name>/kill`.
"""
from __future__ import annotations

import re
import uuid

import pytest

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file
from tests.test_operating_card import CARD_BYTE_BUDGET

SECTION_HEADING = "## Self-check"

# What the agent needs to act on, not the exact wording.
OMP_MARKERS = [
    "`eval`",                         # the omp tool that owns the browser
    "`browser`",                      # its global object
    "browser.cdpUrl",                 # whether the browser is the shared one
    "browser.open(",                  # how a tab is opened
    "tab.errors()",                   # uncaught page errors (syntax too)
    "tab.console()",                  # console output
    "tab.screenshot()",               # visual check
    "python3 -m http.server",         # how the file reaches the browser
    "-d /deliverables/<task_id>",     # ... serving only the task folder
    "hostname -i",                    # ... under which address
    "`file://`",                      # the trap that does not work
    "proc://srv/kill",                # how the server is stopped again
    "--break-system-packages",        # named as forbidden
]

NON_OMP_HARNESSES = ["claude", "grok", "hermes", "kimi", "jarvis", None]

# Longest live role text on 2026-10-21 was 149 bytes. Role texts are free
# operator input, so the budget is pinned a bit above that, not at the short
# role names the matrix in test_operating_card.py uses.
LONG_ROLE = ("Developer — " + "lokale Umsetzung, Tests, Web-Seiten " * 5)[:160]


def _render_card(
    harness: str | None,
    role: str = "Developer",
    comm_v2: bool = False,
    is_lead: bool = False,
) -> str:
    agent = Agent(
        id=uuid.uuid4(),
        name="TestAgent",
        role=role,
        board_id=uuid.uuid4(),
        is_board_lead=is_lead,
        harness=harness,
        comm_v2=comm_v2,
    )
    ctx = build_agent_context(agent, agents_on_board=[])
    return render_agent_file("CARD.md.j2", ctx)


def _section(card: str) -> str:
    start = card.index(SECTION_HEADING)
    nxt = card.find("\n## ", start + len(SECTION_HEADING))
    return card[start:] if nxt == -1 else card[start:nxt]


def _flat(card: str) -> str:
    return " ".join(_section(card).split())


@pytest.mark.parametrize("comm_v2", [True, False])
@pytest.mark.parametrize("role", ["Developer", "Reviewer", "Designer"])
def test_omp_card_has_self_check_section(role, comm_v2):
    card = _render_card("omp", role=role, comm_v2=comm_v2)
    assert SECTION_HEADING in card, "omp card lost its self-check section"
    section = _section(card)
    missing = [m for m in OMP_MARKERS if m not in section]
    assert not missing, f"self-check section misses {missing}:\n{section}"


def test_omp_self_check_says_file_urls_do_not_reach_the_browser():
    # The section must steer AWAY from file:// and localhost, never towards
    # them: both fail against the shared browser container.
    flat = _flat(_render_card("omp"))
    assert "`file://` and `localhost` do not reach" in flat, flat


def test_omp_self_check_shared_browser_is_conditional():
    # cdp-browser is an optional compose profile; without it omp runs its own
    # local Chromium and file:// works. The "other container" claim must hang
    # on the agent's own config, not be stated as a fact for every install.
    flat = _flat(_render_card("omp"))
    cond = flat.find("If `omp config get browser.cdpUrl` prints a URL")
    claim = flat.find("another container")
    assert cond != -1, flat
    assert claim != -1 and cond < claim, flat


def test_omp_self_check_forbids_installing_a_browser():
    flat = _flat(_render_card("omp"))
    assert "never search for or install" in flat, flat


def test_omp_self_check_serves_only_the_task_folder():
    # Without -d, http.server serves the working directory — the whole
    # workspace — to every container on the Docker network.
    flat = _flat(_render_card("omp"))
    cmds = re.findall(r"python3 -m http\.server[^`]*", flat)
    assert cmds, flat
    for cmd in cmds:
        assert re.fullmatch(r"python3 -m http\.server \d+ -d /deliverables/<task_id>", cmd), cmd


def test_omp_self_check_stops_the_server_it_started():
    flat = _flat(_render_card("omp"))
    named = re.search(r"`bash` service named `(\w+)`", flat)
    assert named, flat
    assert f"`write proc://{named.group(1)}/kill`" in flat, flat


def test_omp_self_check_opens_the_page_by_ip():
    # An all-digit container id is not a valid URL host (WHATWG parses it as
    # an IPv4 number); the IP always is, and needs no DNS.
    flat = _flat(_render_card("omp"))
    assert "`hostname -i`" in flat, flat
    assert "http://<ip>:8765/" in flat, flat
    assert "<hostname>" not in flat, flat


def test_omp_self_check_needs_no_tool_the_image_may_lack():
    # `node` is not guaranteed in the omp image; a card line that runs it
    # turns every web task into a "missing tool" report. The browser already
    # reports inline-script syntax errors.
    flat = _flat(_render_card("omp"))
    for tool in ("node ", "npx ", "node --check"):
        assert tool not in flat, f"section relies on {tool.strip()!r}: {flat}"
    assert "`tab.errors()` (syntax errors too)" in flat, flat


def test_omp_self_check_missing_tool_is_not_a_blocker():
    # Hard gate 5 turns "stuck" into `mc blocked`; a missing nice-to-have
    # check tool must not feed it.
    flat = _flat(_render_card("omp"))
    assert "not a blocker" in flat, flat
    assert "report a missing tool" not in flat, flat


@pytest.mark.parametrize("harness", NON_OMP_HARNESSES)
def test_self_check_section_only_for_omp(harness):
    card = _render_card(harness)
    assert SECTION_HEADING not in card, f"harness={harness!r} got the omp self-check section"
    assert "browser.open(" not in card
    assert "python3 -m http.server" not in card


@pytest.mark.parametrize(
    "role, is_lead",
    [("Developer", True), ("Orchestrator", False), ("Lead", False)],
)
def test_self_check_skipped_for_leads(role, is_lead):
    # Leads are forbidden to write or test code themselves (card "Forbidden"),
    # so the self-check would be dead weight in their card.
    card = _render_card("omp", role=role, is_lead=is_lead, comm_v2=True)
    assert SECTION_HEADING not in card, f"role={role} is_lead={is_lead} got the self-check"
    assert "browser.open(" not in card


@pytest.mark.parametrize("is_lead", [True, False])
@pytest.mark.parametrize("comm_v2", [True, False])
def test_omp_card_with_long_live_role_fits_budget(is_lead, comm_v2):
    assert len(LONG_ROLE.encode("utf-8")) >= 150
    card = _render_card("omp", role=LONG_ROLE, comm_v2=comm_v2, is_lead=is_lead)
    size = len(card.encode("utf-8"))
    assert size <= CARD_BYTE_BUDGET, (
        f"omp card with a {len(LONG_ROLE.encode())}-byte role, is_lead={is_lead}, "
        f"comm_v2={comm_v2} is {size} bytes, over the {CARD_BYTE_BUDGET} budget"
    )
