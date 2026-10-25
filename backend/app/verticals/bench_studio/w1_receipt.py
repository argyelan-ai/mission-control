"""Bench W1 — summaries, the write-once receipt, the truth gate, truncation.

One frozen record per contestant (concept §7.2) feeds every artefact: the
result page, the video's receipt card, the card PNG, the settings text and
the alt text. The receipt lives on the contestant's head entry (repeat 1);
every change is a NEW ``bench_receipts`` row (never an update) and the
entry mirrors the newest. The gate (§6.4) is computed from those receipts —
every artefact carries the checksum of the receipts it was built from.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.bench import BenchChallenge, BenchEntry, BenchReceipt

from . import w1_common as c
from .orchestrator import LIB_LANE_KEYS

DONE_ENTRY = ("generated", "rendered")


# ── Stats ────────────────────────────────────────────────────────────────


def stat(values: list[float | None], *, reason: str | None = None) -> dict[str, Any]:
    vals = [float(v) for v in values if v is not None]
    if not vals:
        return {"measured": False, "n": 0, "reason": {"code": reason or "not_recorded", "params": {}}}
    med = c.median(vals)
    return {"measured": True, "median": med, "min": min(vals), "max": max(vals), "n": len(vals)}


def done_runs(entries: list[BenchEntry]) -> list[BenchEntry]:
    return [e for e in entries if e.status in DONE_ENTRY]


def _m(e: BenchEntry) -> dict[str, Any]:
    return e.metrics or {}


def contestant_summary(
    pc: dict[str, Any], entries: list[BenchEntry], *, finished: bool
) -> dict[str, Any]:
    """Median + range over the finished runs of one contestant. A missing
    headline number says why (K15); class B has no tempo row at all."""
    runs = done_runs(entries)
    run_class = pc.get("run_class", "A")
    pending = None if finished else "runs_pending"
    if run_class == "B":
        e = runs[0] if runs else None
        m = _m(e) if e else {}
        active = m.get("active_ms") or m.get("duration_ms")
        out = m.get("tokens_out")
        summary: dict[str, Any] = {
            "time": stat([active], reason=pending or ("harvest_pending" if e else "all_runs_failed")),
            "time_kind": "active_ms",
            "wait_ms": m.get("wait_ms"),
            "tokens_out": stat([out], reason=pending or ("harvest_pending" if e else "all_runs_failed")),
            "tokens_cache_read": m.get("tokens_cache_read"),
            "cost": cost_of(pc, entries),
            "page_check": m.get("page_check"),
        }
        return summary
    reason_missing = pending or ("all_runs_failed" if not runs else "no_usage")
    decode = [_m(e).get("decode_tok_s") for e in runs if _m(e).get("decode_sane")]
    summary = {
        "time": stat([_m(e).get("gen_ms") for e in runs], reason=pending or ("all_runs_failed" if not runs else "not_recorded")),
        "time_kind": "gen_ms",
        "tokens_out": stat([_m(e).get("tokens_out") for e in runs], reason=reason_missing),
        "cost": cost_of(pc, entries),
        "page_check": page_check_of(entries),
    }
    reasoning = [_m(e).get("tokens_reasoning") for e in runs if _m(e).get("tokens_reasoning") is not None]
    if reasoning:
        summary["tokens_reasoning"] = stat(reasoning)
    cache = [_m(e).get("tokens_cache_read") for e in runs if _m(e).get("tokens_cache_read")]
    if cache:
        summary["tokens_cache_read"] = int(c.median(cache))
    if any(v is not None for v in decode):
        summary["decode_tok_s"] = stat(decode)
    ttft = [_m(e).get("ttft_ms") for e in runs if _m(e).get("ttft_ms") is not None]
    if ttft:
        summary["ttft_ms"] = stat(ttft)
    return summary


def page_check_of(entries: list[BenchEntry]) -> str | None:
    video = next((e for e in entries if e.repeat_idx == c.VIDEO_RUN_INDEX and _m(e).get("page_check")), None)
    if video is not None:
        return _m(video)["page_check"]
    return None


def cost_of(pc: dict[str, Any], entries: list[BenchEntry]) -> dict[str, Any]:
    """Money as spent: local/subscription never carry ``usd`` (R3); a paid
    run is the API list price of the tokens really used — by EVERY attempt
    that reported usage, a failed one too (it was paid all the same)."""
    runs = [e for e in entries if e.status in DONE_ENTRY
            or _m(e).get("tokens_out") is not None or _m(e).get("tokens_in") is not None]
    money = dict(pc.get("money") or {"cost_kind": "unknown"})
    kind = money.get("cost_kind", "unknown")
    price = pc.get("price") or {}
    out = {"cost_kind": kind}
    for k in ("price_source", "price_date"):
        if money.get(k):
            out[k] = money[k]
    if price and runs:
        spent = sum(
            ((_m(e).get("tokens_in") or 0) * price.get("input_per_mtok", 0)
             + (_m(e).get("tokens_out") or 0) * price.get("output_per_mtok", 0)) / 1e6
            for e in runs
        )
        if kind == "api_price":
            out["usd"] = round(spent, 4)
        elif kind in ("local", "subscription"):
            out["hypothetical_usd"] = round(spent, 4)
    elif kind in ("local", "subscription") and money.get("hypothetical_usd") is not None:
        out["hypothetical_usd"] = money["hypothetical_usd"]
    if kind == "unknown" and money.get("reason"):
        out["reason"] = money["reason"]
    return out


def ranges_overlap(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if not (a.get("measured") and b.get("measured")):
        return False
    return a["min"] <= b["max"] and b["min"] <= a["max"]


# ── Receipt ──────────────────────────────────────────────────────────────


def build_receipt(
    challenge: BenchChallenge, pc: dict[str, Any], entries: list[BenchEntry], *, harvested_at: str | None = None
) -> dict[str, Any]:
    """The frozen record of one contestant (§7.2). Unknown fields are named
    with a reason, never guessed."""
    runs = done_runs(entries)
    head = next((e for e in entries if e.repeat_idx == c.VIDEO_RUN_INDEX), entries[0] if entries else None)
    hm = _m(head) if head else {}
    run_class = pc.get("run_class", "A")
    summary = contestant_summary(pc, entries, finished=True)
    unknown: dict[str, str] = {}
    if not pc.get("engine"):
        unknown["engine"] = "hand_made_runtime" if pc.get("locality") == "local" else "cloud_provider"
        unknown["engine_version"] = unknown["engine"]
    else:
        unknown["engine_version"] = "not_recorded"
    if pc.get("locality") == "local" and pc.get("spec_decoding") is None:
        unknown["spec_decoding"] = "no_recipe"
    if pc.get("locality") == "local" and not pc.get("gpu_mode"):
        unknown["gpu_mode"] = "not_reported"
    attempts = sum(max(1, e.attempt_no or 1) for e in entries)
    failures = attempts - len(runs)
    receipt: dict[str, Any] = {
        "protocol_version": challenge.protocol_version or c.PROTOCOL_V1,
        "run_class": run_class,
        "served_model_id": pc.get("served_model_id"),
        "display_name": pc.get("display_name"),
        "locality": pc.get("locality"),
        "device": pc.get("device_name"),
        "nodes": pc.get("nodes"),
        "provider": pc.get("provider"),
        "engine": pc.get("engine"),
        "recipe_slug": pc.get("recipe_slug"),
        "quant": pc.get("quant"),
        "gpu_mode": pc.get("gpu_mode"),
        "spec_decoding": pc.get("spec_decoding"),
        "harness": pc.get("harness"),
        "effort_requested": pc.get("effort", "default"),
        "prompt_sha256": challenge.prompt_sha256,
        "n_runs": pc.get("n_runs"),
        "video_run_index": c.VIDEO_RUN_INDEX if pc.get("video") else None,
        "attempts": attempts,
        "failures": failures,
        "network_blocked": hm.get("network_blocked"),
        "network_attempts": hm.get("network_attempts"),
        **({"network_blocked_scripts": hm["network_blocked_scripts"]}
           if hm.get("network_blocked_scripts") is not None else {}),
        "libraries_inlined": list(hm.get("libraries_inlined") or []),
        # Lane lib:three-local — only when the recorder reported it, so the
        # receipts of older recordings keep their checksum.
        **{k: hm[k] for k in LIB_LANE_KEYS if hm.get(k) is not None},
        "html_bytes": hm.get("html_bytes"),
        "recorded_at": hm.get("recorded_at"),
        "harvested_at": harvested_at,
        "time_kind": summary["time_kind"],
        "cost_kind": summary["cost"]["cost_kind"],
        "cost_usd": summary["cost"].get("usd"),
        "cost_hypothetical_usd": summary["cost"].get("hypothetical_usd"),
        "price_source": summary["cost"].get("price_source"),
        "price_date": summary["cost"].get("price_date"),
        "page_check": summary.get("page_check"),
    }
    for name in ("time", "tokens_out"):
        st = summary[name]
        if st.get("measured"):
            receipt[f"{name}_median"] = st["median"]
            receipt[f"{name}_min"] = st["min"]
            receipt[f"{name}_max"] = st["max"]
        else:
            receipt[f"{name}_missing"] = st["reason"]["code"]
    if run_class == "A":
        receipt.update({
            "effort_sent": hm.get("effort_sent"),
            "effort_observed": hm.get("effort_observed"),
            "thinking_key": hm.get("thinking_key"),
            "temperature": hm.get("temperature", c.TEMPERATURE),
            "max_tokens": hm.get("max_tokens") or pc.get("max_tokens"),
            # context = the whole context window (local, no practical cap) ·
            # engine = none sent, the engine's maximum · cap = a token cap.
            "max_tokens_rule": pc.get("max_tokens_rule"),
            "time_limit_min": pc.get("time_limit_min"),
            "system_prompt_sha": c.SYSTEM_PROMPT_SHA,
            "quiet_box": hm.get("quiet_box"),
            "serving_age_min": hm.get("serving_age_min"),
            "finish_reasons": [_m(e).get("finish_reason") for e in sorted(entries, key=lambda e: e.repeat_idx)],
            "truncated_runs": sum(1 for e in entries if _m(e).get("truncated")),
            "tokens_in": c.median([_m(e).get("tokens_in") for e in runs]),
        })
        for name in ("decode_tok_s", "ttft_ms", "tokens_reasoning"):
            st = summary.get(name)
            if st and st.get("measured"):
                receipt[f"{name}_median"] = st["median"]
                receipt[f"{name}_min"] = st["min"]
                receipt[f"{name}_max"] = st["max"]
        if summary.get("tokens_cache_read"):
            receipt["tokens_cache_read"] = summary["tokens_cache_read"]
        receipt["runs"] = [
            {
                "repeat_idx": e.repeat_idx,
                "gen_ms": _m(e).get("gen_ms"),
                "tokens_out": _m(e).get("tokens_out"),
                "decode_tok_s": _m(e).get("decode_tok_s") if _m(e).get("decode_sane") else None,
                "ttft_ms": _m(e).get("ttft_ms"),
                "finish_reason": _m(e).get("finish_reason"),
            }
            for e in sorted(entries, key=lambda e: e.repeat_idx)
        ]
    else:
        receipt.update({
            "brief_sha": pc.get("brief_sha"),
            "tools_allowed": True,
            "time_limit_min": pc.get("time_limit_min"),
            "effort_observed": hm.get("effort_observed"),
            "wait_ms": hm.get("wait_ms"),
            "tokens_in": hm.get("tokens_in"),
            "tokens_cache_read": hm.get("tokens_cache_read"),
        })
        unknown["harness_version"] = "not_recorded"
        if not pc.get("time_limit_min"):
            unknown["time_limit_min"] = "not_recorded"
    if receipt.get("network_blocked") is None:
        unknown["network"] = "not_recorded" if run_class == "A" else "recorded_before_block"
    receipt["unknown"] = unknown
    return receipt


async def latest_receipt(session: AsyncSession, entry_id: uuid.UUID) -> BenchReceipt | None:
    return (
        await session.exec(
            select(BenchReceipt).where(BenchReceipt.entry_id == entry_id).order_by(BenchReceipt.version.desc())  # type: ignore[union-attr]
        )
    ).first()


async def write_receipt(
    session: AsyncSession, head: BenchEntry, receipt: dict[str, Any], *, reason: str
) -> tuple[int, str, bool]:
    """Write-once: a receipt identical to the newest version is not written
    again; a changed one becomes version n+1 (``bench_receipts`` rows are
    never updated). The entry mirrors the newest. Returns (version,
    checksum, written)."""
    digest = c.checksum(receipt)
    last = await latest_receipt(session, head.id)
    if last is not None and last.checksum == digest:
        return last.version, last.checksum, False
    version = (last.version + 1) if last is not None else 1
    session.add(BenchReceipt(entry_id=head.id, version=version, receipt=receipt, checksum=digest, reason=reason))
    head.receipt = {**receipt, "_version": version, "_checksum": digest}
    session.add(head)
    return version, digest, True


def receipt_parts(entry: BenchEntry) -> tuple[dict[str, Any] | None, int | None, str | None]:
    """(receipt, version, checksum) mirrored on the head entry."""
    if not entry.receipt:
        return None, None, None
    r = dict(entry.receipt)
    version = r.pop("_version", None)
    digest = r.pop("_checksum", None)
    return r, version, digest


# ── Truncation (private warning, decision 24.10.) ─────────────────────────


def truncation_info(
    contestants: list[tuple[dict[str, Any], list[BenchEntry]]], *, context_limit: int | None = None
) -> dict[str, Any] | None:
    """Runs that stopped at max_tokens — for the OPERATOR only. Never on a
    public artefact; the result page offers "re-run with a larger budget"
    when a capped run was cut (``larger_budget``). A local run already had
    the whole context window: a larger budget does not exist there."""
    from app.services.reasoning_params import _PROMPT_HEADROOM

    cut: list[BenchEntry] = []
    total = 0
    max_tokens = 0
    capped_cut = False
    no_page = 0
    video_cut = False
    for pc, entries in contestants:
        if pc.get("run_class") != "A":
            continue
        capped = pc.get("max_tokens_rule", "cap") == "cap"
        for e in entries:
            total += 1
            m = _m(e)
            max_tokens = max(max_tokens, int(m.get("max_tokens") or pc.get("max_tokens") or m.get("tokens_out") or 0))
            if m.get("truncated") or m.get("finish_reason") == "length":
                cut.append(e)
                capped_cut = capped_cut or capped
                if e.status not in DONE_ENTRY:
                    no_page += 1
                elif pc.get("video") and (e.repeat_idx or 1) == c.VIDEO_RUN_INDEX:
                    video_cut = True
    if not cut:
        return None
    suggested = max_tokens
    if capped_cut:
        suggested = max_tokens * c.LARGER_BUDGET_FACTOR
        if context_limit and context_limit > _PROMPT_HEADROOM:
            suggested = max(max_tokens, min(suggested, context_limit - _PROMPT_HEADROOM))
    return {
        "runs_cut": len(cut),
        "n_runs": total,
        "max_tokens": max_tokens,
        "suggested_max_tokens": suggested,
        "larger_budget": suggested > max_tokens,
        "entry_ids": [e.id for e in cut],
        "runs_without_page": no_page,
        "video_run_cut": video_cut,
    }


# ── Gate (§6.4) ──────────────────────────────────────────────────────────


def _reason(rule: str, severity: str, code: str, params: dict | None = None, fix: str | None = None) -> dict:
    d: dict[str, Any] = {"rule": rule, "severity": severity, "code": code, "params": params or {}}
    if fix:
        d["fix"] = fix
    return d


def evaluate_gate(
    *,
    protocol_version: str,
    prompt_sha: str | None,
    blocks: list[dict[str, Any]],
    receipts: list[tuple[dict[str, Any], dict[str, Any] | None, str | None]],
    public_texts: list[str],
    private_words: list[str],
    truncation: dict[str, Any] | None,
    finished: bool,
) -> dict[str, Any]:
    """Six red rules (R6 = the page needed the internet), the rest amber (printed, not blocking). ``receipts``
    = (contestant result, receipt, checksum) per contestant."""
    if not finished:
        return {"status": "pending", "reasons": []}
    reasons: list[dict] = []
    for block in blocks:
        if len({r.get("run_class") for r in block["contestants"]} | {block["run_class"]}) > 1:
            reasons.append(_reason("R5", "red", "unfair_comparison", {"why": "classes_mixed"}))
        # Lanes (lib:three-local vs none, 25.10.2026): MC served three.js to
        # one contestant, the other wrote everything itself. Same prompt, so
        # not unfair — but never compared silently: printed, and each pane
        # chip names its lane.
        lanes = sorted({c.lib_lane(r.get("receipt")) for r in block["contestants"]})
        if len(lanes) > 1:
            reasons.append(_reason("amber", "amber", "lib_tracks_mixed", {"tracks": lanes}))
    for result, receipt, _sum in receipts:
        summary = result["summary"]
        run_class = (receipt or {}).get("run_class") or result.get("run_class", "A")
        # R1 — a headline number missing (or the agent harvest still pending)
        for field in ("time", "tokens_out"):
            if not summary[field].get("measured"):
                fix = "harvest" if run_class == "B" else "rerun"
                reasons.append(_reason("R1", "red", "headline_missing", {"field": field}, fix))
        # R2 — name on the artefact = served model id, verbatim
        served = result.get("served_model_id")
        if not served or result.get("display_name") != served or (
            receipt and receipt.get("display_name") not in (None, served)
        ):
            reasons.append(_reason("R2", "red", "name_mismatch", {}))
        for run in result.get("runs", []):
            echoed = run.get("_echoed_model")
            if echoed and served and echoed != served:
                reasons.append(_reason("R2", "red", "name_mismatch", {}))
                break
        # R3 — a bare $ on a local or subscription run
        cost = summary["cost"]
        if cost.get("cost_kind") in ("local", "subscription") and cost.get("usd") is not None:
            reasons.append(_reason("R3", "red", "dollar_on_local", {}))
        if receipt and receipt.get("cost_kind") in ("local", "subscription") and receipt.get("cost_usd") is not None:
            reasons.append(_reason("R3", "red", "dollar_on_local", {}))
        # R5 — same prompt, decode only, labelled time
        if receipt and prompt_sha and receipt.get("prompt_sha256") not in (None, prompt_sha):
            reasons.append(_reason("R5", "red", "unfair_comparison", {"why": "prompt_differs"}))
        if run_class == "B" and summary.get("decode_tok_s") is not None:
            reasons.append(_reason("R5", "red", "unfair_comparison", {"why": "tempo_on_agent"}))
        if run_class == "A" and summary.get("time_kind") != "gen_ms":
            reasons.append(_reason("R5", "red", "unfair_comparison", {"why": "time_kind"}))
        if run_class == "B" and summary.get("time_kind") != "active_ms":
            reasons.append(_reason("R5", "red", "unfair_comparison", {"why": "time_kind"}))
        r = receipt or {}
        # R6 — the page needed the internet (live finding 25.10.2026): the
        # recorder blocks every request, so a blocked script/module (three.js
        # from a CDN) leaves an empty scene — red. Only fonts/images/styles
        # blocked: the scene still draws — amber, printed.
        net = c.blocked_network(r)
        if net == "script":
            reasons.append(_reason("R6", "red", "needs_network"))
        elif net == "assets":
            reasons.append(_reason("amber", "amber", "network_assets_blocked"))
        # The page asked for another three.js version than the one MC serves
        # (e.g. r128 code on r170): printed, the scene may differ in detail.
        if r.get("three_version_mismatch"):
            reasons.append(_reason("amber", "amber", "three_version_differs"))
        # amber
        obs = r.get("effort_observed")
        if not obs or (isinstance(obs, dict) and obs.get("source") == "unknown"):
            reasons.append(_reason("amber", "amber", "effort_unknown"))
        if r.get("quiet_box") in ("busy", "timeout"):
            reasons.append(_reason("amber", "amber", "box_was_busy"))
        if (r.get("n_runs") or 1) < 2 or run_class == "B":
            reasons.append(_reason("amber", "amber", "single_run"))
        age = r.get("serving_age_min")
        if run_class == "A" and r.get("locality") == "local" and age is not None and age < c.FRESH_MIN:
            reasons.append(_reason("amber", "amber", "first_15_min"))
        if summary.get("page_check") == "blank":
            reasons.append(_reason("amber", "amber", "page_check_failed"))
    hits = privacy_check(public_texts, private_words)
    if hits:
        reasons.append(_reason("R4", "red", "privacy_hit", {"kinds": hits}))
    if protocol_version != c.PROTOCOL_V1:
        reasons.append(_reason("amber", "amber", "method_v0"))
    if truncation:
        reasons.append(_reason("amber", "amber", "runs_truncated",
                               {"runs_cut": truncation["runs_cut"], "n_runs": truncation["n_runs"]},
                               "rerun_larger" if truncation.get("larger_budget") else None))
    # one reason per (rule, code)
    seen: set[tuple] = set()
    unique: list[dict] = []
    for r in reasons:
        key = (r["rule"], r["code"], tuple(sorted((k, str(v)) for k, v in r["params"].items())))
        if key not in seen:
            seen.add(key)
            unique.append(r)
    status = "red" if any(r["severity"] == "red" for r in unique) else ("amber" if unique else "green")
    sums = [s for _r, _rec, s in receipts if s]
    return {"status": status, "reasons": unique, "checksum": c.checksum(sums) if sums else None}


def privacy_check(texts: list[str], private_words: list[str]) -> list[str]:
    return c.privacy_hits(texts, private_words)


async def private_words(session: AsyncSession) -> list[str]:
    """What must never reach an artefact: operator/user names, agent names,
    host slugs and addresses (R4 / G13)."""
    from app.models.agent import Agent
    from app.models.host import Host
    from app.models.user import User

    from .w1_kit import HARNESS_WORDS

    harness_text = " ".join(HARNESS_WORDS.values()).casefold()
    words: list[str] = []
    for u in (await session.exec(select(User))).all():
        for part in (u.name or "").split():
            words.append(part)
        if u.email:
            words.append(u.email.split("@")[0])
    for a in (await session.exec(select(Agent))).all():
        # An agent called like a harness ("Grok") would turn every Grok
        # Build test red — the harness word is public by design.
        if a.name and a.name.casefold() not in harness_text:
            words.append(a.name)
    for h in (await session.exec(select(Host))).all():
        public = (h.display_name or "").casefold().replace("-", " ")
        for v in (h.slug, h.ssh_host, h.tailscale_host):
            # A slug that only repeats the operator's own display name
            # ("dgx-spark" for "DGX Spark 1") is not a leak; addresses are
            # always caught by the IP / host-name patterns as well.
            if v and v.casefold().replace("-", " ") not in public:
                words.append(v)
    return [w for w in words if w and len(w) >= 3]
