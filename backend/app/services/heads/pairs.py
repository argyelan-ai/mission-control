"""Harness × runtime pairs for heads (docs/specs/head-launcher.md §4, §7).

v1 offers two pairs on a local engine: **omp × local** (default) and
**claude × local** (experimental, only where the engine serves the Anthropic
route). Everything else is listed as blocked with a reason code so the UI
can say why in plain words. kimi, hermes and grok are not offered at all.

Reason codes (text lives in the frontend i18n, never here):
protocol_mismatch · harness_not_supported · needs_operator_decision ·
unproven · engine_not_ready · box_busy · quota_limit.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict, dataclass

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost
from app.services.harness_compat import HARNESS_LABELS, runtime_protocol
from app.services.heads import engine
from app.services.recipe_switcher import runtime_host_ids
from app.services.runtime_protocols import runtime_protocols

#: Harnesses listed in the picker. kimi (unattended only with --yolo),
#: hermes (singleton bridge) and grok (xAI-only, -p banned) are not offered.
OFFERED_HARNESSES: tuple[str, ...] = ("omp", "claude", "openclaude")
DEFAULT_HARNESS = "omp"

LABELS = {**HARNESS_LABELS, "omp": "omp"}


@dataclass
class Pair:
    harness: str
    harness_label: str
    runtime_slug: str
    runtime_label: str
    model: str | None
    locality: str  # local | cloud
    status: str  # ok | experimental | blocked
    reason_code: str | None
    live: bool
    box_keys: list[str]
    busy_by: dict | None = None
    engine_in_use: bool = False

    @property
    def startable(self) -> bool:
        return self.status in ("ok", "experimental")

    def to_dict(self) -> dict:
        d = asdict(self)
        d["startable"] = self.startable
        return d


def is_local(runtime: Runtime) -> bool:
    """A runtime on one of our own boxes (host bound) speaking the OpenAI
    protocol. Cloud/HTTP-only runtimes carry no host (Runtime model contract)."""
    return runtime.host_id is not None and runtime_protocol(runtime) == "openai"


def pair_status(harness: str, runtime: Runtime, protocols: set[str]) -> tuple[str, str | None]:
    """Static part of the pair decision — which pairs exist at all in v1."""
    local = is_local(runtime)
    base = runtime_protocol(runtime)
    if harness == "omp":
        if local:
            return "ok", None
        if base == "anthropic":
            return "blocked", "needs_operator_decision"
        if base == "openai":
            return "blocked", "unproven"
        return "blocked", "protocol_mismatch"
    if harness == "claude":
        if local:
            if "anthropic" in protocols:
                return "experimental", None
            return "blocked", "protocol_mismatch"
        if base in ("anthropic", "openai"):
            return "blocked", "unproven"
        return "blocked", "protocol_mismatch"
    if harness == "openclaude":
        return ("blocked", "unproven") if base == "openai" else ("blocked", "protocol_mismatch")
    return "blocked", "harness_not_supported"


async def _members(session: AsyncSession, runtimes: list[Runtime]) -> dict:
    if not runtimes:
        return {}
    rows = (
        await session.exec(select(RuntimeHost).where(RuntimeHost.runtime_id.in_([r.id for r in runtimes])))
    ).all()
    members: dict = {}
    for row in rows:
        members.setdefault(row.runtime_id, []).append(row)
    return members


async def box_keys_for(session: AsyncSession, runtime: Runtime) -> list[str]:
    members = await _members(session, [runtime])
    return [str(h) for h in runtime_host_ids(runtime, members)]


def _dedupe_local(runtimes: list[Runtime], served: dict[str, frozenset | None]) -> list[Runtime]:
    """Several rows can point at one engine (slot row + recipe rows). Keep one
    row per (endpoint, model) — the slot row when present (it follows the box)."""
    chosen: dict[tuple[str, str], Runtime] = {}
    for rt in runtimes:
        key = (rt.endpoint, rt.model_identifier or "")
        prev = chosen.get(key)
        if prev is None or (rt.is_slot and not prev.is_slot):
            chosen[key] = rt
    return sorted(chosen.values(), key=lambda r: (r.ui_order, r.slug))


async def list_pairs(session: AsyncSession, occupancy: dict | None = None) -> dict:
    """All offered pairs + the default pair (always the best LOCAL pair)."""
    runtimes = [
        rt
        for rt in (await session.exec(select(Runtime).where(Runtime.enabled == True))).all()  # noqa: E712
        if runtime_protocol(rt) in ("openai", "anthropic") and rt.model_identifier
    ]
    local = [rt for rt in runtimes if is_local(rt)]
    cloud = [rt for rt in runtimes if not is_local(rt)]

    endpoints = sorted({rt.endpoint for rt in local})
    served_list = await asyncio.gather(*(engine.served_models(ep) for ep in endpoints))
    served = dict(zip(endpoints, served_list))
    busy_list = await asyncio.gather(*(engine.running_requests(ep) for ep in endpoints))
    in_use = {ep: bool(n) for ep, n in zip(endpoints, busy_list)}
    local = _dedupe_local(local, served)

    protocols_list = await asyncio.gather(*(runtime_protocols(rt) for rt in local))
    protocols = {rt.slug: p for rt, p in zip(local, protocols_list)}
    members = await _members(session, local)
    occupancy = occupancy or {}

    pairs: list[Pair] = []
    for rt in local:
        models = served.get(rt.endpoint)
        live = models is not None and (rt.model_identifier in models)
        keys = [str(h) for h in runtime_host_ids(rt, members)]
        busy = next((occupancy[k] for k in keys if k in occupancy), None)
        for harness in OFFERED_HARNESSES:
            status, reason = pair_status(harness, rt, protocols.get(rt.slug, set()))
            if status != "blocked":
                if not live:
                    status, reason = "blocked", "engine_not_ready"
                elif busy is not None:
                    status, reason = "blocked", "box_busy"
            pairs.append(Pair(
                harness=harness, harness_label=LABELS.get(harness, harness),
                runtime_slug=rt.slug, runtime_label=rt.display_name, model=rt.model_identifier,
                locality="local", status=status, reason_code=reason, live=live, box_keys=keys,
                busy_by=busy, engine_in_use=in_use.get(rt.endpoint, False),
            ))
    for rt in sorted(cloud, key=lambda r: (r.ui_order, r.slug)):
        for harness in OFFERED_HARNESSES:
            status, reason = pair_status(harness, rt, {runtime_protocol(rt) or ""})
            pairs.append(Pair(
                harness=harness, harness_label=LABELS.get(harness, harness),
                runtime_slug=rt.slug, runtime_label=rt.display_name, model=rt.model_identifier,
                locality="cloud", status=status, reason_code=reason, live=True, box_keys=[],
            ))

    default = default_pair(pairs)
    return {"pairs": [p.to_dict() for p in pairs], "default_pair": default.to_dict() if default else None}


def default_pair(pairs: list[Pair]) -> Pair | None:
    """Always omp × the best local runtime — live first, then catalogue order.
    Never a cloud pair, not even when no local engine is running: then the
    default stays local with startable=false (reason engine_not_ready)."""
    local_omp = [p for p in pairs if p.harness == DEFAULT_HARNESS and p.locality == "local"]
    if not local_omp:
        return None
    return sorted(local_omp, key=lambda p: (not p.live, p.status == "blocked"))[0]


async def resolve_pair(session: AsyncSession, harness: str, runtime_slug: str, occupancy: dict | None = None,
                       ignore_run_id: str | None = None) -> tuple[Pair | None, Runtime | None]:
    """The one pair a start request names (with live checks)."""
    runtime = (await session.exec(select(Runtime).where(Runtime.slug == runtime_slug))).first()
    if runtime is None or harness not in OFFERED_HARNESSES:
        return None, runtime
    occ = {k: v for k, v in (occupancy or {}).items() if v.get("run_id") != ignore_run_id}
    listing = await list_pairs(session, occ)
    for p in listing["pairs"]:
        if p["harness"] == harness and p["runtime_slug"] == runtime_slug:
            return Pair(**{k: v for k, v in p.items() if k != "startable"}), runtime
    # A local row hidden by the dedupe (same engine + model as the slot row).
    return None, runtime
