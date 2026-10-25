"""Bench W1 plan builder — what MC already knows, so Mark types nothing.

``GET /bench/plan``, ``GET /bench/contestants`` and ``GET /bench/runtime-
summary`` all read ONE view of the fleet (:func:`load_views`):

- every enabled runtime that can be tested: OpenAI-protocol rows for the
  direct API path (class A), plus runtimes an agent harness is bound to
  (omp, Grok Build) for class B;
- local rows deduplicated per engine (``pairs._dedupe_local``) and only the
  row whose model the engine REALLY serves (a drifted recipe row that still
  names the previous model is dropped — "the box is the truth");
- served model id = the name on every artefact (gate R2), never
  ``agents.model`` (the "Rex trap", P3 §2);
- device display name, nodes and GPU mode from the host rows — never an IP,
  slug or host name (§8);
- engine / quant / spec decoding from the recipe; no recipe = unknown;
- effort chips from the STORED probe (per runtime + serving_since): this
  module never probes and never sends a completion (read-only GETs).
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlsplit

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.bench import BenchChallenge, BenchEntry, BenchProbe
from app.models.host import Host
from app.models.local_recipe import LocalRecipe
from app.models.runtime import Runtime
from app.models.runtime_host import RuntimeHost

from . import w1_common as c

logger = logging.getLogger("mc.bench_studio")

#: Agent harness (agents.harness) -> W1 harness name. D4/D5: omp (local
#: agent) and Grok Build (cloud agent) in W1; Claude contestants only from
#: W2.1 (heads, single runs Mark taps himself); hermes not decided.
AGENT_HARNESSES = {"omp": "omp", "grok": "grok-build"}

#: Harnesses whose task session starts on a known thinking level the bench
#: can name before the run (doc 06: a bench omp session starts on "high");
#: the observed level of the last run wins (W0.7).
SESSION_EFFORT_NOTES = {"omp": ("omp_session_default", "high")}

#: Known API providers by endpoint domain -> (display name, cost kind when
#: no role tag says otherwise). Ollama Cloud runs through the operator's
#: subscription (D4: "über dein Ollama-Abo").
_PROVIDERS: tuple[tuple[str, str, str | None], ...] = (
    ("ollama.com", "Ollama Cloud", "subscription"),
    ("x.ai", "xAI", None),
    ("openai.com", "OpenAI", None),
    ("deepseek.com", "DeepSeek", None),
    ("openrouter.ai", "OpenRouter", None),
    ("together.xyz", "Together", None),
    ("fireworks.ai", "Fireworks", None),
    ("groq.com", "Groq", None),
    ("mistral.ai", "Mistral", None),
    ("googleapis.com", "Google", None),
    ("moonshot.ai", "Moonshot", None),
    ("moonshot.cn", "Moonshot", None),
    ("anthropic.com", "Anthropic", None),
)

#: Operator overrides on runtimes.role_tags (no schema change needed).
TAG_SUBSCRIPTION = "bench:subscription"
TAG_API_PRICE = "bench:api_price"

#: Fallback estimates when a runtime has no history yet (seconds per run).
DEFAULT_A_RUN_S = (45, 150)
DEFAULT_B_RUN_S = (300, 3600)
#: Token guesses for the hypothetical API price without history.
DEFAULT_TOKENS_IN = 400
DEFAULT_TOKENS_OUT = 6600

_SPEC_TAGS = ("dflash", "mtp", "eagle3", "eagle", "medusa", "ngram", "speculative")


# ── Live lookups (patched in tests; the conftest keeps engines offline) ──

#: Seconds one box may take to say what it serves. On the LAN an engine
#: answers /models in well under a second; a sleeping box never answers, so
#: a long wait only delays the sheet.
LIVE_TIMEOUT_S = 2.0
#: How long an answer stands. The sheet re-reads the plan on every preset or
#: prompt tap — a sleeping box must cost its timeout once, not on every tap.
#: A cached "down" expires like any other answer, so a woken box shows up
#: within this window (the same 20 s the engine's own /models cache keeps).
SERVED_TTL_S = 20.0
#: Load changes fast (a run starts or ends); only a short window.
IN_USE_TTL_S = 5.0

_live_cache: dict[tuple[str, str], tuple[float, Any]] = {}


def clear_live_cache() -> None:
    _live_cache.clear()


async def _cached(kind: str, endpoint: str, ttl: float, ask) -> Any:
    """``ask()`` once per ``ttl`` per endpoint; a timeout or error is the
    answer None and is cached too (that is the whole point: the dead box)."""
    hit = _live_cache.get((kind, endpoint))
    if hit is not None and time.monotonic() - hit[0] < ttl:
        return hit[1]
    try:
        value = await asyncio.wait_for(ask(), timeout=LIVE_TIMEOUT_S)
    except Exception:  # noqa: BLE001 — an unreachable box is a state, not an error
        value = None
    _live_cache[(kind, endpoint)] = (time.monotonic(), value)
    return value


async def _served(endpoint: str) -> frozenset[str] | None:
    from app.services.heads import engine

    return await _cached("served", endpoint, SERVED_TTL_S, lambda: engine.served_models(endpoint))


async def _in_use(endpoint: str) -> bool | None:
    from app.services.heads import engine

    n = await _cached("in_use", endpoint, IN_USE_TTL_S, lambda: engine.running_requests(endpoint))
    return None if n is None else n > 0


# ── The fleet view ────────────────────────────────────────────────────────


@dataclass
class EngineView:
    runtime: Runtime
    row_ids: list[uuid.UUID]
    locality: str  # local | cloud
    protocol: str | None
    served_model: str | None
    reachable: bool
    in_use: bool | None
    host: Host | None = None
    nodes: int | None = None
    device_name: str | None = None
    gpu_mode: str | None = None
    recipe: LocalRecipe | None = None
    provider: str | None = None
    has_key: bool = False
    agents: list[Agent] = field(default_factory=list)
    bench_running: bool = False

    @property
    def key(self) -> str:
        return str(self.runtime.id)

    @property
    def serving_since(self) -> datetime | None:
        return c.as_aware(self.runtime.serving_since)

    @property
    def power_managed(self) -> bool:
        return bool(self.runtime.power_managed or (self.host is not None and self.host.power_managed))

    @property
    def engine(self) -> str | None:
        return self.recipe.engine if self.recipe is not None else None

    @property
    def quant(self) -> str | None:
        return self.recipe.quant if self.recipe is not None else None

    @property
    def spec_decoding(self) -> str | None:
        """From the recipe only — without one it is unknown, never "off"."""
        if self.recipe is None:
            return None
        hay = " ".join(
            [*(self.recipe.tags or []), self.recipe.slug or "", self.recipe.launch_template or ""]
        ).lower()
        for tag in _SPEC_TAGS:
            if tag in hay:
                return "on" if tag == "speculative" else tag
        return "off"


def _protocol(runtime: Runtime) -> str | None:
    from app.services.harness_compat import runtime_protocol

    return runtime_protocol(runtime)


def _is_local(runtime: Runtime) -> bool:
    from app.services.heads.pairs import is_local

    return is_local(runtime)


def provider_of(runtime: Runtime) -> tuple[str | None, str | None]:
    """(display name, default cost kind) of a cloud runtime's provider."""
    if (runtime.runtime_type or "").strip() == "grok":
        return "xAI", "subscription"
    try:
        host = (urlsplit(runtime.endpoint or "").hostname or "").lower()
    except ValueError:
        host = ""
    for domain, name, kind in _PROVIDERS:
        if host == domain or host.endswith("." + domain):
            return name, kind
    return None, None


_TRAILING_NO = re.compile(r"[\s#_-]+\d+$")  # "Spark 2", not the 10 in "GX10"
_PAREN = re.compile(r"\s*\([^)]*\)\s*")


def device_display_name(hosts: list[Host], nodes: int | None) -> str | None:
    """'DGX Spark ×2' — the head's display name (a trailing box number and
    any bracket note stripped when several boxes form one engine). Never the
    slug, SSH host or Tailscale name."""
    if not hosts:
        return None
    name = _PAREN.sub(" ", hosts[0].display_name or "").strip()
    if not name:
        return None
    if nodes and nodes > 1:
        return f"{_TRAILING_NO.sub('', name).strip() or name} ×{nodes}"
    return name


def _gpu_mode(host: Host | None) -> str | None:
    from app.services.device_state import GPU_MODES

    state = (host.agent_device_state or {}) if host is not None else {}
    mode = state.get("gpu_mode") if isinstance(state, dict) else None
    return mode if mode in GPU_MODES else None


def _recipe_for(rows: list[Runtime], recipes: list[LocalRecipe], served: str | None) -> LocalRecipe | None:
    """The recipe this engine runs: an explicit back-link on any of its rows,
    a ``recipe_ref`` in a row's launch command, or exactly one recipe for the
    served model. Ambiguous = unknown (printed as unknown, never guessed)."""
    by_slug = {r.slug: r for r in recipes}
    for rt in rows:
        topo = rt.topology if isinstance(rt.topology, dict) else {}
        slug = topo.get("recipe_slug")
        if slug and slug in by_slug:
            return by_slug[slug]
    for rt in rows:
        for rec in recipes:
            ref = (rec.recipe_ref or "").strip()
            if ref and rt.launch_command and ref in rt.launch_command:
                return rec
    if served:
        matches = [r for r in recipes if (r.model_identifier or "").strip().lower() == served.lower()]
        if len(matches) == 1:
            return matches[0]
    return None


async def load_views(session: AsyncSession, *, live: bool = True) -> list[EngineView]:
    """Every testable engine, local first (catalogue order), then cloud."""
    runtimes = [
        rt for rt in (await session.exec(select(Runtime).where(Runtime.enabled == True))).all()  # noqa: E712
        if rt.model_identifier and _protocol(rt) in ("openai", "grok")
    ]
    hosts = {h.id: h for h in (await session.exec(select(Host))).all()}
    members: dict[uuid.UUID, list[uuid.UUID]] = {}
    for m in (await session.exec(select(RuntimeHost))).all():
        members.setdefault(m.runtime_id, []).append(m.host_id)
    recipes = list((await session.exec(select(LocalRecipe))).all())
    agents = list((await session.exec(select(Agent))).all())
    running_runtime_ids = await _running_bench_runtime_ids(session)

    local_rows = [rt for rt in runtimes if _is_local(rt)]
    cloud_rows = [rt for rt in runtimes if not _is_local(rt)]

    by_endpoint: dict[str, list[Runtime]] = {}
    for rt in local_rows:
        by_endpoint.setdefault(rt.endpoint, []).append(rt)
    endpoints = sorted(by_endpoint)
    if live:
        served_list = await asyncio.gather(*(_served(ep) for ep in endpoints))

        async def _busy(ep: str, served: frozenset[str] | None) -> bool | None:
            # A box that did not answer /models is not asked again for its
            # load: a sleeping box would add its timeout to every sheet.
            return await _in_use(ep) if served is not None else None

        in_use_list = await asyncio.gather(*(_busy(ep, sv) for ep, sv in zip(endpoints, served_list)))
    else:
        served_list = [None] * len(endpoints)
        in_use_list = [None] * len(endpoints)

    views: list[EngineView] = []
    for ep, served, in_use in zip(endpoints, served_list, in_use_list):
        rows = sorted(by_endpoint[ep], key=lambda r: (not r.is_slot, r.ui_order, r.slug))
        chosen: list[tuple[Runtime, str | None]] = []
        if served:
            live_rows = [r for r in rows if r.model_identifier in served]
            if live_rows:
                from app.services.heads.pairs import _dedupe_local

                for rt in _dedupe_local(live_rows, {ep: served}):
                    chosen.append((rt, rt.model_identifier))
            elif len(served) == 1:
                # The engine serves a model no row names (no slot row, a
                # recipe started by hand): the box is the truth.
                chosen.append((rows[0], next(iter(served))))
            else:
                chosen.append((rows[0], None))
        else:
            chosen.append((rows[0], None))
        for rt, model in chosen:
            same_engine = [r for r in rows if model is None or r.model_identifier == model or r.is_slot]
            row_ids = [r.id for r in same_engine] or [rt.id]
            host_ids: list[uuid.UUID] = []
            for r in same_engine or [rt]:
                for hid in ([r.host_id] if r.host_id else []) + members.get(r.id, []):
                    if hid not in host_ids:
                        host_ids.append(hid)
            recipe = _recipe_for(same_engine or [rt], recipes, model)
            nodes = len(host_ids) or None
            if recipe is not None and isinstance(recipe.topology, dict):
                rn = recipe.topology.get("nodes")
                if isinstance(rn, int) and rn > (nodes or 0):
                    nodes = rn
            head = hosts.get(rt.host_id) if rt.host_id else None
            box_hosts = [hosts[h] for h in host_ids if h in hosts]
            views.append(EngineView(
                runtime=rt,
                row_ids=row_ids,
                locality="local",
                protocol=_protocol(rt),
                served_model=model,
                reachable=served is not None,
                in_use=in_use,
                host=head,
                nodes=nodes,
                device_name=device_display_name(box_hosts or ([head] if head else []), nodes),
                gpu_mode=_gpu_mode(head),
                recipe=recipe,
                agents=[a for a in agents if a.runtime_id in row_ids],
                bench_running=any(i in running_runtime_ids for i in row_ids),
            ))
    views.sort(key=lambda v: (v.runtime.ui_order, v.runtime.slug))

    cloud: list[EngineView] = []
    for rt in sorted(cloud_rows, key=lambda r: (r.ui_order, r.slug)):
        provider, _kind = provider_of(rt)
        cloud.append(EngineView(
            runtime=rt,
            row_ids=[rt.id],
            locality="cloud",
            protocol=_protocol(rt),
            served_model=rt.model_identifier,
            reachable=True,
            in_use=None,
            provider=provider,
            has_key=rt.api_key_secret_id is not None,
            agents=[a for a in agents if a.runtime_id == rt.id],
            bench_running=rt.id in running_runtime_ids,
        ))
    return views + cloud


async def _running_bench_runtime_ids(session: AsyncSession) -> set[uuid.UUID]:
    """Runtimes a W1 test is generating on right now (a test never shares a
    box with a second one — the runner is serial, this only feeds the
    "box busy" hint)."""
    rows = (
        await session.exec(
            select(BenchEntry.runtime_id)
            .join(BenchChallenge, BenchEntry.challenge_id == BenchChallenge.id)  # type: ignore[arg-type]
            .where(BenchEntry.status == "generating", BenchChallenge.status == "generating")
        )
    ).all()
    return {r for r in rows if r is not None}


def find_view(views: list[EngineView], runtime_id: uuid.UUID | None) -> EngineView | None:
    if runtime_id is None:
        return None
    for v in views:
        if v.runtime.id == runtime_id:
            return v
    for v in views:
        if runtime_id in v.row_ids:
            return v
    return None


def subject_view(views: list[EngineView]) -> EngineView | None:
    """No runtime given: the most recently started LIVE local runtime, else
    the first live cloud one (the QuickSheet "Modell testen" path)."""
    live_local = [v for v in views if v.locality == "local" and v.served_model and v.serving_since]
    if live_local:
        return max(live_local, key=lambda v: v.serving_since)
    live_any = [v for v in views if v.served_model and api_capable(v)]
    if live_any:
        return live_any[0]
    return views[0] if views else None


def api_capable(view: EngineView) -> bool:
    return view.protocol == "openai"


# ── Blockers ──────────────────────────────────────────────────────────────


def blockers_for(view: EngineView, now: datetime | None = None) -> list[dict[str, Any]]:
    """Why a contestant cannot start NOW. ``blocking`` False = the test still
    starts and waits (concept §3: "Box belegt → startet sobald frei",
    "Modell läuft erst 6 min → startet 20:27")."""
    now = now or c.now_utc()
    out: list[dict[str, Any]] = []
    if view.locality == "local":
        if not view.reachable:
            code = "box_asleep" if view.power_managed else "engine_down"
            return [{"code": code, "blocking": True}]
        if not view.served_model:
            return [{"code": "no_model", "blocking": True}]
        since = view.serving_since
        if since is None or now - since < timedelta(minutes=c.FRESH_MIN):
            until = since + timedelta(minutes=c.FRESH_MIN) if since else None
            out.append({"code": "model_fresh", "blocking": False, "until": until})
        if view.in_use or view.bench_running or any(a.current_task_id for a in view.agents):
            out.append({"code": "box_busy", "blocking": False})
        return out
    if view.protocol == "openai" and not view.has_key:
        if not _agent_options(view):
            return [{"code": "no_api_key", "blocking": True}]
    if view.protocol != "openai" and not _agent_options(view):
        return [{"code": "protocol_mismatch", "blocking": True}]
    return out


def device_state(view: EngineView, blockers: list[dict[str, Any]]) -> str:
    codes = {b["code"] for b in blockers}
    if "box_asleep" in codes:
        return "asleep"
    if codes & {"engine_down", "no_model"}:
        return "offline"
    if "box_busy" in codes:
        return "busy"
    return "free"


# ── Effort (stored probe only) ───────────────────────────────────────────


async def stored_probe(session: AsyncSession, view: EngineView) -> BenchProbe | None:
    from . import effort_probe

    if not view.served_model:
        return None
    ttl = effort_probe.CLOUD_PROBE_TTL if view.locality == "cloud" else effort_probe.UNPINNED_PROBE_TTL
    return await effort_probe.stored_probe_for(
        session, view.runtime.id, view.served_model, view.runtime.serving_since, ttl=ttl
    )


def effort_view(probe: BenchProbe | None) -> dict[str, Any]:
    """The chips as the probe really found them (critic 32 S3): ok · only
    on/off · no effect (default only) · check failed (default only, effort
    unknown) · unchecked. The server default is merged into the level it
    maps to ("high (default)")."""
    default = {"level": "default", "is_default": True}
    if probe is None:
        return {"status": "unchecked", "options": [default], "default_observed": "unknown"}
    status = probe.status
    if status == "ok" and probe.effort_param is None:
        status = "only_on_off"
    merged = (probe.raw or {}).get("default_level") if status == "ok" else None
    if merged:
        default["merged_level"] = merged
    options = [default]
    if status in ("ok", "only_on_off"):
        options += [{"level": lv} for lv in (probe.levels or []) if lv != merged]
    return {
        "status": status,
        "options": options,
        "probed_at": probe.probed_at,
        "default_observed": probe.default_observed if probe.default_observed in ("on", "off") else "unknown",
    }


def offered_levels(effort: dict[str, Any]) -> list[str]:
    return [o["level"] for o in effort.get("options", [])]


# ── History (estimates + token guesses) ──────────────────────────────────


@dataclass
class History:
    a_run_s: list[float] = field(default_factory=list)
    a_tokens_in: list[float] = field(default_factory=list)
    a_tokens_out: list[float] = field(default_factory=list)
    a_decode: list[float] = field(default_factory=list)
    b_run_s: dict[str, list[float]] = field(default_factory=dict)
    b_level: dict[str, str] = field(default_factory=dict)


async def load_history(session: AsyncSession, views: list[EngineView]) -> dict[str, History]:
    """Past runs per engine view: class A per served model (W0 rows carry
    no runtime_id), class B per harness on the same engine."""
    entries = (
        await session.exec(
            select(BenchEntry)
            .where(BenchEntry.status.in_(("generated", "rendered")))  # type: ignore[union-attr]
            .order_by(BenchEntry.id)
            .limit(2000)
        )
    ).all()
    agent_harness = {a.id: a.harness for a in (await session.exec(select(Agent))).all()}
    out: dict[str, History] = {}
    for v in views:
        h = History()
        model = (v.served_model or "").lower()
        for e in entries:
            m = e.metrics or {}
            same_rt = e.runtime_id is not None and e.runtime_id in v.row_ids
            if (e.run_class or ("A" if e.source_kind in ("spark", "api") else "B")) == "A":
                served = str(m.get("model") or e.spark_model or "").lower()
                if not (same_rt or (model and served == model)):
                    continue
                ms = m.get("gen_ms") or m.get("duration_ms")
                if ms:
                    h.a_run_s.append(ms / 1000)
                if m.get("tokens_in") is not None:
                    h.a_tokens_in.append(m["tokens_in"])
                if m.get("tokens_out") is not None:
                    h.a_tokens_out.append(m["tokens_out"])
                if m.get("decode_tok_s"):
                    h.a_decode.append(m["decode_tok_s"])
            else:
                if not (same_rt or (e.agent_id is not None and any(a.id == e.agent_id for a in v.agents))):
                    continue
                harness = e.harness or AGENT_HARNESSES.get(agent_harness.get(e.agent_id) or "", "")
                if not harness:
                    continue
                ms = m.get("active_ms") or m.get("duration_ms")
                if ms:
                    h.b_run_s.setdefault(harness, []).append(ms / 1000)
                obs = m.get("effort_observed") or {}
                if isinstance(obs, dict) and obs.get("level"):
                    h.b_level[harness] = obs["level"]
        out[v.key] = h
    return out


def estimate_a(hist: History, n_runs: int) -> dict[str, Any]:
    if hist.a_run_s:
        lo, hi = min(hist.a_run_s), max(hist.a_run_s)
        return {"min_s": int(lo * n_runs), "max_s": int(round(hi * n_runs + 0.5)), "basis": "history"}
    lo, hi = DEFAULT_A_RUN_S
    return {"min_s": lo * n_runs, "max_s": hi * n_runs, "basis": "default"}


def estimate_a_effort(hist: History, n_runs: int, level: str, context_limit: int | None) -> dict[str, Any]:
    """Deep levels can think until max_tokens (critic 32 S4): the upper end
    then comes from the measured decode rate and the scaled budget."""
    from app.services import reasoning_params

    base = estimate_a(hist, n_runs)
    if level in ("high", "max", "on") and hist.a_decode:
        budget = reasoning_params.max_tokens_for(level, c.BASE_MAX_TOKENS, context_limit)
        worst = budget / max(1.0, c.median(hist.a_decode) or 1.0)
        return {"min_s": base["min_s"], "max_s": int(max(base["max_s"], worst * n_runs)), "basis": "probe"}
    return base


def estimate_b(hist: History, harness: str) -> dict[str, Any]:
    runs = hist.b_run_s.get(harness) or []
    if runs:
        return {"min_s": int(min(runs)), "max_s": int(round(max(runs) + 0.5)), "basis": "history"}
    lo, hi = DEFAULT_B_RUN_S
    return {"min_s": lo, "max_s": hi, "basis": "default"}


def sum_estimates(parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not parts:
        return {"min_s": None, "max_s": None, "basis": "default"}
    basis = "history" if all(p.get("basis") == "history" for p in parts) else (
        "probe" if any(p.get("basis") == "probe" for p in parts) else "default"
    )
    return {
        "min_s": sum(p.get("min_s") or 0 for p in parts),
        "max_s": sum(p.get("max_s") or 0 for p in parts),
        "basis": basis,
    }


# ── Money (D3/D4) ─────────────────────────────────────────────────────────


@dataclass
class PriceInfo:
    input_per_mtok: float
    output_per_mtok: float
    source: str
    date: str | None


async def load_prices(session: AsyncSession) -> list[Any]:
    from app.models.model_usage import ModelPrice

    return list((await session.exec(select(ModelPrice))).all())


def price_for(model: str | None, prices: list[Any], now: datetime | None = None) -> PriceInfo | None:
    """The API list price of ``model`` — None for the catch-all ``*`` row or
    a 0/0 row (that is "price unknown", never "$0.00")."""
    import fnmatch

    if not model:
        return None
    now = now or c.now_utc()
    candidates = []
    for p in prices:
        vf = c.as_aware(p.valid_from)
        if vf is not None and vf > now:
            continue
        if fnmatch.fnmatch(model, p.model_pattern) or fnmatch.fnmatch(model.lower(), p.model_pattern.lower()):
            candidates.append(p)
    if not candidates:
        return None
    candidates.sort(key=lambda p: (p.priority, c.as_aware(p.valid_from)), reverse=True)
    best = candidates[0]
    if best.model_pattern.strip() in ("*", "**") or (not best.input_per_mtok and not best.output_per_mtok):
        return None
    vf = c.as_aware(best.valid_from)
    return PriceInfo(
        input_per_mtok=best.input_per_mtok,
        output_per_mtok=best.output_per_mtok,
        source=best.model_pattern,
        date=vf.date().isoformat() if vf else None,
    )


def cost_kind_of(view: EngineView, price: PriceInfo | None) -> str:
    if view.locality == "local":
        return "local"
    tags = set(view.runtime.role_tags or [])
    if TAG_SUBSCRIPTION in tags:
        return "subscription"
    if TAG_API_PRICE in tags:
        return "api_price" if price is not None else "unknown"
    _name, kind = provider_of(view.runtime)
    if kind == "subscription":
        return "subscription"
    return "api_price" if price is not None else "unknown"


def money_for(
    view: EngineView,
    prices: list[Any],
    hist: History,
    *,
    n_runs: int,
    run_class: str,
    max_tokens: int = c.BASE_MAX_TOKENS,
) -> dict[str, Any]:
    """What one contestant costs. local/subscription: no money, the API list
    price only as a small comparison (D3). api_price: real money, quoted as
    the worst case (every run up to its token budget) so the confirmed
    amount is never too low (D4)."""
    price = price_for(view.served_model, prices)
    kind = cost_kind_of(view, price)
    tok_in = c.median(hist.a_tokens_in) or DEFAULT_TOKENS_IN
    tok_out = c.median(hist.a_tokens_out) or DEFAULT_TOKENS_OUT
    money: dict[str, Any] = {"cost_kind": kind}
    if price is not None:
        money["price_source"] = price.source
        money["price_date"] = price.date
        expected = n_runs * (tok_in * price.input_per_mtok + tok_out * price.output_per_mtok) / 1e6
        if kind in ("local", "subscription"):
            money["hypothetical_usd"] = round(expected, 4)
        elif kind == "api_price":
            if run_class == "B":
                money["usd"] = round(expected, 4)
            else:
                worst = n_runs * (tok_in * price.input_per_mtok + max_tokens * price.output_per_mtok) / 1e6
                money["usd"] = round(max(expected, worst), 4)
            money["needs_confirmation"] = True
            money["second_confirmation"] = money["usd"] > c.COST_CAP_USD
    elif kind == "unknown":
        money["reason"] = {"code": "price_unknown", "params": {}}
    return money


def total_money(parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not parts:
        return {"cost_kind": "local"}
    kinds = [p["cost_kind"] for p in parts]
    kind = next((k for k in ("api_price", "unknown", "subscription") if k in kinds), "local")
    out: dict[str, Any] = {"cost_kind": kind}
    usd = [p["usd"] for p in parts if p.get("usd") is not None]
    hyp = [p["hypothetical_usd"] for p in parts if p.get("hypothetical_usd") is not None]
    if kind == "api_price":
        out["usd"] = round(sum(usd), 4)
        out["needs_confirmation"] = True
        out["second_confirmation"] = out["usd"] > c.COST_CAP_USD
    if hyp:
        out["hypothetical_usd"] = round(sum(hyp), 4)
    src = next((p for p in parts if p.get("price_source")), None)
    if src:
        out["price_source"] = src["price_source"]
        out["price_date"] = src.get("price_date")
    reason = next((p["reason"] for p in parts if p.get("reason")), None)
    if kind == "unknown" and reason:
        out["reason"] = reason
    return out


# ── Harness options ──────────────────────────────────────────────────────


def _agent_options(view: EngineView) -> list[tuple[str, Agent]]:
    seen: dict[str, Agent] = {}
    for a in sorted(view.agents, key=lambda a: (a.operational_mode != "active", a.current_task_id is not None, a.name)):
        h = AGENT_HARNESSES.get((a.harness or "").strip())
        if h and h not in seen and a.board_id is not None:
            seen[h] = a
    return list(seen.items())


def agent_for(view: EngineView, harness: str) -> Agent | None:
    return dict(_agent_options(view)).get(harness)


def harness_options(view: EngineView, hist: History, n_runs: int, blockers: list[dict]) -> list[dict[str, Any]]:
    blocked = any(b["blocking"] for b in blockers)
    out: list[dict[str, Any]] = []
    if api_capable(view):
        api_ok = not blocked and (view.locality == "local" or view.has_key)
        opt: dict[str, Any] = {
            "harness": "api", "run_class": "A", "available": api_ok,
            "effort_settable": True, "n_runs": n_runs,
            "estimate": estimate_a(hist, n_runs),
        }
        if not api_ok:
            code = next((b["code"] for b in blockers if b["blocking"]), "no_api_key")
            opt["reason"] = {"code": code, "params": {}}
        out.append(opt)
    for harness, agent in _agent_options(view):
        opt = {
            "harness": harness, "run_class": "B", "available": True,
            "effort_settable": False, "n_runs": 1,
            "estimate": estimate_b(hist, harness),
        }
        note = SESSION_EFFORT_NOTES.get(harness)
        if note is not None:
            code, default_level = note
            opt["effort_note"] = {"code": code,
                                  "params": {"level": hist.b_level.get(harness, default_level)}}
        else:
            opt["effort_note"] = {"code": "harness_default", "params": {}}
        if agent.operational_mode != "active":
            opt["available"] = False
            opt["reason"] = {"code": "agent_paused", "params": {}}
        elif agent.current_task_id is not None:
            opt["reason"] = {"code": "agent_busy", "params": {}}
        if view.locality == "local" and blocked:
            opt["available"] = False
            opt["reason"] = {"code": next(b["code"] for b in blockers if b["blocking"]), "params": {}}
        out.append(opt)
    return out


# ── Contestants ──────────────────────────────────────────────────────────


async def last_tests(session: AsyncSession) -> dict[uuid.UUID, dict[str, Any]]:
    """Newest finished, not archived test per runtime row id."""
    rows = (
        await session.exec(
            select(BenchEntry.runtime_id, BenchChallenge.id, BenchChallenge.title, BenchChallenge.created_at)
            .join(BenchChallenge, BenchEntry.challenge_id == BenchChallenge.id)  # type: ignore[arg-type]
            .where(BenchEntry.runtime_id.is_not(None))  # type: ignore[union-attr]
            .where(BenchChallenge.archived_at.is_(None))  # type: ignore[union-attr]
        )
    ).all()
    out: dict[uuid.UUID, dict[str, Any]] = {}
    for rt_id, cid, title, created in rows:
        prev = out.get(rt_id)
        if prev is None or c.as_aware(created) > c.as_aware(prev["created_at"]):
            out[rt_id] = {"id": cid, "title": title, "created_at": created}
    return out


def _last_for(view: EngineView, lt: dict[uuid.UUID, dict[str, Any]]) -> dict[str, Any] | None:
    found = [lt[i] for i in view.row_ids if i in lt]
    if not found:
        return None
    return max(found, key=lambda d: c.as_aware(d["created_at"]))


async def contestant(
    session: AsyncSession,
    view: EngineView,
    *,
    prices: list[Any],
    hist: History,
    n_runs: int,
    lt: dict[uuid.UUID, dict[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    blockers = blockers_for(view, now)
    effort = effort_view(await stored_probe(session, view))
    run_class = "A" if api_capable(view) else "B"
    d: dict[str, Any] = {
        "key": view.key,
        "runtime_id": view.runtime.id,
        "served_model_id": view.served_model,
        "display_name": view.served_model or view.runtime.model_identifier or "",
        "locality": view.locality,
        "provider": view.provider,
        "engine": view.engine,
        "quant": view.quant,
        "recipe_slug": view.recipe.slug if view.recipe is not None else None,
        "spec_decoding": view.spec_decoding,
        "serving_since": view.serving_since,
        "harnesses": harness_options(view, hist, n_runs, blockers),
        "effort": effort,
        "money": money_for(view, prices, hist, n_runs=n_runs if run_class == "A" else 1, run_class=run_class),
        "blockers": blockers,
        "cloud_build_unknown": view.locality == "cloud",
        "last_test": _last_for(view, lt or {}),
    }
    if view.locality == "local":
        d["device"] = {
            "name": view.device_name or "local box",
            "nodes": view.nodes,
            "gpu_mode": view.gpu_mode,
            "state": device_state(view, blockers),
        }
    return d


async def list_contestants(session: AsyncSession) -> dict[str, Any]:
    views = await load_views(session)
    prices = await load_prices(session)
    history = await load_history(session, views)
    lt = await last_tests(session)
    out: dict[str, list] = {"local": [], "cloud": []}
    for v in views:
        if not v.served_model and v.locality == "cloud":
            continue
        d = await contestant(session, v, prices=prices, hist=history[v.key], n_runs=c.PRESET_RUNS["standard"], lt=lt)
        out[v.locality].append(d)
    return out


# ── Plan ─────────────────────────────────────────────────────────────────


async def default_prompt(session: AsyncSession, template_id: uuid.UUID | None) -> dict[str, Any]:
    """The prompt of a test: the given template, else the signature prompt
    Pagoda Garden (operator default), else the oldest template, else the
    built-in Pagoda text. Returns {template_id, title, text, sha256}."""
    from app.models.prompt_template import PromptTemplate

    tpl = None
    if template_id is not None:
        tpl = await session.get(PromptTemplate, template_id)
        if tpl is None:
            from fastapi import HTTPException

            raise HTTPException(404, "Prompt template not found")
    if tpl is None:
        templates = (await session.exec(select(PromptTemplate).order_by(PromptTemplate.created_at))).all()
        tpl = next((t for t in templates if "pagoda" in (t.title or "").lower()), None) or (
            templates[0] if templates else None
        )
    if tpl is None:
        return {"template_id": None, "title": BUILTIN_PROMPT_TITLE, "text": BUILTIN_PROMPT,
                "sha256": c.sha256_text(BUILTIN_PROMPT)}
    return {"template_id": tpl.id, "title": tpl.title, "text": tpl.body, "sha256": c.sha256_text(tpl.body)}


BUILTIN_PROMPT_TITLE = "Pagoda Garden"
BUILTIN_PROMPT = (
    "Build a single self-contained index.html: an animated Japanese pagoda garden at dusk. "
    "A five-tier pagoda, a koi pond with ripples, drifting cherry blossom petals, stone "
    "lanterns that glow as night falls, and a slow camera drift. Use canvas or WebGL written "
    "by hand, no external libraries, no network requests. It must run smoothly for at least "
    "20 seconds without interaction."
)


def preset_view(preset: str, prompt: dict[str, Any], n_runs: int | None = None) -> dict[str, Any]:
    return {
        "id": preset,
        "n_runs": n_runs or c.PRESET_RUNS.get(preset, 3),
        "multi_effort": preset == "effort_ladder",
        "prompt": {k: prompt[k] for k in ("template_id", "title", "sha256")},
    }


def default_efforts(preset: str, effort: dict[str, Any]) -> list[str]:
    offered = offered_levels(effort)
    if preset != "effort_ladder":
        return ["default"]
    ladder = [lv for lv in ("off", "default", "max") if lv in offered]
    if len(ladder) < 2:
        ladder = offered[:3]
    return ladder or ["default"]


async def build_plan(
    session: AsyncSession,
    *,
    runtime_id: uuid.UUID | None,
    preset: str,
    template_id: uuid.UUID | None,
    rerun_of: uuid.UUID | None,
) -> dict[str, Any]:
    from fastapi import HTTPException

    views = await load_views(session)
    prices = await load_prices(session)
    history = await load_history(session, views)
    lt = await last_tests(session)

    old: BenchChallenge | None = None
    old_picks: list[dict[str, Any]] = []
    n_runs = c.PRESET_RUNS.get(preset, 3)
    if rerun_of is not None:
        old = await session.get(BenchChallenge, rerun_of)
        if old is None:
            raise HTTPException(404, "Test not found")
        preset = "rerun"
        old_plan = old.plan or {}
        old_picks = list(old_plan.get("picks") or [])
        n_runs = int(old_plan.get("n_runs") or c.PRESET_RUNS["standard"])
        prompt = {"template_id": old.prompt_template_id, "title": old.title,
                  "text": old.prompt_text, "sha256": old.prompt_sha256 or c.sha256_text(old.prompt_text)}
    else:
        prompt = await default_prompt(session, template_id)

    picked_views: list[EngineView] = []
    picks: list[dict[str, Any]] = []
    if old_picks:
        for p in old_picks:
            v = find_view(views, uuid.UUID(str(p["runtime_id"])))
            if v is not None and v not in picked_views:
                picked_views.append(v)
                picks.append({"runtime_id": v.runtime.id, "harness": p.get("harness") or "api",
                              "efforts": list(p.get("efforts") or ["default"])})
    if not picked_views:
        subject = find_view(views, runtime_id) if runtime_id else subject_view(views)
        if runtime_id is not None and subject is None:
            raise HTTPException(404, "Runtime not found or not testable")
        if subject is not None:
            picked_views.append(subject)

    contestants = []
    estimates: list[dict] = []
    moneys: list[dict] = []
    blockers: list[dict] = []
    for i, v in enumerate(picked_views):
        d = await contestant(session, v, prices=prices, hist=history[v.key], n_runs=n_runs, lt=lt)
        contestants.append(d)
        if i >= len(picks):
            harness = next((h["harness"] for h in d["harnesses"] if h["available"]),
                           d["harnesses"][0]["harness"] if d["harnesses"] else "api")
            picks.append({"runtime_id": v.runtime.id, "harness": harness,
                          "efforts": default_efforts(preset, d["effort"])})
        pick = picks[i]
        if pick["harness"] == "api":
            for level in pick["efforts"]:
                estimates.append(estimate_a_effort(history[v.key], n_runs, level, v.runtime.max_context_len))
            moneys.append(money_for(v, prices, history[v.key], n_runs=n_runs * len(pick["efforts"]),
                                    run_class="A"))
        else:
            estimates.append(estimate_b(history[v.key], pick["harness"]))
            moneys.append(money_for(v, prices, history[v.key], n_runs=1, run_class="B"))
        for b in d["blockers"]:
            if b not in blockers:
                blockers.append(b)

    classes = {("A" if p["harness"] == "api" else "B") for p in picks}
    presets = [preset_view(p, prompt) for p in ("standard", "quick", "effort_ladder")]
    if old is not None:
        presets.append(preset_view("rerun", prompt, n_runs))
    return {
        "preset": preset_view(preset, prompt, n_runs),
        "presets": presets,
        "contestants": contestants,
        "picks": picks,
        "estimate": sum_estimates(estimates),
        "money": total_money(moneys),
        "blockers": blockers,
        "sequential": classes == {"A", "B"},
        "video_run_index": c.VIDEO_RUN_INDEX,
        "max_video_contestants": c.MAX_VIDEO_CONTESTANTS,
    }


# ── Runtime summary (Runtimes cards, P1) ─────────────────────────────────


async def runtime_summary(session: AsyncSession) -> list[dict[str, Any]]:
    """One row per enabled runtime the Runtimes page shows. A recipe row
    that is the twin of the live slot row is testable too (same engine)."""
    views = await load_views(session)
    lt = await last_tests(session)
    running = await _running_tests_by_runtime(session)
    planned = await _planned_auto_tests(session)
    by_row: dict[uuid.UUID, EngineView] = {}
    for v in views:
        for rid in v.row_ids:
            by_row.setdefault(rid, v)
    live_local = [v for v in views if v.locality == "local" and v.served_model and v.serving_since]
    primary_view = max(live_local, key=lambda v: v.serving_since) if live_local else None
    all_rows = (await session.exec(select(Runtime).where(Runtime.enabled == True))).all()  # noqa: E712
    out: list[dict[str, Any]] = []
    for rt in sorted(all_rows, key=lambda r: (r.ui_order, r.slug)):
        v = by_row.get(rt.id)
        row: dict[str, Any] = {"runtime_id": rt.id, "testable": False}
        if v is None:
            row["reason"] = {"code": "no_model" if _protocol(rt) in ("openai", "grok") else "protocol_mismatch",
                             "params": {}}
        else:
            blockers = blockers_for(v)
            hard = next((b for b in blockers if b["blocking"]), None)
            if hard is not None:
                row["reason"] = {"code": hard["code"], "params": {}}
            elif v.locality == "local" and rt.model_identifier != v.served_model and not rt.is_slot:
                row["reason"] = {"code": "no_model", "params": {}}
            else:
                row["testable"] = True
                row["primary"] = primary_view is not None and v is primary_view and rt.id == v.runtime.id
            row["last_test"] = _last_for(v, lt)
            row["running_test_id"] = next((running[i] for i in v.row_ids if i in running), None)
        host_id = rt.host_id or (v.runtime.host_id if v is not None else None)
        plan = planned.get(str(host_id)) if host_id else None
        if plan is not None and not row.get("running_test_id"):
            row["planned_test"] = plan
        out.append(row)
    return out


async def _planned_auto_tests(session: AsyncSession) -> dict[str, dict[str, Any]]:
    """Waiting auto tests that have no runs yet (the switched model is still
    loading), by host id — the Runtimes card shows them as planned."""
    rows = (
        await session.exec(
            select(BenchChallenge).where(BenchChallenge.status == "waiting",
                                         BenchChallenge.trigger == "auto_switch",
                                         BenchChallenge.protocol_version == c.PROTOCOL_V1)
        )
    ).all()
    out: dict[str, dict[str, Any]] = {}
    for ch in rows:
        plan = ch.plan or {}
        host_id = (plan.get("auto") or {}).get("host_id")
        if not host_id:
            continue
        nb = plan.get("not_before")
        out[host_id] = {"id": ch.id, "not_before": c.as_aware(datetime.fromisoformat(nb)) if nb else None}
    return out


async def _running_tests_by_runtime(session: AsyncSession) -> dict[uuid.UUID, uuid.UUID]:
    rows = (
        await session.exec(
            select(BenchEntry.runtime_id, BenchChallenge.id)
            .join(BenchChallenge, BenchEntry.challenge_id == BenchChallenge.id)  # type: ignore[arg-type]
            .where(BenchChallenge.status.in_(("waiting", "generating", "rendering", "composing")))  # type: ignore[union-attr]
            .where(BenchChallenge.protocol_version == c.PROTOCOL_V1)
            .where(BenchEntry.runtime_id.is_not(None))  # type: ignore[union-attr]
        )
    ).all()
    return {rt: cid for rt, cid in rows}
