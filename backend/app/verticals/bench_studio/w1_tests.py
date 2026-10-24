"""Bench W1 tests — create, read model (detail + list), rerun, stop, harvest,
auto test after a recipe switch.

A test is a ``bench_challenges`` row (protocol v1) with one ``bench_entries``
row per RUN: a class A contestant has n entries (repeat 1..n, same
``position``), a class B contestant one. The frozen start plan
(``challenge.plan``) holds one record per contestant (position-indexed): the
public names, device, recipe facts, harness, effort, token budget, money —
what the receipt, the card and the settings text print later, as it was at
start, not as the fleet looks today.

States Mark sees (concept §3): waiting · running · done · done – numbers
missing · stopped (reason). Pre-W1 (v0) challenges are read through the
same shape with their unknowns spelled out.
"""
from __future__ import annotations

import logging
import uuid
from datetime import timedelta
from typing import Any

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry

from . import w1_common as c
from . import w1_kit, w1_plan, w1_receipt

logger = logging.getLogger("mc.bench_studio")

LIVE = ("waiting", "generating", "rendering", "composing")
DONE = ("review", "drafted", "published")
#: Abort codes (challenge.error on v1) + the legacy texts they replace.
ABORT_CODES = ("stopped_by_operator", "task_deleted", "superseded_by_switch", "switch_failed",
               "all_runs_failed", "backend_restart")
_LEGACY_ABORT = {
    "stopped by operator": "stopped_by_operator",
    "all entries failed during generation": "all_runs_failed",
    "all entries failed during rendering": "all_runs_failed",
}
#: Aborts that need no look from the operator (he did it, or a newer
#: switch replaced the test).
_QUIET_ABORTS = ("stopped_by_operator", "superseded_by_switch")


def is_v1(challenge: BenchChallenge) -> bool:
    return (challenge.protocol_version or "v0") == c.PROTOCOL_V1


async def entries_of(session: AsyncSession, challenge_id: uuid.UUID) -> list[BenchEntry]:
    return list((await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == challenge_id))).all())


# ── Contestant plans ─────────────────────────────────────────────────────


def _legacy_pc(e: BenchEntry, position: int) -> dict[str, Any]:
    """A pre-W1 entry read as a contestant: what it recorded, the rest
    unknown. The name is the served model when it was recorded; otherwise
    the old free-text label (and gate R2 says so)."""
    m = e.metrics or {}
    run_class = e.run_class or ("A" if e.source_kind in ("spark", "api") else "B")
    served = m.get("served_model_id") or (m.get("model") if run_class == "A" else None) or (
        e.spark_model if run_class == "A" else None
    )
    return {
        "position": position,
        "key": f"{e.runtime_id or e.agent_id or e.id}:{e.position}",
        "runtime_id": e.runtime_id,
        "served_model_id": served,
        "display_name": served or e.model_label,
        "locality": m.get("locality") or ("local" if e.source_kind == "spark" else None),
        "harness": e.harness or ("api" if run_class == "A" else None),
        "run_class": run_class,
        "effort": e.effort_requested or "default",
        "n_runs": 1,
        "money": {"cost_kind": "local" if (m.get("locality") == "local" or e.source_kind == "spark") else "unknown"},
        "video": bool(e.video_path),
    }


def contestant_plans(challenge: BenchChallenge, entries: list[BenchEntry]) -> list[tuple[dict, list[BenchEntry]]]:
    by_pos: dict[int, list[BenchEntry]] = {}
    for e in entries:
        by_pos.setdefault(e.position or 0, []).append(e)
    for es in by_pos.values():
        es.sort(key=lambda e: (e.repeat_idx or 1))
    if is_v1(challenge) and (challenge.plan or {}).get("contestants"):
        out = []
        for pc in challenge.plan["contestants"]:
            out.append((pc, by_pos.get(pc["position"], [])))
        return out
    return [(_legacy_pc(es[0], pos), es) for pos, es in sorted(by_pos.items())]


# ── Read model ───────────────────────────────────────────────────────────

_RUN_STATE = {"pending": "waiting", "generating": "running", "generated": "done", "rendered": "done",
              "failed": "failed"}


def run_view(pc: dict[str, Any], e: BenchEntry, *, wait: dict | None) -> dict[str, Any]:
    m = e.metrics or {}
    state = _RUN_STATE.get(e.status, "waiting")
    phase = m.get("phase") or {"waiting": "queued", "running": "generating", "done": "done",
                               "failed": "done"}[state]
    if state == "done" and phase not in ("recording", "composing", "done"):
        phase = "done"
    view: dict[str, Any] = {
        "entry_id": e.id,
        "repeat_idx": e.repeat_idx or 1,
        "attempt_no": e.attempt_no or 1,
        "state": state,
        "phase": phase,
        "elapsed_ms": m.get("gen_ms") or m.get("active_ms") or m.get("elapsed_ms") or m.get("duration_ms"),
        "tokens_out": m.get("tokens_out") if m.get("tokens_out") is not None else m.get("tokens_so_far"),
        "ttft_ms": m.get("ttft_ms"),
        "finish_reason": m.get("finish_reason"),
        "truncated": bool(m.get("truncated")),
        "is_video_run": bool(pc.get("video")) and (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX,
        "_echoed_model": m.get("model") if pc.get("run_class") == "A" else None,
    }
    if pc.get("run_class") == "A" and m.get("decode_sane"):
        view["decode_tok_s"] = m.get("decode_tok_s")
    elif pc.get("run_class") == "A" and state == "running" and m.get("live_decode_tok_s"):
        view["decode_tok_s"] = m.get("live_decode_tok_s")
    if state == "waiting" and wait:
        view["wait"] = wait
    if state == "failed":
        view["error"] = {"code": m.get("error_code") or "run_failed", "params": {}}
    return view


def _abort_code(challenge: BenchChallenge) -> str:
    err = (challenge.error or "").strip()
    if err in ABORT_CODES:
        return err
    return _LEGACY_ABORT.get(err, "all_runs_failed")


def human_state(challenge: BenchChallenge, gate: dict, contestants: list[dict], truncation: dict | None):
    """(state, section, state_reason) — concept §3."""
    if challenge.status == "waiting":
        plan = challenge.plan or {}
        reason = {"code": challenge.wait_reason or "box_busy", "params": {}}
        if challenge.wait_reason in ("model_fresh",) and plan.get("not_before"):
            reason["params"]["until"] = plan["not_before"]
        return "waiting", "running", reason if challenge.wait_reason else None
    if challenge.status in ("generating", "rendering", "composing"):
        if challenge.wait_reason:
            params = {}
            if challenge.wait_reason == "model_fresh" and (challenge.plan or {}).get("not_before"):
                params["until"] = challenge.plan["not_before"]
            # The quiet-box guard holds the test before its first run: Mark
            # sees "Wartet (Grund)", not "Läuft".
            started = any(run["state"] != "waiting" for r in contestants for run in r["runs"])
            return ("running" if started else "waiting"), "running", {
                "code": challenge.wait_reason, "params": params}
        return "running", "running", None
    if challenge.status == "failed":
        code = _abort_code(challenge)
        section = "done" if code in _QUIET_ABORTS else "needs_look"
        return "aborted", section, {"code": code, "params": {}}
    missing = []
    for r in contestants:
        for field in ("time", "tokens_out"):
            if not r["summary"][field].get("measured") and field not in missing:
                missing.append(field)
    if missing:
        return "done_missing", "needs_look", {"code": "numbers_missing", "params": {"fields": missing}}
    if truncation:
        return "done", "needs_look", {"code": "runs_truncated",
                                      "params": {"runs_cut": truncation["runs_cut"], "n_runs": truncation["n_runs"]}}
    if gate.get("status") == "red":
        return "done", "needs_look", None
    return "done", "done", None


def _wait_for(challenge: BenchChallenge, pc: dict, pcs: list[dict]) -> dict | None:
    if challenge.wait_reason and challenge.status in ("waiting", "generating"):
        params: dict[str, Any] = {}
        if challenge.wait_reason == "model_fresh" and (challenge.plan or {}).get("not_before"):
            params["until"] = challenge.plan["not_before"]
        return {"code": challenge.wait_reason, "params": params}
    if pc.get("run_class") == "B" and any(p.get("run_class") == "A" for p in pcs):
        n = max((p.get("n_runs") or 1) for p in pcs if p.get("run_class") == "A")
        return {"code": "after_class_a", "params": {"after_run": n}}
    return None


async def test_detail(
    session: AsyncSession,
    challenge: BenchChallenge,
    *,
    entries: list[BenchEntry] | None = None,
    private: list[str] | None = None,
    with_actions: bool = True,
    finished: bool | None = None,
) -> dict[str, Any]:
    """``finished`` overrides the state check — the runner composes the
    artefacts from the FINAL record while the test still says "composing"."""
    entries = entries if entries is not None else await entries_of(session, challenge.id)
    pairs = contestant_plans(challenge, entries)
    pcs = [pc for pc, _ in pairs]
    if finished is None:
        finished = challenge.status in DONE or (
            challenge.status == "failed" and any(w1_receipt.done_runs(es) for _pc, es in pairs)
        )
    results: list[dict[str, Any]] = []
    receipts: list[tuple[dict, dict | None, str | None]] = []
    for pc, es in pairs:
        head = next((e for e in es if (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX), es[0] if es else None)
        summary = w1_receipt.contestant_summary(pc, es, finished=finished or challenge.status == "failed")
        rec, version, digest = w1_receipt.receipt_parts(head) if head else (None, None, None)
        if rec is None and finished and head is not None:
            rec = w1_receipt.build_receipt(challenge, pc, es)
            digest = c.checksum(rec)
        hm = (head.metrics or {}) if head else {}
        effort = {"requested": pc.get("effort") or "default"}
        if pc.get("effort_default_level"):
            effort["default_level"] = pc["effort_default_level"]
        if hm.get("effort_sent") is not None:
            effort["sent"] = hm["effort_sent"]
        if hm.get("effort_observed") is not None:
            effort["observed"] = hm["effort_observed"]
        wait = _wait_for(challenge, pc, pcs)
        result = {
            "key": pc.get("key") or str(pc.get("position")),
            "runtime_id": pc.get("runtime_id"),
            "served_model_id": pc.get("served_model_id"),
            "display_name": pc.get("display_name") or pc.get("served_model_id") or "",
            "alias": pc.get("alias"),
            "locality": pc.get("locality"),
            "device_name": pc.get("device_name"),
            "provider": pc.get("provider"),
            "harness": pc.get("harness"),
            "effort": effort,
            "runs": [run_view(pc, e, wait=wait if i == 0 or pc.get("run_class") == "B" else None)
                     for i, e in enumerate(es)],
            "summary": summary,
            "receipt": rec,
            "receipt_version": version,
            "receipt_checksum": digest,
            "run_class": pc.get("run_class", "A"),
            "n_runs": pc.get("n_runs") or 1,
            "_video": bool(pc.get("video")),
        }
        results.append(result)
        receipts.append((result, rec, digest))

    a = [r for r in results if r["run_class"] == "A"]
    b = [r for r in results if r["run_class"] == "B"]
    blocks: list[dict[str, Any]] = []
    for group in (a, b):
        for r in group:
            for other in group:
                if other is not r and r["run_class"] == "A" and w1_receipt.ranges_overlap(
                    r["summary"]["time"], other["summary"]["time"]
                ):
                    r["summary"].setdefault("tie_with", []).append(other["key"])
    if a:
        blocks.append({"run_class": "A", "contestants": a})
    if b:
        blocks.append({"run_class": "B", "contestants": b, "starts_after": "A" if a else None})

    context_limit = (challenge.plan or {}).get("context_limit")
    # A test whose runs ALL stopped at the limit ends "aborted" — exactly the
    # case the private warning + "larger budget" re-run exists for.
    truncation = (w1_receipt.truncation_info(pairs, context_limit=context_limit)
                  if finished or challenge.status == "failed" else None)
    prompt = {
        "template_id": challenge.prompt_template_id,
        "title": ((challenge.plan or {}).get("prompt") or {}).get("title") or challenge.title,
        "sha256": challenge.prompt_sha256 or c.sha256_text(challenge.prompt_text),
    }
    detail: dict[str, Any] = {
        "id": challenge.id,
        "title": challenge.title,
        "created_at": challenge.created_at,
        "trigger": challenge.trigger,
        "preset": challenge.preset,
        "protocol_version": "v1" if is_v1(challenge) else "v0",
        "prompt": prompt,
        "blocks": blocks,
        "rerun_of": challenge.rerun_of,
    }
    gate_pre = {"status": "pending", "reasons": [], "checksum": None}
    if finished:
        detail["gate"] = w1_receipt.evaluate_gate(
            protocol_version=detail["protocol_version"], prompt_sha=prompt["sha256"], blocks=blocks,
            receipts=receipts, public_texts=[], private_words=[], truncation=truncation, finished=True,
        )
        if private is None:
            private = await w1_receipt.private_words(session)
        texts = w1_kit.public_strings(detail)
        detail["gate"] = w1_receipt.evaluate_gate(
            protocol_version=detail["protocol_version"], prompt_sha=prompt["sha256"], blocks=blocks,
            receipts=receipts, public_texts=texts, private_words=private, truncation=truncation, finished=True,
        )
    else:
        detail["gate"] = gate_pre
    state, section, reason = human_state(challenge, detail["gate"], results, truncation)
    detail.update({"state": state, "section": section, "state_reason": reason, "truncation": truncation})
    lead = (a or b or [None])[0]
    if lead is not None and finished:
        detail["verdict"] = {
            "served_model_id": lead["served_model_id"] or lead["display_name"],
            "run_class": lead["run_class"],
            "n_runs": lead["n_runs"],
            "time": lead["summary"]["time"],
            "tokens_out": lead["summary"]["tokens_out"],
            "page_check": lead["summary"].get("page_check"),
        }
    if challenge.composed_video_path:
        plan = challenge.plan or {}
        detail["video"] = {
            "path": challenge.composed_video_path,
            "duration_s": plan.get("video_seconds"),
            "run_index": c.VIDEO_RUN_INDEX,
        }
    detail["allowed"] = allowed_actions(challenge, detail, results)
    if with_actions:
        detail["overlay_actions"] = await overlay_actions(session, challenge, entries)
    return detail


def allowed_actions(challenge: BenchChallenge, detail: dict, results: list[dict]) -> list[str]:
    if challenge.status in LIVE:
        return ["stop"]
    gate = detail["gate"]
    out: list[str] = []
    harvest = any(r["run_class"] == "B" for r in results) or any(
        x.get("fix") == "harvest" for x in gate.get("reasons", [])
    )
    if detail["state"] in ("done", "done_missing") and gate.get("status") in ("green", "amber"):
        out.append("kit")
    if harvest and detail["state"] != "aborted":
        out.append("harvest")
    out.append("rerun")
    if detail.get("truncation"):
        out.append("rerun_larger")
    if challenge.composed_video_path or any(r["_video"] for r in results):
        if detail["state"] in ("done", "done_missing"):
            out += ["rerender", "recompose"]
    if challenge.archived_at is None:
        out.append("archive")
    out.append("delete")
    return out


async def overlay_actions(session: AsyncSession, challenge: BenchChallenge, entries: list[BenchEntry]) -> list[dict]:
    from app.verticals import hooks

    from . import w1_schemas as s

    out = []
    for raw in await hooks.collect_challenge_actions(session, challenge, entries):
        try:
            out.append(s.OverlayAction.model_validate(raw).model_dump())
        except Exception:  # noqa: BLE001 — a provider's odd action is skipped, never a 500
            logger.warning("bench: overlay action skipped (%r)", raw)
    return out


async def list_tests(
    session: AsyncSession,
    *,
    section: str | None,
    model: str | None,
    device: str | None,
    harness: str | None,
    run_class: str | None,
    include_archived: bool,
) -> dict[str, Any]:
    stmt = select(BenchChallenge).order_by(BenchChallenge.created_at.desc())  # type: ignore[attr-defined]
    if not include_archived:
        stmt = stmt.where(BenchChallenge.archived_at.is_(None))  # type: ignore[union-attr]
    challenges = (await session.exec(stmt)).all()
    all_entries = (await session.exec(select(BenchEntry))).all()
    by_ch: dict[uuid.UUID, list[BenchEntry]] = {}
    for e in all_entries:
        by_ch.setdefault(e.challenge_id, []).append(e)
    private = await w1_receipt.private_words(session)
    items: list[dict] = []
    counts = {"running": 0, "needs_look": 0, "done": 0}
    last_at = None
    for ch in challenges:
        d = await test_detail(session, ch, entries=by_ch.get(ch.id, []), private=private, with_actions=False)
        results = [r for b in d["blocks"] for r in b["contestants"]]
        classes = {b["run_class"] for b in d["blocks"]}
        item = {
            "id": ch.id,
            "title": ch.title,
            "created_at": ch.created_at,
            "section": d["section"],
            "state": d["state"],
            "state_reason": d["state_reason"],
            "run_class": next(iter(classes)) if len(classes) == 1 else (None if not classes else "A"),
            "contestants": [
                {"display_name": r["display_name"], "harness": r["harness"], "locality": r["locality"]}
                for r in results
            ],
            "gate_status": d["gate"]["status"],
        }
        if d["state"] in ("running", "waiting"):
            runs = [run for r in results for run in r["runs"]]
            item["progress"] = {"done": sum(1 for run in runs if run["state"] == "done"), "total": len(runs)}
        # filters
        if model and not any(model.lower() in (r["display_name"] or "").lower() for r in results):
            continue
        if device and not any(device.lower() in (r.get("device_name") or "").lower() for r in results):
            continue
        if harness and not any(r["harness"] == harness for r in results):
            continue
        if run_class and run_class not in classes:
            continue
        counts[d["section"]] += 1
        if section and d["section"] != section:
            continue
        items.append(item)
        created = c.as_aware(ch.created_at)
        if last_at is None or (created and created > last_at):
            last_at = created
    return {"items": items, "counts": counts, "total": sum(counts.values()), "last_at": last_at}


# ── Create ───────────────────────────────────────────────────────────────


def _blocked(detail: str, blockers: list[dict]) -> HTTPException:
    return HTTPException(409, {"code": "bench_blocked", "message": detail, "blockers": [
        {"code": b["code"], "blocking": b["blocking"]} for b in blockers]})


def _field_422(loc: list, code: str, msg: str) -> HTTPException:
    return HTTPException(422, [{"loc": loc, "msg": msg, "type": code}])


async def _resolve_contestants(
    session: AsyncSession,
    picks: list[dict[str, Any]],
    *,
    preset: str,
    n_runs: int,
    prompt_text: str,
    max_tokens_override: int | None,
) -> tuple[list[dict[str, Any]], dict[str, Any], list[dict[str, Any]]]:
    """Re-resolve the sheet's picks against the fleet as it runs NOW (a
    stale sheet never freezes a wrong model). Returns (contestant plans,
    total money, blockers)."""
    from app.services import reasoning_params

    from .orchestrator import AGENT_BRIEF_TEMPLATE

    views = await w1_plan.load_views(session)
    prices = await w1_plan.load_prices(session)
    history = await w1_plan.load_history(session, views)
    pcs_a: list[dict] = []
    pcs_b: list[dict] = []
    moneys: list[dict] = []
    blockers: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for i, pick in enumerate(picks):
        dup = (str(pick["runtime_id"]), pick.get("harness") or "api")
        if dup in seen:
            raise _field_422(["body", "picks", i], "bench_pick_duplicate", "the same contestant was picked twice")
        seen.add(dup)
        view = w1_plan.find_view(views, uuid.UUID(str(pick["runtime_id"])))
        if view is None or not view.served_model:
            raise _field_422(["body", "picks", i, "runtime_id"], "bench_runtime_not_testable",
                             "runtime not found or serves no model right now")
        d = await w1_plan.contestant(session, view, prices=prices, hist=history[view.key], n_runs=n_runs)
        hard = [b for b in d["blockers"] if b["blocking"]]
        if hard:
            raise _blocked("contestant cannot start", hard)
        options = {h["harness"]: h for h in d["harnesses"]}
        harness = pick.get("harness") or "api"
        opt = options.get(harness)
        if opt is None or not opt["available"]:
            raise _field_422(["body", "picks", i, "harness"], "bench_harness_not_available",
                             f"harness {harness!r} is not available for this runtime")
        efforts = list(pick.get("efforts") or ["default"])
        if len(set(efforts)) != len(efforts):
            raise _field_422(["body", "picks", i, "efforts"], "bench_effort_duplicate", "effort chosen twice")
        if len(efforts) > 1 and preset != "effort_ladder" and preset != "rerun":
            raise _field_422(["body", "picks", i, "efforts"], "bench_effort_ladder_only",
                             "several efforts only with the effort ladder")
        for b in d["blockers"]:
            if b not in blockers:
                blockers.append(b)
        base = {
            "runtime_id": str(view.runtime.id),
            "row_ids": [str(x) for x in view.row_ids],
            "served_model_id": view.served_model,
            "display_name": view.served_model,
            "locality": view.locality,
            "device_name": view.device_name if view.locality == "local" else None,
            "nodes": view.nodes,
            "gpu_mode": view.gpu_mode,
            "provider": view.provider,
            "engine": view.engine,
            "quant": view.quant,
            "recipe_slug": view.recipe.slug if view.recipe is not None else None,
            "spec_decoding": view.spec_decoding,
            "serving_since": view.serving_since.isoformat() if view.serving_since else None,
            "context_limit": view.runtime.max_context_len,
            "endpoint_row": str(view.runtime.id),
        }
        price = w1_plan.price_for(view.served_model, prices)
        price_d = ({"input_per_mtok": price.input_per_mtok, "output_per_mtok": price.output_per_mtok}
                   if price is not None else None)
        if opt["run_class"] == "A":
            offered = w1_plan.offered_levels(d["effort"])
            for level in efforts:
                if level not in offered:
                    raise _field_422(["body", "picks", i, "efforts"], "bench_effort_not_offered",
                                     f"effort {level!r} was not offered by the probe")
                max_tokens = max_tokens_override or reasoning_params.max_tokens_for(
                    level, c.BASE_MAX_TOKENS, view.runtime.max_context_len)
                money = w1_plan.money_for(view, prices, history[view.key], n_runs=n_runs, run_class="A",
                                          max_tokens=max_tokens)
                moneys.append(money)
                key = str(view.runtime.id) if len(efforts) == 1 else f"{view.runtime.id}:{level}"
                merged = next((o.get("merged_level") for o in d["effort"]["options"] if o.get("is_default")), None)
                pcs_a.append({**base, "key": key, "harness": "api", "run_class": "A", "effort": level,
                              "max_tokens": max_tokens, "n_runs": n_runs, "money": money, "price": price_d,
                              "effort_status": d["effort"]["status"], "effort_default_level": merged})
        else:
            if efforts != ["default"]:
                raise _field_422(["body", "picks", i, "efforts"], "bench_effort_agent_path",
                                 "effort cannot be set on the agent path yet (D5) — it is recorded")
            agent = w1_plan.agent_for(view, harness)
            money = w1_plan.money_for(view, prices, history[view.key], n_runs=1, run_class="B")
            moneys.append(money)
            brief = AGENT_BRIEF_TEMPLATE.format(prompt=prompt_text)
            pcs_b.append({**base, "key": f"{view.runtime.id}:{harness}", "harness": harness, "run_class": "B",
                          "effort": "default", "n_runs": 1, "money": money, "price": price_d,
                          "agent_id": str(agent.id) if agent else None, "brief_sha": c.sha256_text(brief),
                          "time_limit_min": c.AGENT_TIME_LIMIT_MIN})
    pcs = pcs_a + pcs_b
    video_class = "A" if pcs_a else "B"
    n_video = 0
    for pos, pc in enumerate(pcs):
        pc["position"] = pos
        pc["video"] = pc["run_class"] == video_class and n_video < c.MAX_VIDEO_CONTESTANTS
        if pc["video"]:
            n_video += 1
    return pcs, w1_plan.total_money(moneys), blockers


def _cost_gate(money: dict, confirmed: float | None, over_cap: bool, unknown_ok: bool = False) -> None:
    """D4: a paid test starts only with the operator's OK for the quoted
    amount; above $1 a second, explicit OK. Fail-closed: a cloud API whose
    price MC does not know (not a subscription) asks "Kosten unbekannt —
    OK?" before the first request."""
    if money.get("cost_kind") == "unknown":
        if not unknown_ok:
            raise HTTPException(409, {"code": "bench_cost_confirmation", "step": 1, "usd": None,
                                      "cap_usd": c.COST_CAP_USD, "price_unknown": True})
        return
    if money.get("cost_kind") != "api_price":
        return
    usd = float(money.get("usd") or 0)
    if confirmed is None or confirmed + 1e-9 < usd:
        raise HTTPException(409, {"code": "bench_cost_confirmation", "step": 1, "usd": usd,
                                  "cap_usd": c.COST_CAP_USD})
    if usd > c.COST_CAP_USD and not over_cap:
        raise HTTPException(409, {"code": "bench_cost_confirmation", "step": 2, "usd": usd,
                                  "cap_usd": c.COST_CAP_USD})


async def create_test(
    session: AsyncSession,
    *,
    picks: list[dict[str, Any]],
    preset: str,
    trigger: str,
    user_id: str | None,
    template_id: uuid.UUID | None = None,
    prompt: dict[str, Any] | None = None,
    n_runs: int | None = None,
    rerun_of: uuid.UUID | None = None,
    budget: str = "same",
    max_tokens_override: int | None = None,
    cost_confirmed_usd: float | None = None,
    confirm_over_cap: bool = False,
    confirm_unknown_cost: bool = False,
) -> BenchChallenge:
    if prompt is None:
        prompt = await w1_plan.default_prompt(session, template_id)
    n_runs = n_runs or c.PRESET_RUNS.get(preset, 3)
    pcs, money, blockers = await _resolve_contestants(
        session, picks, preset=preset, n_runs=n_runs, prompt_text=prompt["text"],
        max_tokens_override=max_tokens_override,
    )
    _cost_gate(money, cost_confirmed_usd, confirm_over_cap, confirm_unknown_cost)
    video = [pc for pc in pcs if pc["video"]]
    plan = {
        "preset": preset,
        "n_runs": n_runs,
        "picks": [{"runtime_id": str(p["runtime_id"]), "harness": p.get("harness") or "api",
                   "efforts": list(p.get("efforts") or ["default"])} for p in picks],
        "prompt": {"template_id": str(prompt["template_id"]) if prompt.get("template_id") else None,
                   "title": prompt["title"], "sha256": prompt["sha256"]},
        "contestants": pcs,
        "money": money,
        "cost_confirmed_usd": cost_confirmed_usd,
        "budget": budget,
        "max_tokens_override": max_tokens_override,
        "blockers_at_start": [b["code"] for b in blockers],
        "context_limit": min((pc["context_limit"] for pc in pcs if pc.get("context_limit")), default=None),
        "video_seconds": c.VIDEO_DURATION_S + 4.5 if video else None,
        "created_by": user_id,
        "temperature": c.TEMPERATURE,
        "system_prompt_sha": c.SYSTEM_PROMPT_SHA,
    }
    challenge = BenchChallenge(
        title=prompt["title"],
        prompt_template_id=prompt.get("template_id"),
        prompt_text=prompt["text"],
        mode="side_by_side" if len(video) == 2 else "single",
        record_duration_s=c.VIDEO_DURATION_S,
        status="waiting",
        preset=preset,
        prompt_sha256=prompt["sha256"],
        protocol_version=c.PROTOCOL_V1,
        trigger=trigger,
        rerun_of=rerun_of,
        plan=plan,
    )
    session.add(challenge)
    await session.flush()
    for pc in pcs:
        runs = pc["n_runs"] if pc["run_class"] == "A" else 1
        for r in range(1, runs + 1):
            session.add(BenchEntry(
                challenge_id=challenge.id,
                model_label=pc["served_model_id"],
                position=pc["position"],
                source_kind="api" if pc["run_class"] == "A" else "agent",
                spark_model=pc["served_model_id"] if pc["run_class"] == "A" else None,
                agent_id=uuid.UUID(pc["agent_id"]) if pc.get("agent_id") else None,
                runtime_id=uuid.UUID(pc["runtime_id"]),
                harness=pc["harness"],
                run_class=pc["run_class"],
                effort_requested=pc["effort"],
                repeat_idx=r,
                attempt_no=1,
                metrics={"effort_requested": pc["effort"], "phase": "queued"},
            ))
    await session.commit()
    await session.refresh(challenge)
    _wake_runner()
    logger.info("bench W1 test %s created (%d contestants, trigger %s)", challenge.id, len(pcs), trigger)
    return challenge


def _wake_runner() -> None:
    try:
        from . import w1_runner

        w1_runner.wake()
    except Exception:  # noqa: BLE001 — the loop also polls; a missed wake costs seconds
        logger.debug("bench W1 runner wake skipped", exc_info=True)


async def get_or_404(session: AsyncSession, test_id: uuid.UUID) -> BenchChallenge:
    ch = await session.get(BenchChallenge, test_id)
    if ch is None:
        raise HTTPException(404, "Test not found")
    return ch


def _old_picks(challenge: BenchChallenge, entries: list[BenchEntry]) -> list[dict[str, Any]]:
    plan = challenge.plan or {}
    if is_v1(challenge) and plan.get("picks"):
        return [{"runtime_id": uuid.UUID(str(p["runtime_id"])), "harness": p.get("harness") or "api",
                 "efforts": list(p.get("efforts") or ["default"])} for p in plan["picks"]]
    picks: list[dict[str, Any]] = []
    for e in sorted(entries, key=lambda e: e.position or 0):
        if e.runtime_id is None:
            continue
        harness = e.harness or "api"
        if not any(p["runtime_id"] == e.runtime_id and p["harness"] == harness for p in picks):
            picks.append({"runtime_id": e.runtime_id, "harness": harness, "efforts": ["default"]})
    return picks[:2]


async def rerun_test(
    session: AsyncSession,
    old: BenchChallenge,
    *,
    budget: str,
    user_id: str | None,
    cost_confirmed_usd: float | None = None,
    confirm_over_cap: bool = False,
    confirm_unknown_cost: bool = False,
    picks: list[dict[str, Any]] | None = None,
) -> BenchChallenge:
    """"Nochmal" = the same plan against the model live NOW. ``budget``
    "larger" = the truncation re-run: the token budget the warning
    suggested (or twice the old one)."""
    entries = await entries_of(session, old.id)
    if picks is None:
        picks = _old_picks(old, entries)
    if not picks:
        views = await w1_plan.load_views(session)
        subject = w1_plan.subject_view(views)
        if subject is None:
            raise HTTPException(409, {"code": "bench_blocked", "blockers": [{"code": "no_model", "blocking": True}]})
        picks = [{"runtime_id": subject.runtime.id, "harness": "api", "efforts": ["default"]}]
    plan = old.plan or {}
    override = plan.get("max_tokens_override")
    if budget == "larger":
        pairs = contestant_plans(old, entries)
        trunc = w1_receipt.truncation_info(pairs, context_limit=plan.get("context_limit"))
        if trunc is not None:
            override = trunc["suggested_max_tokens"]
        else:
            current = max((int((pc.get("max_tokens") or c.BASE_MAX_TOKENS)) for pc, _ in pairs), default=c.BASE_MAX_TOKENS)
            override = current * c.LARGER_BUDGET_FACTOR
    prompt = {"template_id": old.prompt_template_id, "title": old.title, "text": old.prompt_text,
              "sha256": old.prompt_sha256 or c.sha256_text(old.prompt_text)}
    n_runs = int(plan.get("n_runs") or (c.PRESET_RUNS["standard"] if is_v1(old) else 3))
    return await create_test(
        session, picks=picks, preset="rerun", trigger="rerun", user_id=user_id, prompt=prompt,
        n_runs=n_runs, rerun_of=old.id, budget=budget, max_tokens_override=override,
        cost_confirmed_usd=cost_confirmed_usd, confirm_over_cap=confirm_over_cap,
        confirm_unknown_cost=confirm_unknown_cost,
    )


async def stop_test(session: AsyncSession, challenge: BenchChallenge, user_id: str) -> None:
    from . import orchestrator

    if challenge.status not in LIVE:
        raise HTTPException(409, f"Test is {challenge.status!r} — stop only while waiting or running.")
    try:
        from . import w1_runner

        w1_runner.cancel(challenge.id)
    except Exception:  # noqa: BLE001
        logger.debug("bench W1 cancel skipped", exc_info=True)
    await orchestrator.stop_challenge(session, challenge, user_id)
    challenge.error = "stopped_by_operator"
    challenge.wait_reason = None
    session.add(challenge)
    entries = await entries_of(session, challenge.id)
    for e in entries:
        if e.error == orchestrator.STOPPED_BY_OPERATOR:
            e.metrics = {**(e.metrics or {}), "error_code": "stopped_by_operator", "phase": "done"}
            session.add(e)
    await session.commit()


# ── Auto test after a recipe switch (W1.11) ──────────────────────────────


async def create_auto_test(
    session: AsyncSession, *, host_id: uuid.UUID, recipe_slug: str, user_id: str | None
) -> dict[str, Any]:
    """A Standard test in state waiting (model_loading). The vertical's loop
    resolves the runtime once the switched model answers and starts it ≥ 15
    min after serving_since on a quiet box. A pending auto test of the same
    host is superseded (aborted: superseded_by_switch)."""
    from app.models.host import Host

    if await session.get(Host, host_id) is None:
        raise HTTPException(404, "Host not found")
    superseded: uuid.UUID | None = None
    open_tests = (
        await session.exec(
            select(BenchChallenge).where(
                BenchChallenge.trigger == "auto_switch",
                BenchChallenge.status.in_(LIVE),  # type: ignore[union-attr]
            )
        )
    ).all()
    for old in open_tests:
        auto = (old.plan or {}).get("auto") or {}
        if auto.get("host_id") == str(host_id):
            try:
                from . import w1_runner

                w1_runner.cancel(old.id)
            except Exception:  # noqa: BLE001
                pass
            old.status = "failed"
            old.error = "superseded_by_switch"
            old.wait_reason = None
            session.add(old)
            for e in await entries_of(session, old.id):
                if e.status in ("pending", "generating"):
                    e.status = "failed"
                    e.error = "superseded_by_switch"
                    e.metrics = {**(e.metrics or {}), "error_code": "superseded_by_switch"}
                    session.add(e)
            superseded = old.id
    prompt = await w1_plan.default_prompt(session, None)
    now = c.now_utc()
    not_before = now + timedelta(minutes=c.FRESH_MIN)
    challenge = BenchChallenge(
        title=prompt["title"],
        prompt_template_id=prompt.get("template_id"),
        prompt_text=prompt["text"],
        mode="single",
        record_duration_s=c.VIDEO_DURATION_S,
        status="waiting",
        wait_reason="model_loading",
        preset="standard",
        prompt_sha256=prompt["sha256"],
        protocol_version=c.PROTOCOL_V1,
        trigger="auto_switch",
        plan={
            "preset": "standard",
            "n_runs": c.PRESET_RUNS["standard"],
            "picks": [],
            "contestants": [],
            "prompt": {"template_id": str(prompt["template_id"]) if prompt.get("template_id") else None,
                       "title": prompt["title"], "sha256": prompt["sha256"]},
            "auto": {"host_id": str(host_id), "recipe_slug": recipe_slug, "requested_at": now.isoformat()},
            "not_before": not_before.isoformat(),
            "created_by": user_id,
        },
    )
    session.add(challenge)
    await session.commit()
    await session.refresh(challenge)
    _wake_runner()
    return {"test_id": challenge.id, "state": "waiting", "not_before": not_before,
            "superseded_test_id": superseded}


# ── Eingang: the result page clears its own "Test fertig" item ───────────


async def mark_seen(session: AsyncSession, challenge: BenchChallenge) -> int:
    """Opening a FINISHED test's result page is what the Eingang item asks
    for — it resolves the item (no side effect for ``bench_result``), so a
    test Mark watched live does not cost him an extra "Erledigt" tap.
    Returns how many items were resolved."""
    from app.models.approval import Approval

    if challenge.status in LIVE:
        return 0
    rows = (
        await session.exec(select(Approval).where(Approval.action_type == "bench_result",
                                                  Approval.status == "pending"))
    ).all()
    n = 0
    for a in rows:
        if (a.payload or {}).get("test_id") == str(challenge.id):
            a.status = "approved"
            a.resolved_at = c.now_utc()
            a.resolver_note = "seen on the result page"
            session.add(a)
            n += 1
    if n:
        await session.commit()
    return n
