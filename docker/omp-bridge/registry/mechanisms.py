"""Registry of mechanisms every family branch must serve (card f5f1bb9b).

The Bridge/chat-daemon "family" shares mechanics that were repeatedly built
at ONE site and silently missing at its twin site — found in operation, not
by tests (#566 model pinning, #557 context-env writers, #547 cwd, the
reactivation-path family). This registry is the machine-checkable contract:
three states per (branch, mechanism) cell, and a parity test
(``tests/test_mechanism_registry_parity.py``) that goes RED when a branch
stops serving a REQUIRED entry.

States:
    SERVED            the branch implements the mechanism (anchor verified)
    REQUIRED_MISSING  gap that must be fixed -> test goes red
    DELIBERATE        consciously not served here, with a reason — first-class
                      state, NOT a failure (avoids pressure towards false
                      alignment; cf. #562)

Anti-#560 rule: anchors are structural (AST function/def lookup or exact
shell construct), never loose substring greps on prose.

Maintenance rule: keep it SMALL. An entry earns its place by having bitten
once (every current entry cites its incident/PR). A registry nobody updates
is the next ledger.
"""

from __future__ import annotations

import os
from typing import Dict, Optional, Tuple

REPO_ROOT = os.path.dirname(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

SERVED = "served"
REQUIRED_MISSING = "required_missing"
DELIBERATE = "deliberate"


class Mechanism:
    __slots__ = ("key", "title", "why", "anchor")

    def __init__(self, key: str, title: str, why: str, anchor: str) -> None:
        self.key = key
        self.title = title
        self.why = why  # the incident that earned the entry
        self.anchor = anchor  # human-readable anchor description


class Entry:
    __slots__ = ("state", "anchor", "reason", "not_anchor")

    def __init__(
        self,
        state: str,
        anchor: Optional[str] = None,
        reason: Optional[str] = None,
        not_anchor: Optional[str] = None,
    ) -> None:
        self.state = state
        self.anchor = anchor  # "file.py::qualname" or "shell::pattern"
        self.reason = reason  # required for DELIBERATE / REQUIRED_MISSING
        # W2 (Rex review): optional counter-anchor for DELIBERATE cells.
        # If it RESOLVES in the branch source, the deliberate-abstention
        # claim is stale (the branch meanwhile serves the mechanism) and
        # the suite goes red. PARTIAL guard: only usable where absence is
        # checkable at all -- cells like (LAUNCH, server_cursor_semantics)
        # ("the launcher builds no messages") stay trust-based. Where the
        # anchor is a python symbol, absence is provable; where the twin
        # writes shell constructs with a different shape, a not_anchor may
        # not exist -- that is fine, it is optional by design.
        self.not_anchor = not_anchor


# -- family members ------------------------------------------------------------
OMP_CONTAINER_ROOT = os.path.join(REPO_ROOT, "docker", "omp-bridge")
SCRIPTS_ROOT = os.path.join(REPO_ROOT, "scripts")
SHARED_ROOT = os.path.join(REPO_ROOT, "docker", "shared")
HERMES_DIR_ROOT = os.path.join(REPO_ROOT, "docker", "hermes")

BRIDGE = "bridge.py (omp container: ACP + native TUI paths)"
ACP_CHAT = "acp_chat.py (omp chat daemon)"
GROK = "grok-bridge.py (host tmux driver, grok)"
HERMES = "hermes-bridge.py (host tmux driver, hermes)"
LAUNCH = "launch-omp.sh (native omp invocation, ADR-049)"
POLL = "poll.sh (shared polling heart)"

BRANCH_FILES: Dict[str, str] = {
    BRIDGE: os.path.join(OMP_CONTAINER_ROOT, "bridge.py"),
    ACP_CHAT: os.path.join(OMP_CONTAINER_ROOT, "acp_chat.py"),
    GROK: os.path.join(SCRIPTS_ROOT, "grok-bridge.py"),
    HERMES: os.path.join(SCRIPTS_ROOT, "hermes-bridge.py"),
    LAUNCH: os.path.join(OMP_CONTAINER_ROOT, "launch-omp.sh"),
    POLL: os.path.join(SHARED_ROOT, "poll.sh"),
}

# -- mechanisms (each one earned by an incident) --------------------------------
M_MODEL = Mechanism(
    "model_pinning",
    "Model pinning - never let the runtime fall back to an unpinned model",
    "#566/#483: unpinned sessions opened on omp's built-in openai provider "
    "(unpinned default model) with the shim key and died 401 in operation "
    "after a container recreate.",
    "model selector wired at session/invocation start",
)
M_AUTH = Mechanism(
    "auth_error_classification",
    "Provider auth/quota error classification - operator sees key/budget "
    "problems as such, not as generic rpc_error",
    "#566 aftermath: 401 sk-noauth surfaced as an opaque failure; bridge.py "
    "and acp_chat.py classify provider auth/quota errors explicitly.",
    "provider auth-error regex applied to failure text",
)
M_CURSOR = Mechanism(
    "server_cursor_semantics",
    "Server-side cursor semantics - bridge never acks locally; the agent's "
    "own mc inbox ack advances the cursor",
    "#557-family + nudge mode (ADR-071): local acks made the server miss "
    "deliveries; every paste-mode branch must not ack locally.",
    "MSG_DELIVERY_MODE + no local-ack invariant",
)
M_HEARTBEAT = Mechanism(
    "heartbeat_fields",
    "Heartbeat contract - sender exists and body stays a valid JSON object; "
    "context_pct omitted (not zeroed) when unknown",
    "CTX-01: heartbeat must never break on scrape failure (grok/hermes), "
    "poll.sh encodes status/ctx/task/attempt (CTX-01 Nachzug).",
    "heartbeat sender + JSON-object body invariant",
)
M_CONTEXT_ENV = Mechanism(
    "task_context_env",
    "Task context channel - per-dispatch task context reaches the agent "
    "(ENV-FILE promise, dispatch.py:8-18)",
    "#557: two of three MC_CONTEXT_ENV_PATH writers carried, the third "
    "didn't; a fresh dispatch must deliver fresh context on every branch.",
    "write_task_context_env OR inline-context reason",
)

# -- the registry: (branch, mechanism) -> Entry ----------------------------------
REGISTRY = {
    # M1 model pinning ---------------------------------------------------------
    (BRIDGE, M_MODEL): Entry(
        SERVED,
        anchor="docker/omp-bridge/bridge.py::run_omp_subprocess&const:--model",
    ),
    (ACP_CHAT, M_MODEL): Entry(
        SERVED,
        anchor="docker/omp-bridge/acp_chat.py::AcpChat._pin_model&call:self._pin_model()",
    ),
    (GROK, M_MODEL): Entry(
        DELIBERATE,
        reason="ADR-066: grok has NO OPENAI_*/ANTHROPIC_* provider env and no "
        "MC-bound model endpoint; the runtime binding is a display/anchor only.",
    ),
    (HERMES, M_MODEL): Entry(
        SERVED,
        anchor="docker/hermes/entrypoint.sh::PATCH_SCRIPT=.*hermes-config-patch\.py",
    ),
    (LAUNCH, M_MODEL): Entry(
        SERVED,
        anchor="docker/omp-bridge/launch-omp.sh::\$\{OMP_MODEL_SELECTOR:-",
    ),
    (POLL, M_MODEL): Entry(
        DELIBERATE,
        reason="poll.sh is a polling heart - it invokes no model runtime and "
        "pins nothing (no continuation path of its own).",
        # W2: stale-abstention guard -- if poll.sh ever starts wiring a
        # model selector, this cell must be re-classified.
        not_anchor="docker/shared/poll.sh::OMP_MODEL_SELECTOR",
    ),
    # M2 auth error classification --------------------------------------------
    (BRIDGE, M_AUTH): Entry(
        SERVED,
        anchor="docker/omp-bridge/bridge.py::PROVIDER_AUTH_ERROR_RE",
    ),
    (ACP_CHAT, M_AUTH): Entry(
        SERVED,
        anchor="docker/omp-bridge/acp_chat.py::_PROVIDER_ERROR_RE",
    ),
    (GROK, M_AUTH): Entry(
        DELIBERATE,
        reason="Watchdog model: grok-bridge restarts the tmux session on "
        "no-progress; failure text reaches the operator via MC, not via an "
        "error-class channel (no provider API key handling in the bridge).",
    ),
    (HERMES, M_AUTH): Entry(
        DELIBERATE,
        reason="Watchdog model (entrypoint.sh while-true loop): auth failures "
        "kill the binary and surface as watchdog restarts; bridge holds no "
        "provider keys, nothing to classify at this layer.",
    ),
    (LAUNCH, M_AUTH): Entry(
        DELIBERATE,
        reason="One-shot launcher: a 401 here ends the invocation; the "
        "classification lives in the bridge paths that own long-lived runs.",
    ),
    (POLL, M_AUTH): Entry(
        DELIBERATE,
        reason="poll.sh classifies stream abort KINDS (abort_transient_api "
        "etc.) but speaks no provider protocol; no auth surface of its own.",
    ),
    # M3 server cursor semantics ----------------------------------------------
    (BRIDGE, M_CURSOR): Entry(
        SERVED,
        anchor="docker/omp-bridge/bridge.py::MSG_DELIVERY_MODE",
    ),
    (ACP_CHAT, M_CURSOR): Entry(
        DELIBERATE,
        reason="acp_chat is the in-chat event sink (transcript/preview/error "
        "events); it dispatches no MC messages and owns no delivery mode.",
    ),
    (GROK, M_CURSOR): Entry(
        SERVED,
        anchor="scripts/grok-bridge.py::MSG_DELIVERY_MODE",
    ),
    (HERMES, M_CURSOR): Entry(
        SERVED,
        anchor="scripts/hermes-bridge.py::MSG_DELIVERY_MODE",
    ),
    (LAUNCH, M_CURSOR): Entry(
        DELIBERATE,
        reason="Launcher builds no messages; delivery mode is decided by the "
        "bridges that own the paste/nudge paths.",
    ),
    (POLL, M_CURSOR): Entry(
        SERVED,
        anchor="docker/shared/poll.sh::MSG_DELIVERY_MODE=\"\\$\\{MSG_DELIVERY_MODE:-",
    ),
    # M4 heartbeat fields -------------------------------------------------------
    (BRIDGE, M_HEARTBEAT): Entry(
        SERVED,
        anchor="docker/omp-bridge/bridge.py::start_heartbeater",
    ),
    (ACP_CHAT, M_HEARTBEAT): Entry(
        DELIBERATE,
        reason="acp_chat emits transcript/preview/error events into session "
        "files; heartbeat emission stays with the bridges/poll loops.",
    ),
    (GROK, M_HEARTBEAT): Entry(
        SERVED,
        anchor="scripts/grok-bridge.py::_heartbeat_body",
    ),
    (HERMES, M_HEARTBEAT): Entry(
        SERVED,
        anchor="scripts/hermes-bridge.py::_heartbeat_body",
    ),
    (LAUNCH, M_HEARTBEAT): Entry(
        DELIBERATE,
        reason="One-shot launcher: no heartbeat of its own (the relaunching "
        "bridge owns it).",
    ),
    (POLL, M_HEARTBEAT): Entry(
        SERVED,
        anchor="docker/shared/poll.sh::^build_heartbeat_payload\(\)\s*\{",
    ),
    # M5 task context channel ----------------------------------------------------
    (BRIDGE, M_CONTEXT_ENV): Entry(
        SERVED,
        anchor="docker/omp-bridge/bridge.py::write_task_context_env",
    ),
    (ACP_CHAT, M_CONTEXT_ENV): Entry(
        DELIBERATE,
        reason="Chat sessions are interactive continuations of an existing "
        "thread; per-dispatch task context is a DISPATCH concern (bridge.py).",
    ),
    (GROK, M_CONTEXT_ENV): Entry(
        SERVED,
        anchor="scripts/grok-bridge.py::write_task_context_env",
    ),
    (HERMES, M_CONTEXT_ENV): Entry(
        SERVED,
        # #557 nachzug (2026-09-15): the DELIBERATE not_anchor above is what
        # it was there to catch -- hermes-bridge.py now sets its own
        # per-agent MC_CONTEXT_ENV_PATH default (propagated into the agent
        # subprocess env) and docker/hermes/entrypoint.sh's watchdog loop
        # re-asserts that default across restarts, so hermes IS one of the
        # per-agent context-file channels now, not an abstainer.
        anchor="scripts/hermes-bridge.py::MC_CONTEXT_ENV_PATH",
    ),
    (LAUNCH, M_CONTEXT_ENV): Entry(
        DELIBERATE,
        reason="Launcher receives cwd only; task context is written by the "
        "dispatching bridges before the paste.",
    ),
    (POLL, M_CONTEXT_ENV): Entry(
        SERVED,
        # B1 (Rex review): poll.sh DOES carry the per-task context file --
        # it writes the per-agent context file in run_task() and clears it
        # on operator stop (docker/shared/poll.sh:756-761, :967; config.py:50
        # even names poll.sh as a writer).
        # #557 nachzug (2026-09-15): poll.sh no longer hard-codes the
        # literal /tmp path -- it now writes via $MC_CONTEXT_ENV_PATH
        # (default unchanged: /tmp/mc-context.env), the same indirection
        # this mechanism's title cites for the other writers.
        anchor=r'docker/shared/poll.sh::^\s*cat > "\$MC_CONTEXT_ENV_PATH"',
    ),
}


def branch_source(branch: str) -> str:
    return BRANCH_FILES[branch]


def served_entries():
    return {k: v for k, v in REGISTRY.items() if v.state == SERVED}


def deliberate_entries():
    return {k: v for k, v in REGISTRY.items() if v.state == DELIBERATE}
