"""Harness development ring — capability declarations + two-way guard.

Marks directive (2026-09-16): every agent, regardless of kind, harness, host
or container, must work exactly as before after a runtime switch — and the
dev ring must show what to do when integrating a NEW harness.

This guard has TWO directions:

1. FORWARD: a harness appearing in HARNESS_CAPABILITIES (backend) or
   HOST_ADAPTERS (backend) without a capability declaration here → red.
2. BACKWARD: a "cannot, deliberately" exception that has gone stale — the
   claim no longer matches a verifiable code fact → red. A one-way
   exception list rots; this one is forced to re-verify.

Declaration values per (harness, place, capability):
  "da"                 — capability present, `proof` cites file:line or a
                         live measurement (see mc-cli-onboarding §7a).
  "fehlt-bewusst"      — deliberately absent. MUST carry `since` (ISO date)
                         and `recheck` (how to notice it went stale). The
                         guard re-derives the fact each run: if the cited
                         recheck contradicts the claim, red.
  "offen"              — unproven. Counts as a gap; keep few.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from app.services.harness_compat import HARNESSES, HARNESS_CAPABILITIES  # noqa: E402
from app.services import host_harness_adapter as hha  # noqa: E402

# ── The counted capability universe ─────────────────────────────────────────
# Derived from mechanism, not wish lists:
#  - Layer A (mechanical dispatch): what the backend does TO an agent.
#  - Layer B (task contract): what an agent must call to run a task.
#  - Layer C (thread/messaging): comm_v2 side (optional per agent — declared).
# Each entry: id -> (layer, what proves it). Proof style per
# mc-cli-onboarding §7a: file:line or live measurement, never config trust.

CAPABILITIES: dict[str, tuple[str, str]] = {
    # Layer A — mechanical dispatch (poll.sh / bridge / adapter level)
    "dispatch-prompt": ("A", "prompt arrives at the turn boundary (poll.sh paste/nudge or bridge wrap_prompt)"),
    "ack-transition": ("A", "backend sees in_progress via heartbeat/ack path (agent_heartbeat, agent_scoped.py:326)"),
    "turn-signal": ("A", "turn-signal file honored at turn end (poll.sh TURN_SIGNAL_FILE, :180)"),
    "term-int-survival": ("A", "TERM/INT trap cleanup (poll.sh:1610-1616)"),
    "heartbeat-context": ("A", "heartbeat carries context_pct (bridge _build_heartbeat_payload / statusline scrape)"),
    # Layer B — task contract (mc CLI CommandSpec registry, commands.py)
    "cli-ack": ("B", "mc ack (Task pickup; context file rewrite W5-E)"),
    "cli-comment": ("B", "mc comment progress/blocker/resolution"),
    "cli-checklist": ("B", "mc checklist add/done/skip"),
    "cli-deliverable": ("B", "mc deliverable (inline + file)"),
    "cli-finish": ("B", "mc finish + reflection contract (reflection.py)"),
    "cli-ask": ("B", "mc ask / question (operator channel)"),
    "cli-inbox": ("B", "mc inbox (nudge+pull delivery, MSG_DELIVERY_MODE=nudge)"),
    "cli-msg-thread": ("B", "mc msg / thread (comm_v2 message delivery)"),
    "cli-memory": ("B", "mc memory search/save (Qdrant + vault)"),
    "cli-vault": ("B", "mc vault-write/search"),
    "cli-review": ("B", "mc review/approve/reject/review-note (reviewer role)"),
    "cli-park": ("B", "mc park/hold (dispatch lifecycle)"),
    "cli-worker-restart": ("B", "mc worker-restart (self-restart via backend)"),
    "cli-task-state": ("B", "mc done/failed/blocked/hold/recover (status verbs)"),
    "cli-delegate": ("B", "mc delegate (orchestrator agents)"),
    "cli-docs": ("B", "mc docs/group-doc (shared knowledge)"),
    "deliverable-get": ("B", "mc deliverable-get (read back a registered deliverable)"),
    # Layer C — host/container placement
    "ssh-push": ("C", "workflow-scope workaround: SSH remote push (rule 15)"),
    "container-place": ("C", "runs as mc-<harness>-agent container or host entrypoint"),
}

# ── Harness x place declarations ────────────────────────────────────────────
# place: "container" or "host". Every (harness, place) pair the system can
# run MUST have a statement for EVERY capability. status ∈ {da,
# fehlt-bewusst, offen}. `proof` = file:line or measurement; `since`/`recheck`
# mandatory for fehlt-bewusst (stale-exception guard, direction 2).

D: list[dict] = [
    dict(harness="claude", place="container", cap="dispatch-prompt", status="da", proof="docker/mc-agent-base/start-claude.sh; settings_extras=True harness_compat.py:303"),
    dict(harness="claude", place="container", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326 + poll.sh heartbeat loop"),
    dict(harness="claude", place="container", cap="turn-signal", status="da", proof="poll.sh:180 TURN_SIGNAL_FILE; settings.json hooks (settings_extras)"),
    dict(harness="claude", place="container", cap="term-int-survival", status="da", proof="poll.sh:1615-1616 TERM/INT traps"),
    dict(harness="claude", place="container", cap="heartbeat-context", status="da", proof="statusLine scrape (settings_extras); #606/#609 holder"),
    dict(harness="claude", place="container", cap="ssh-push", status="fehlt-bewusst", proof="workflow-scope lock on HTTPS (fork probe identical, 15.09.)", since="2026-09-15", recheck="gh auth status scope list of agent credential"),
    dict(harness="claude", place="container", cap="container-place", status="da", proof="docker/mc-claude-agent/"),
    dict(harness="openclaude", place="host", cap="dispatch-prompt", status="da", proof="OpenClaudeHostAdapter host_harness_adapter.py:326 (staged host adapter)"),
    dict(harness="openclaude", place="host", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326 (poll.sh shared)"),
    dict(harness="openclaude", place="host", cap="turn-signal", status="offen", proof="settings_extras=None (protocol-flexible, harness_compat.py:310) - hook wiring per runtime unproven"),
    dict(harness="openclaude", place="host", cap="term-int-survival", status="da", proof="poll.sh:1615-1616 traps (shared poll.sh)"),
    dict(harness="openclaude", place="host", cap="heartbeat-context", status="offen", proof="scrape heuristics per runtime unknown (PANE_UI_OVERRIDE is claude-family only)"),
    dict(harness="openclaude", place="host", cap="ssh-push", status="fehlt-bewusst", proof="same credential as claude host path", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="openclaude", place="host", cap="container-place", status="fehlt-bewusst", proof="no docker/mc-openclaude-agent image in docker/", since="2026-09-16", recheck="ls docker/ for new image dir"),
    dict(harness="openclaude", place="container", cap="dispatch-prompt", status="offen", proof="no dedicated image; mc-agent-base variant unproven"),
    dict(harness="openclaude", place="container", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326 (endpoint harness-agnostic)"),
    dict(harness="openclaude", place="container", cap="turn-signal", status="offen", proof="settings_extras=None - hooks not wired"),
    dict(harness="openclaude", place="container", cap="term-int-survival", status="da", proof="poll.sh:1615-1616"),
    dict(harness="openclaude", place="container", cap="heartbeat-context", status="offen", proof="no statusLine surface proven"),
    dict(harness="openclaude", place="container", cap="ssh-push", status="fehlt-bewusst", proof="container credential lacks workflow scope (global lock, 15.09.)", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="openclaude", place="container", cap="container-place", status="fehlt-bewusst", proof="no docker/mc-openclaude-agent image", since="2026-09-16", recheck="ls docker/"),
    dict(harness="omp", place="container", cap="dispatch-prompt", status="da", proof="docker/omp-bridge/bridge.py wrap_prompt:1140 + run_acp_once:4151"),
    dict(harness="omp", place="container", cap="ack-transition", status="da", proof="bridge serve_loop heartbeater (start_heartbeater:1703)"),
    dict(harness="omp", place="container", cap="turn-signal", status="da", proof="bridge control channel (_on_control) + poll nudge; settings_extras=False (harness_compat.py:315) - bridge-native"),
    dict(harness="omp", place="container", cap="term-int-survival", status="da", proof="bridge acp-cancel watcher:4396 + entrypoint traps"),
    dict(harness="omp", place="container", cap="heartbeat-context", status="da", proof="ACPContextPct holder #609 (bridge.py:1639-1655)"),
    dict(harness="omp", place="container", cap="ssh-push", status="fehlt-bewusst", proof="container credential lacks workflow scope", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="omp", place="container", cap="container-place", status="da", proof="docker/omp-bridge/ (mc-omp-agent image)"),
    dict(harness="omp", place="host", cap="dispatch-prompt", status="da", proof="OmpHostAdapter (HOST_ADAPTERS host_harness_adapter.py:446-454)"),
    dict(harness="omp", place="host", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326"),
    dict(harness="omp", place="host", cap="turn-signal", status="offen", proof="bridge-native control proven in container; host entrypoint wiring unproven"),
    dict(harness="omp", place="host", cap="term-int-survival", status="da", proof="poll.sh traps on host path"),
    dict(harness="omp", place="host", cap="heartbeat-context", status="da", proof="same bridge holder path"),
    dict(harness="omp", place="host", cap="ssh-push", status="fehlt-bewusst", proof="same credential", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="omp", place="host", cap="container-place", status="da", proof="OmpHostAdapter in HOST_ADAPTERS host_harness_adapter.py:446-454"),
    dict(harness="kimi", place="container", cap="dispatch-prompt", status="da", proof="KimiHostAdapter host_harness_adapter.py:141; container docker/mc-kimi-agent/"),
    dict(harness="kimi", place="container", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326"),
    dict(harness="kimi", place="container", cap="turn-signal", status="da", proof="shared poll.sh turn-signal (kimi-host entrypoint, skill mc-cli-onboarding section 3)"),
    dict(harness="kimi", place="container", cap="term-int-survival", status="da", proof="poll.sh:1615-1616"),
    dict(harness="kimi", place="container", cap="heartbeat-context", status="offen", proof="no statusline/usage scrape proven for kimi"),
    dict(harness="kimi", place="container", cap="ssh-push", status="fehlt-bewusst", proof="same credential", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="kimi", place="container", cap="container-place", status="da", proof="docker/mc-kimi-agent/ (container) / KimiHostAdapter:141 (host)"),
    dict(harness="claude", place="host", cap="dispatch-prompt", status="da", proof="docker/mc-agent-base/start-claude.sh; settings_extras=True harness_compat.py:303"),
    dict(harness="claude", place="host", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326 + poll.sh heartbeat loop"),
    dict(harness="claude", place="host", cap="turn-signal", status="da", proof="poll.sh:180 TURN_SIGNAL_FILE; settings.json hooks (settings_extras)"),
    dict(harness="claude", place="host", cap="term-int-survival", status="da", proof="poll.sh:1615-1616 TERM/INT traps"),
    dict(harness="claude", place="host", cap="heartbeat-context", status="da", proof="statusLine scrape (settings_extras); #606/#609 holder"),
    dict(harness="claude", place="host", cap="ssh-push", status="fehlt-bewusst", proof="workflow-scope lock on HTTPS (fork probe identical, 15.09.)", since="2026-09-15", recheck="gh auth status scope list of agent credential"),
    dict(harness="claude", place="host", cap="container-place", status="da", proof="ClaudeHostAdapter host_harness_adapter.py:180-187 (entrypoint.sh + plist)"),
    dict(harness="kimi", place="host", cap="dispatch-prompt", status="da", proof="KimiHostAdapter host_harness_adapter.py:141; container docker/mc-kimi-agent/"),
    dict(harness="kimi", place="host", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326"),
    dict(harness="kimi", place="host", cap="turn-signal", status="da", proof="shared poll.sh turn-signal (kimi-host entrypoint, skill mc-cli-onboarding section 3)"),
    dict(harness="kimi", place="host", cap="term-int-survival", status="da", proof="poll.sh:1615-1616"),
    dict(harness="kimi", place="host", cap="heartbeat-context", status="offen", proof="no statusline/usage scrape proven for kimi"),
    dict(harness="kimi", place="host", cap="ssh-push", status="fehlt-bewusst", proof="same credential", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="kimi", place="host", cap="container-place", status="da", proof="docker/mc-kimi-agent/ (container) / KimiHostAdapter:141 (host)"),
    dict(harness="hermes", place="host", cap="dispatch-prompt", status="da", proof="hermes host adapter (HOST_ADAPTERS, native driver)"),
    dict(harness="hermes", place="host", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326"),
    dict(harness="hermes", place="host", cap="turn-signal", status="da", proof="native driver turn boundary (hermes ACP driver knob)"),
    dict(harness="hermes", place="host", cap="term-int-survival", status="da", proof="native driver lifecycle (ADR-064/066 host-only)"),
    dict(harness="hermes", place="host", cap="heartbeat-context", status="offen", proof="context_pct scrape not proven for native driver"),
    dict(harness="hermes", place="host", cap="ssh-push", status="fehlt-bewusst", proof="same host credential", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="hermes", place="host", cap="container-place", status="da", proof="host-only by design (harness_compat.py:330-337)"),
    dict(harness="hermes", place="container", cap="dispatch-prompt", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="container", cap="ack-transition", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="container", cap="turn-signal", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="container", cap="term-int-survival", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="container", cap="heartbeat-context", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="container", cap="ssh-push", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="container", cap="container-place", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="dispatch-prompt", status="da", proof="grok host adapter (HOST_ADAPTERS, native driver)"),
    dict(harness="grok", place="host", cap="ack-transition", status="da", proof="agent_heartbeat agent_scoped.py:326"),
    dict(harness="grok", place="host", cap="turn-signal", status="da", proof="native driver turn boundary (hermes ACP driver knob)"),
    dict(harness="grok", place="host", cap="term-int-survival", status="da", proof="native driver lifecycle (ADR-064/066 host-only)"),
    dict(harness="grok", place="host", cap="heartbeat-context", status="offen", proof="context_pct scrape not proven for native driver"),
    dict(harness="grok", place="host", cap="ssh-push", status="fehlt-bewusst", proof="same host credential", since="2026-09-15", recheck="gh auth status scope list"),
    dict(harness="grok", place="host", cap="container-place", status="da", proof="host-only by design (harness_compat.py:330-337)"),
    dict(harness="grok", place="container", cap="dispatch-prompt", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="container", cap="ack-transition", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="container", cap="turn-signal", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="container", cap="term-int-survival", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="container", cap="heartbeat-context", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="container", cap="ssh-push", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="container", cap="container-place", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="claude", place="container", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="container", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="claude", place="host", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="container", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="omp", place="host", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="container", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="kimi", place="host", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="container", cap="cli-ack", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-comment", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-checklist", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-deliverable", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-finish", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-ask", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-inbox", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-msg-thread", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-memory", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-vault", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-review", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-park", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-worker-restart", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-task-state", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-delegate", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="container", cap="cli-docs", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="host", cap="cli-ack", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-comment", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-checklist", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-deliverable", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-finish", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-ask", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-inbox", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-msg-thread", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-memory", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-vault", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-review", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-park", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-worker-restart", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-task-state", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-delegate", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="openclaude", place="host", cap="cli-docs", status="da", proof="mc CLI in image (docker/mc-agent-base/mc-cli/) or host checkout (scripts/mc-cli) - harness-agnostic; test_mc_cli_endpoints guards endpoints"),
    dict(harness="hermes", place="host", cap="cli-ack", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-ack", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-comment", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-comment", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-checklist", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-checklist", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-deliverable", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-deliverable", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-finish", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-finish", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-ask", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-ask", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-inbox", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-inbox", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-msg-thread", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-msg-thread", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-memory", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-memory", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-vault", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-vault", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-review", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-review", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-park", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-park", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-worker-restart", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-worker-restart", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-task-state", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-task-state", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-delegate", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-delegate", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="hermes", place="host", cap="cli-docs", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="cli-docs", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-ack", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-ack", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-comment", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-comment", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-checklist", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-checklist", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-deliverable", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-deliverable", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-finish", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-finish", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-ask", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-ask", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-inbox", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-inbox", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-msg-thread", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-msg-thread", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-memory", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-memory", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-vault", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-vault", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-review", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-review", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-park", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-park", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-worker-restart", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-worker-restart", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-task-state", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-task-state", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-delegate", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-delegate", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="cli-docs", status="da", proof="mc CLI via host checkout scripts/mc-cli (host agents share ${HOME}/.mc, cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="cli-docs", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="claude", place="container", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="claude", place="host", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="omp", place="container", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="omp", place="host", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="kimi", place="container", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="kimi", place="host", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="openclaude", place="container", cap="deliverable-get", status="fehlt-bewusst", proof="no container image for this harness yet (see container-place row)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="openclaude", place="host", cap="deliverable-get", status="da", proof="mc deliverable-get CommandSpec (scripts/mc-cli/mc_cli/commands.py) in image or host checkout - harness-agnostic"),
    dict(harness="hermes", place="host", cap="deliverable-get", status="da", proof="mc deliverable-get via host checkout scripts/mc-cli (cli_bridge_runner.py:7)"),
    dict(harness="hermes", place="container", cap="deliverable-get", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
    dict(harness="grok", place="host", cap="deliverable-get", status="da", proof="mc deliverable-get via host checkout scripts/mc-cli (cli_bridge_runner.py:7)"),
    dict(harness="grok", place="container", cap="deliverable-get", status="fehlt-bewusst", proof="host-only harness (harness_compat.py:330-337)", since="2026-09-16", recheck="new container image appearing in docker/"),
]


def _decl(harness: str, place: str, cap: str) -> dict | None:
    for row in D:
        if row["harness"] == harness and row["place"] == place and row["cap"] == cap:
            return row
    return None


@pytest.mark.parametrize("harness", sorted(HARNESS_CAPABILITIES))
@pytest.mark.parametrize("place", ["container", "host"])
def test_every_harness_place_has_a_statement_for_every_capability(harness: str, place: str):
    """Direction 1: no harness appears without a full statement column."""
    missing = [cap for cap in CAPABILITIES if _decl(harness, place, cap) is None]
    assert not missing, (
        f"Harness '{harness}' ({place}) lacks capability statements for: {missing}. "
        "Add a row to D in tests/test_harness_dev_ring.py with status da/"
        "fehlt-bewusst/offen + proof. See module docstring for the contract."
    )


@pytest.mark.parametrize("row", D, ids=lambda r: f"{r['harness']}/{r['place']}/{r['cap']}")
def test_declaration_shape(row: dict):
    assert row["status"] in ("da", "fehlt-bewusst", "offen")
    assert row.get("proof"), "every statement carries a proof (file:line or measurement)"
    if row["status"] == "fehlt-bewusst":
        assert row.get("since"), "fehlt-bewusst needs since=ISO date"
        assert row.get("recheck"), "fehlt-bewusst needs recheck= (how staleness would show)"


# Every registry a harness can enter must be patrolled. HARNESS_CAPABILITIES
# alone is not enough: runtimes.py iterates the cli-bridge matrix over
# HARNESSES, and host_harness_adapter.HOST_ADAPTERS gates host offers — a
# harness added to either without HARNESS_CAPABILITIES previously slipped
# past the guard (Rex review 2026-09-16, bypass "phantom2").
REGISTRY_SOURCES = {
    "HARNESS_CAPABILITIES": set(HARNESS_CAPABILITIES),
    "HARNESSES": set(HARNESSES),
    "HOST_ADAPTERS": set(hha.HOST_ADAPTERS),
}
# jarvis is the voice worker (single-agent voice binding, no task loop) —
# deliberate exemption, reason recorded here per ADR-084 discipline.
REGISTRY_EXEMPT = {"jarvis": "voice-worker: no task loop, no memory surface"}


def test_harness_registries_stay_in_sync():
    """Direction 1b: no harness enters ANY registry without capabilities."""
    caps = set(HARNESS_CAPABILITIES)
    for source, members in REGISTRY_SOURCES.items():
        unpatrolled = members - caps - set(REGISTRY_EXEMPT)
        assert not unpatrolled, (
            f"{source} contains harnesses without a HARNESS_CAPABILITIES entry: "
            f"{sorted(unpatrolled)}. Add them to HARNESS_CAPABILITIES (or, only "
            "with a recorded reason, to REGISTRY_EXEMPT)."
        )
    dead_exempt = set(REGISTRY_EXEMPT) - set().union(*REGISTRY_SOURCES.values()) if REGISTRY_SOURCES else set()
    assert not dead_exempt, f"exemptions for harnesses in no registry: {dead_exempt}"


def test_registry_and_declarations_stay_in_sync():
    """A harness removed from the registry must not keep dead rows here."""
    declared = {(r["harness"]) for r in D}
    ghosts = declared - set(HARNESS_CAPABILITIES)
    assert not ghosts, f"declaration rows for unknown harnesses: {ghosts}"


# ── Direction 2: stale-exception re-derivation ─────────────────────────────
# A "fehlt-bewusst" claim must MATCH a verifiable fact. We re-derive the two
# facts the system actually controls:

def test_stale_exceptions_container_images():
    """'fehlt-bewusst' on a container place must match an absent image dir."""
    for row in D:
        if row["place"] != "container" or row["status"] != "fehlt-bewusst":
            continue
        img = REPO_ROOT.parent / "docker" / f"mc-{row['harness']}-agent"
        if row["cap"] == "container-place":
            # claiming no container place while the image exists → stale
            assert not img.exists(), (
                f"stale exception: {row['harness']} declared container-place "
                f"fehlt-bewusst (since {row.get('since')}) but docker/mc-{row['harness']}-agent exists — "
                "update the declaration (it can now, or say da with proof)"
            )


def test_stale_exceptions_ssh_scope_claim():
    """ssh-push fehlt-bewusst must still match the credential's scope reality."""
    for row in D:
        if row["cap"] != "ssh-push" or row["status"] != "fehlt-bewusst":
            continue
        # The lock applies to HTTPS/OAuth pushes only; the SSH path exists.
        # Re-derived from the TRACKED marker docs/credential-scope-notes.md —
        # an untracked operator file made this check vacuous in CI (Rex review
        # 2026-09-16): missing basis must FAIL, never pass silently.
        marker = REPO_ROOT.parent / "docs" / "credential-scope-notes.md"
        assert marker.exists(), (
            "docs/credential-scope-notes.md (tracked basis of the ssh-push "
            "fehlt-bewusst claim) is missing — restore it, the guard refuses "
            "to run on an unprovable claim"
        )
        text = marker.read_text(encoding="utf-8")
        # The marker must still record the canonical claim: agent credentials
        # carry NO workflow scope. If the operator re-verified and the claim
        # flipped, they update the marker's claim line — then this goes red
        # and the declarations must follow.
        if not re.search(r"carry[^\n]*\bno\b[^\n]*workflow", text, re.IGNORECASE):
            pytest.fail(
                "stale exception: ssh-push fehlt-bewusst but docs/"
                "credential-scope-notes.md no longer records 'agent "
                "credentials carry no workflow scope' — re-verify the "
                "credential and flip the ssh-push declarations to da"
            )


def test_dev_ring_doc_exists():
    """The dev-ring handreichung must exist and name every layer-A..C item."""
    doc = REPO_ROOT.parent / "docs" / "harness-dev-ring.md"
    assert doc.exists(), "docs/harness-dev-ring.md missing — the dev ring needs its handbook"
    text = doc.read_text(encoding="utf-8")
    for cap in CAPABILITIES:
        assert cap in text, f"docs/harness-dev-ring.md does not mention capability '{cap}'"
