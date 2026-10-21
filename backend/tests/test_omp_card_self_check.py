"""The Operating Card tells omp agents how to check their own web work.

Found live on 2026-10-21: an omp agent built a single-file canvas page, then
searched the container for `node`/`chromium`/`playwright`, found none and
fell back to `pip install --break-system-packages`. A browser was attached
the whole time — omp's `eval` tool has a global `browser` wired to the shared
headless Chrome (`browser.cdpUrl`, docker/omp-bridge/render-omp-config.sh).
The agent just did not know.

The card is the only MC text an omp agent reads (bridge.py `_identity_block`
injects CARD.md and nothing else), so the hint lives there — and only for
omp: the `browser` global is omp's, other harnesses have other tools.

Facts the section must keep right (checked against the omp 18.10.1 binary
and a live probe through the shared browser):
- the browser runs in its own container, so `file://` and `localhost` do not
  reach the agent's files (net::ERR_FILE_NOT_FOUND / ERR_CONNECTION_REFUSED);
- a `python3 -m http.server` in the agent container, opened as
  `http://<hostname>:<port>/`, does (HTTP 200, console errors and page
  errors visible).
"""
from __future__ import annotations

import uuid

import pytest

from app.models.agent import Agent
from app.services.template_renderer import build_agent_context, render_agent_file

SECTION_HEADING = "## Self-check"

# What the agent needs to act on, not the exact wording.
OMP_MARKERS = [
    "`eval`",                         # the omp tool that owns the browser
    "`browser`",                      # its global object
    "browser.open(",                  # how a tab is opened
    "tab.errors()",                   # uncaught page errors
    "tab.console()",                  # console output
    "tab.screenshot()",               # visual check
    "python3 -m http.server",         # how the file reaches the browser
    "hostname",                       # ... under which address
    "`file://`",                      # the trap that does not work
    "node --check",                   # JS syntax check
    "--break-system-packages",        # named as forbidden
]

NON_OMP_HARNESSES = ["claude", "grok", "hermes", "kimi", "jarvis", None]


def _render_card(harness: str | None, role: str = "Developer", comm_v2: bool = False) -> str:
    agent = Agent(
        id=uuid.uuid4(),
        name="TestAgent",
        role=role,
        board_id=uuid.uuid4(),
        is_board_lead=False,
        harness=harness,
        comm_v2=comm_v2,
    )
    ctx = build_agent_context(agent, agents_on_board=[])
    return render_agent_file("CARD.md.j2", ctx)


def _section(card: str) -> str:
    start = card.index(SECTION_HEADING)
    nxt = card.find("\n## ", start + len(SECTION_HEADING))
    return card[start:] if nxt == -1 else card[start:nxt]


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
    section = _section(_render_card("omp"))
    flat = " ".join(section.split())
    assert "`file://` and `localhost` do not reach" in flat, section


def test_omp_self_check_forbids_installing_a_browser():
    flat = " ".join(_section(_render_card("omp")).split())
    assert "never search for or install" in flat, flat


@pytest.mark.parametrize("harness", NON_OMP_HARNESSES)
def test_self_check_section_only_for_omp(harness):
    card = _render_card(harness)
    assert SECTION_HEADING not in card, f"harness={harness!r} got the omp self-check section"
    assert "browser.open(" not in card
    assert "python3 -m http.server" not in card
