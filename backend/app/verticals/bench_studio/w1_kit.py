"""Bench W1 X-Kit — English public texts, the card/video payload, files.

Everything public is built from the frozen receipts (one record, every
artefact): the settings text (§5.5, ≤ 280 chars per block), the alt text
(≤ 1 000 chars, numbers spelled exactly as on the card), facts (only green
numbers, unranked), the thread guide, and the payload the renderer turns
into the 16:9 receipt card and the 1080×1350 card PNG (ONE template).

Never printed (concept §6.4 + decision 24.10.): the token-limit cut (that
warning is private, on the result page), cross-harness ratios, energy, a
bare $ on a local/subscription run, tok/s from wall time, agent names, IPs,
hosts, paths. The footer is the operator's handle from ``MC_BENCH_FOOTER``
(his ``.env``), never a value in the repo.
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from . import w1_common as c

SETTINGS_BLOCK_MAX = 280
ALT_TEXT_MAX = 1000
FOOTER_ENV = "MC_BENCH_FOOTER"

HARNESS_WORDS = {"api": "raw API", "omp": "omp agent", "grok-build": "Grok Build",
                 "hermes": "Hermes agent", "claude-code": "Claude Code"}
EFFORT_WORDS = {"default": "default effort", "off": "effort off", "on": "effort on", "low": "effort low",
                "medium": "effort medium", "high": "effort high", "max": "effort max"}

THREAD_GUIDE = [
    "Main post: your words + video.mp4",
    "Reply 1: card.png + alt text",
    "Then: one reply per settings block (each at most 280 characters)",
]
THREAD_LINK = "Last reply: link"

#: Amber notes that may be printed in the settings text. ``runs_truncated``
#: is deliberately missing: the token-limit cut is the operator's private
#: warning, never public (decision 24.10.2026).
PUBLIC_AMBER = {
    "effort_unknown": "effort unknown",
    "box_was_busy": "box was busy, speed not comparable",
    "single_run": "single run",
    "first_15_min": "measured in the first 15 min after start",
    "page_check_failed": "page did not load",
    "network_assets_blocked": "web fonts/images blocked (offline render)",
    "three_version_differs": "page asked for another three.js version than the recorder serves",
    "lib_tracks_mixed": "not every page used three.js (separate lanes)",
    "method_v0": "old method (v0)",
}


def footer() -> str:
    return (os.environ.get(FOOTER_ENV) or "").strip()


def _date(created: datetime | None) -> str:
    created = c.as_aware(created) or c.now_utc()
    try:
        from zoneinfo import ZoneInfo

        from app.config import settings

        tz = ZoneInfo(settings.usage_timezone) if settings.usage_timezone else None
    except Exception:  # noqa: BLE001
        tz = None
    return (created.astimezone(tz) if tz else created).date().isoformat()


def _where(r: dict[str, Any]) -> str:
    if r.get("locality") == "local":
        return "local"
    return "cloud"


def _place(r: dict[str, Any]) -> str | None:
    if r.get("locality") == "local":
        return r.get("device_name")
    return r.get("provider")


def _effort_word(r: dict[str, Any]) -> str:
    if r.get("run_class") == "B":
        obs = (r.get("effort") or {}).get("observed") or {}
        level = obs.get("level") if isinstance(obs, dict) else None
        return f"effort {level}" if level else "session default effort"
    eff = r.get("effort") or {}
    requested = eff.get("requested") or "default"
    if requested == "default" and eff.get("default_level"):
        # One name per level (K10): the sheet says "hoch (Standard)", the
        # card says the same in English.
        return f"effort {eff['default_level']} (default)"
    return EFFORT_WORDS.get(requested, "default effort")


def three_label(r: dict[str, Any]) -> str | None:
    """'three.js r170' when the recorder served MC's three.js to this
    contestant (lane lib:three-local), else None."""
    rec = r.get("receipt") or {}
    if c.lib_lane(rec) != c.LANE_THREE_LOCAL:
        return None
    provided = rec.get("libraries_provided") or []
    return provided[0] if provided else "three.js"


def chips(r: dict[str, Any], *, compact: bool = False) -> str:
    """'local · DGX Spark ×2 · raw API · default effort' (compact for a duo
    pane: no device). A page that got MC's three.js says so: '… · three.js r170'."""
    parts = [_where(r)]
    place = _place(r)
    if place and not compact:
        parts.append(place)
    parts.append(HARNESS_WORDS.get(r.get("harness") or "api", r.get("harness") or ""))
    parts.append(_effort_word(r))
    parts.append(three_label(r) or "")
    return " · ".join(p for p in parts if p)


def _three_disclosure(r: dict[str, Any], rec: dict[str, Any]) -> list[str]:
    label = three_label(r)
    if not label:
        return []
    asked = ", ".join(rec.get("three_requested") or [])
    served = f"{label} served offline by the recorder"
    if asked and rec.get("three_version_mismatch"):
        served += f" (page asked for {asked})"
    return [served, "page loads three.js from a CDN"]


def _planned(r: dict[str, Any]) -> int:
    return int(r.get("n_runs") or 1)


def _runs(r: dict[str, Any]) -> int:
    """Runs BEHIND the printed median — the measured count, never the
    planned one (a failed run is not in the median). ONE rule for the foot
    line, the number subtitles, the alt text and the settings text."""
    s = r.get("summary") or {}
    for field in ("time", "tokens_out"):
        st = s.get(field) or {}
        if st.get("measured") and st.get("n"):
            return int(st["n"])
    return _planned(r)


def _runs_sub(r: dict[str, Any]) -> str:
    """'median of 3' · 'median of 2 of 3' · 'single run' · '1 of 3 runs'."""
    n, planned = _runs(r), _planned(r)
    if n > 1:
        return f"median of {n}" if n >= planned else f"median of {n} of {planned}"
    return "single run" if planned <= 1 else f"1 of {planned} runs"


def numbers(r: dict[str, Any]) -> list[dict[str, Any]]:
    """The max-3 headline numbers of a contestant, formatted once — card,
    receipt card and alt text all print these exact strings."""
    s = r["summary"]
    if r.get("run_class") == "B":
        out = [
            _num(s["time"], c.fmt_minutes, "work time", "no waiting" if not s.get("wait_ms") else None,
                 alt="work time {v}"),
            _num(s["tokens_out"], c.fmt_tokens, "output tokens", None, alt="{v} output tokens"),
            {"value": "n=1", "label": "single run", "sub": None, "measured": True, "plain": True,
             "alt": "single run"},
        ]
        return out
    sub = _runs_sub(r)
    out = [
        _num(s["time"], c.fmt_clock, "compute time", sub, alt="compute time {v} (" + sub + ")"),
        _num(s["tokens_out"], c.fmt_tokens, "output tokens", sub, alt="{v} output tokens"),
    ]
    dec = s.get("decode_tok_s")
    if dec and dec.get("measured"):
        out.append(_num(dec, c.fmt_rate, "tok/s decode", None, alt="{v} tok/s decode"))
    return out


def _num(st: dict[str, Any], fmt, label: str, sub: str | None, *, alt: str) -> dict[str, Any]:
    if st and st.get("measured"):
        value = fmt(st["median"])
        return {"value": value, "label": label, "sub": sub, "measured": True, "alt": alt.format(v=value)}
    return {"value": "not measured", "label": label, "sub": None, "measured": False,
            "alt": f"{label} not measured"}


def kind_word(run_class: str) -> str:
    return "agent build" if run_class == "B" else "same-prompt showdown"


def card_payload(detail: dict[str, Any]) -> dict[str, Any]:
    """What the renderer draws on the receipt card (16:9, 4.5 s) and the
    card PNG (1080×1350) — one payload, one template."""
    video = video_contestants(detail)
    run_class = video[0].get("run_class", "A") if video else "A"
    ns = [_runs(r) for r in video] or [1]
    failed = sum(max(0, _planned(r) - _runs(r)) for r in video)
    if run_class == "B":
        foot = "agent build · " + ("method v0" if detail.get("protocol_version") != c.PROTOCOL_V1 else "single run")
    elif max(ns) > 1:
        if len(set(ns)) == 1:
            foot = f"median of {ns[0]} runs" + (" each" if len(video) > 1 else "")
        else:
            foot = "median of " + " / ".join(str(n) for n in ns) + " runs"
        foot += " · same prompt"
    else:
        foot = "single run · same prompt"
    if run_class != "B" and failed:
        # Every attempt is counted (concept §5): a run that failed is said.
        foot += f" · {failed} run{'s' if failed > 1 else ''} failed"
    return {
        "layout": "duo" if len(video) == 2 else "solo",
        "run_class": run_class,
        "top": f"{detail['prompt']['title']} · {kind_word(run_class)} · {_date(detail.get('created_at'))}",
        "contestants": [
            {
                "model": r["served_model_id"] or r["display_name"],
                # The card has room for the full line (device + provider are
                # the story of a local-vs-cloud post); only the 840 px video
                # pane gets the compact one.
                "chips": chips(r),
                "chips_short": chips(r, compact=True),
                "numbers": numbers(r),
            }
            for r in video
        ],
        "foot": foot,
        "handle": footer(),
        "checksum": (detail.get("gate") or {}).get("checksum"),
    }


def video_contestants(detail: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for block in detail.get("blocks", []):
        for r in block["contestants"]:
            if r.get("_video"):
                out.append(r)
    if not out:
        for block in detail.get("blocks", [])[:1]:
            out = list(block["contestants"][: c.MAX_VIDEO_CONTESTANTS])
    return out[: c.MAX_VIDEO_CONTESTANTS]


def frame_payload(detail: dict[str, Any]) -> dict[str, Any]:
    """Chips ABOVE each pane, two lines (model; where · harness · effort)."""
    video = video_contestants(detail)
    run_class = video[0].get("run_class", "A") if video else "A"
    return {
        "top": f"{detail['prompt']['title']} · {kind_word(run_class)} · {_date(detail.get('created_at'))}",
        "panes": [
            {"model": r["served_model_id"] or r["display_name"], "chips": chips(r, compact=len(video) == 2)}
            for r in video
        ],
        "handle": footer(),
    }


# ── Copy texts ───────────────────────────────────────────────────────────


def _fit(parts: list[str], limit: int = SETTINGS_BLOCK_MAX) -> str:
    out = ""
    for p in parts:
        if not p:
            continue
        cand = p if not out else f"{out} · {p}"
        if len(cand) > limit:
            break
        out = cand
    return out


def _budget_text(rec: dict[str, Any]) -> str:
    """How max_tokens was set (25.10.2026): a local run has the whole
    context window (or the engine's own maximum), a paid run a cap."""
    rule = rec.get("max_tokens_rule")
    if rule == "engine":
        return "max_tokens engine max"
    value = rec.get("max_tokens") or c.BASE_MAX_TOKENS
    return f"max_tokens {value} (full context)" if rule == "context" else f"max_tokens {value}"


def settings_blocks(detail: dict[str, Any]) -> list[str]:
    """The disclosure line (§5.5), machine-filled, fixed order, English."""
    blocks: list[str] = []
    sha = (detail.get("prompt") or {}).get("sha256") or ""
    for block in detail.get("blocks", []):
        rs = block["contestants"]
        if not rs:
            continue
        first = rs[0]
        rec = first.get("receipt") or {}
        if block["run_class"] == "A":
            n = rec.get("n_runs") or first.get("n_runs") or 1
            counted = min(_runs(r) for r in rs)
            runs = f"n={n}" if counted >= n else f"n={n} ({n - counted} failed)"
            head = ["Setup · model only", f"same prompt (sha {sha[:7]})",
                    f"{runs}, video = run {c.VIDEO_RUN_INDEX}" if n > 1 else runs,
                    f"temp {rec.get('temperature', c.TEMPERATURE)}",
                    _budget_text(rec)]
            if rec.get("time_limit_min"):
                head.append(f"time limit {rec['time_limit_min']} min")
        else:
            limit = rec.get("time_limit_min")
            head = ["Setup · agent build", f"same brief (sha {(rec.get('brief_sha') or sha)[:7]})", "n=1",
                    "tools allowed", f"time limit {limit} min" if limit else "time limit not recorded"]
        blocks.append(_fit(head))
        for r in rs:
            rec = r.get("receipt") or {}
            unknown = rec.get("unknown") or {}
            engine = rec.get("engine") or (
                "unknown (hand-made runtime)" if unknown.get("engine") == "hand_made_runtime" else "unknown"
            )
            parts = [r["served_model_id"] or r["display_name"], _where(r), _place(r) or "",
                     f"engine: {engine}"]
            if rec.get("quant"):
                parts.append(f"quant {rec['quant']}")
            if r.get("locality") == "local":
                parts.append(f"GPU mode {rec.get('gpu_mode')}" if rec.get("gpu_mode") else "GPU mode unknown")
                parts.append(f"spec-decoding: {rec.get('spec_decoding') or 'unknown'}")
            else:
                parts.append("cloud build unknown")
            parts.append(HARNESS_WORDS.get(r.get("harness") or "api", ""))
            blocks.append(_fit(parts))
            tail = [_effort_disclosure(r, rec)]
            if rec.get("network_blocked"):
                tail.append("network blocked during render")
            tail += _three_disclosure(r, rec)
            tail.append(f"method {detail.get('protocol_version', c.PROTOCOL_V1)}")
            tail.append(_date(detail.get("created_at")))
            cost = r["summary"]["cost"]
            if cost.get("cost_kind") in ("local", "subscription") and cost.get("hypothetical_usd"):
                tail.append(f"API list price would be ≈ {c.fmt_usd(cost['hypothetical_usd'])} (comparison only)")
            elif cost.get("cost_kind") == "api_price" and cost.get("usd") is not None:
                tail.append(f"cost {c.fmt_usd(cost['usd'])} at API list price")
            blocks.append(_fit(tail))
    notes = [PUBLIC_AMBER[r["code"]] for r in (detail.get("gate") or {}).get("reasons", [])
             if r.get("severity") == "amber" and r.get("code") in PUBLIC_AMBER]
    tail = []
    if notes:
        tail.append("notes: " + ", ".join(dict.fromkeys(notes)))
    checksum = (detail.get("gate") or {}).get("checksum")
    if checksum:
        tail.append(f"record {checksum[:7]}")
    if tail:
        blocks.append(_fit(tail))
    return [b for b in blocks if b]


def _effort_disclosure(r: dict[str, Any], rec: dict[str, Any]) -> str:
    eff = r.get("effort") or {}
    obs = eff.get("observed") or rec.get("effort_observed") or {}
    if r.get("run_class") == "B":
        level = obs.get("level") if isinstance(obs, dict) else None
        return f"effort: session default (observed: {level})" if level else "effort: not set (D5), not observed"
    requested = eff.get("requested") or "default"
    thinking = obs.get("thinking") if isinstance(obs, dict) else None
    if requested == "default":
        # Same name as on the card ("effort high (default)", K10).
        level = f" = {eff['default_level']}" if eff.get("default_level") else ""
        return f"effort: server default{level} (thinking observed: {thinking or 'unknown'})"
    sent = eff.get("sent") or rec.get("effort_sent") or {}
    wire = f"{sent.get('key')}={sent.get('value')}" if sent.get("key") else "nothing sent"
    return f"effort: {requested} ({wire}; thinking observed: {thinking or 'unknown'})"


def alt_text(detail: dict[str, Any]) -> str:
    card = card_payload(detail)
    sentences = [f"Result card: {card['top']}."]
    for r, cc in zip(video_contestants(detail), card["contestants"]):
        where = (f"running locally on {r.get('device_name')}" if r.get("locality") == "local" and r.get("device_name")
                 else ("running locally" if r.get("locality") == "local"
                       else f"in the cloud via {r.get('provider') or 'an API'}"))
        nums = [nb["alt"] for nb in cc["numbers"]]
        sentences.append(f"{cc['model']}, {where}, {HARNESS_WORDS.get(r.get('harness') or 'api', '')}, "
                         f"{_effort_word(r)}: " + ", ".join(nums) + ".")
    sentences.append(card["foot"].capitalize() + ".")
    text = " ".join(sentences)
    return text if len(text) <= ALT_TEXT_MAX else text[: ALT_TEXT_MAX - 1].rstrip() + "…"


def facts(detail: dict[str, Any]) -> list[str]:
    """3–5 plain sentences from GREEN numbers only, unranked, no draft post.
    Red gate = no facts. A busy box makes speed not comparable: no tempo
    facts then."""
    gate = detail.get("gate") or {}
    if gate.get("status") in ("red", "pending"):
        return []
    codes = {r.get("code") for r in gate.get("reasons", [])}
    out: list[str] = []
    for block in detail.get("blocks", []):
        for r in block["contestants"]:
            s = r["summary"]
            name = r["served_model_id"] or r["display_name"]
            n = s["time"].get("n") or 1
            if r.get("run_class") == "B":
                if s["time"].get("measured"):
                    out.append(f"{name}, {_where(r)}, agent build with {HARNESS_WORDS.get(r.get('harness') or '', '')}: "
                               f"{c.fmt_minutes(s['time']['median'])} work time, single run.")
                if s["tokens_out"].get("measured"):
                    out.append(f"Output tokens: {c.fmt_tokens(s['tokens_out']['median'])}.")
                continue
            t = s["time"]
            if t.get("measured"):
                rng = (f"{c.fmt_clock(t['min'])}–{c.fmt_clock(t['max'])}" if n > 1 else c.fmt_clock(t["median"]))
                out.append(f"{name}, {_where(r)}, {n} run{'s' if n > 1 else ''}: {rng} compute time.")
            tok = s["tokens_out"]
            if tok.get("measured"):
                rng = (f"{c.fmt_tokens(tok['min'])}–{c.fmt_tokens(tok['max'])}" if n > 1
                       else c.fmt_tokens(tok["median"]))
                out.append(f"Output tokens per run: {rng}.")
            dec = s.get("decode_tok_s")
            if dec and dec.get("measured") and "box_was_busy" not in codes:
                rng = (f"{c.fmt_rate(dec['min'])}–{c.fmt_rate(dec['max'])}" if n > 1 else c.fmt_rate(dec["median"]))
                ttft = s.get("ttft_ms")
                extra = f", first token after {c.fmt_seconds(ttft['median'])}" if ttft and ttft.get("measured") else ""
                out.append(f"Decode speed: {rng} tok/s{extra}.")
    return out[:5]


def episode_link(challenge_id) -> str | None:
    """The public episode URL — only when the (private) catalog publisher
    has published this run (its state file carries the URL)."""
    import json

    from .orchestrator import challenge_dir

    try:
        state = json.loads((challenge_dir(challenge_id) / "publish" / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = state.get("url") if isinstance(state, dict) else None
    return url if isinstance(url, str) and url.startswith("https://") else None


def kit_texts(detail: dict[str, Any]) -> dict[str, Any]:
    link = episode_link(detail["id"])
    return {
        "settings": settings_blocks(detail),
        "alt_text": alt_text(detail),
        "facts": facts(detail),
        "link": link,
        "thread_guide": list(THREAD_GUIDE) + ([THREAD_LINK] if link else []),
    }


def public_strings(detail: dict[str, Any]) -> list[str]:
    """Every string that lands on a public artefact (gate R4 scans these;
    the operator's own footer handle is not scanned)."""
    card = card_payload(detail)
    frame = frame_payload(detail)
    strings = [card["top"], card["foot"], frame["top"]]
    for cc in card["contestants"]:
        strings += [cc["model"], cc["chips"]] + [n["value"] for n in cc["numbers"]]
    for p in frame["panes"]:
        strings += [p["model"], p["chips"]]
    strings += settings_blocks({**detail, "gate": {**(detail.get("gate") or {}), "reasons": []}})
    strings.append(alt_text(detail))
    return strings


# ── Files, zip, download token ───────────────────────────────────────────

KIT_TOKEN_SCOPE = "bench_kit"
KIT_TOKEN_MINUTES = 30
VIDEO_SIZE = (1920, 1080)
CARD_SIZE = (1080, 1350)


def create_download_token(user_id: str, test_id: str, minutes: int = KIT_TOKEN_MINUTES) -> str:
    """Resource-scoped, short-lived (an <a href> cannot send the Bearer
    header; the session JWT never goes into a URL). ``require_user``
    refuses any token with a ``scope`` claim, so this one only ever opens
    the kit files of this one test."""
    from datetime import timedelta, timezone

    from jose import jwt

    from app.auth import JWT_ALGORITHM
    from app.config import settings

    now = datetime.now(timezone.utc)
    payload = {"sub": user_id, "scope": KIT_TOKEN_SCOPE, "test_id": test_id,
               "exp": now + timedelta(minutes=minutes), "iat": now}
    return jwt.encode(payload, settings.jwt_secret_key, algorithm=JWT_ALGORITHM)


def verify_download_token(token: str, test_id: str) -> bool:
    from jose import JWTError, jwt

    from app.auth import JWT_ALGORITHM
    from app.config import settings

    try:
        payload = jwt.decode(token, settings.jwt_secret_key, algorithms=[JWT_ALGORITHM])
    except JWTError:
        return False
    return payload.get("scope") == KIT_TOKEN_SCOPE and payload.get("test_id") == test_id


def _slug(text: str, limit: int = 48) -> str:
    import re
    import unicodedata

    ascii_text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    slug = re.sub(r"[^A-Za-z0-9.]+", "-", ascii_text).strip("-.").lower()
    return slug[:limit].strip("-") or "bench"


def download_filename(detail: dict[str, Any], ext: str) -> str:
    """'<model>-<prompt>-<date>.<ext>' — what iOS "Save" keeps."""
    video = video_contestants(detail)
    model = (video[0]["served_model_id"] or video[0]["display_name"]) if video else "bench"
    return f"{_slug(model)}-{_slug(detail['prompt']['title'], 32)}-{_date(detail.get('created_at'))}.{ext}"


def artefact_paths(challenge, detail: dict[str, Any]) -> dict[str, Any]:
    """Current artefacts on disk. A file whose name carries an OLDER record
    checksum is stale (numbers were reloaded since) and not offered."""
    from pathlib import Path

    from .w1_runner import card_path

    checksum = (detail.get("gate") or {}).get("checksum")
    out: dict[str, Any] = {"video.mp4": None, "card.png": None}
    if challenge.composed_video_path:
        p = Path(challenge.composed_video_path)
        fresh = not p.name.startswith("video-") or (checksum and checksum[:12] in p.name)
        if p.is_file() and fresh:
            out["video.mp4"] = p
    card = card_path(challenge.id, checksum)
    if checksum and card.is_file():
        out["card.png"] = card
    return out


def kit_files(challenge, detail: dict[str, Any]) -> list[dict[str, Any]]:
    paths = artefact_paths(challenge, detail)
    files: list[dict[str, Any]] = []
    v = paths["video.mp4"]
    vd: dict[str, Any] = {"name": "video.mp4", "ready": v is not None}
    if v is not None:
        vd["bytes"] = v.stat().st_size
        if v.name.startswith("video-"):
            vd["width"], vd["height"] = VIDEO_SIZE
        vd["duration_s"] = (challenge.plan or {}).get("video_seconds")
    files.append(vd)
    cpath = paths["card.png"]
    cd: dict[str, Any] = {"name": "card.png", "ready": cpath is not None}
    if cpath is not None:
        cd["bytes"] = cpath.stat().st_size
        cd["width"], cd["height"] = CARD_SIZE
    files.append(cd)
    ready = [p for p in (v, cpath) if p is not None]
    zd: dict[str, Any] = {"name": "kit.zip", "ready": bool(ready)}
    if ready:
        zd["bytes"] = sum(p.stat().st_size for p in ready)
    files.append(zd)
    return files


def _zip_key(paths: dict[str, Any], texts: dict[str, Any]) -> str:
    """What is IN the zip: the files (name, size, mtime) and the texts. A
    compose that adds the card or a new video behind an unchanged record
    checksum gives a new key, so a stale zip is never served."""
    import hashlib
    import json

    parts: list[Any] = []
    for name in ("video.mp4", "card.png"):
        p = paths.get(name)
        if p is not None:
            st = p.stat()
            parts.append([name, p.name, st.st_size, int(st.st_mtime)])
    parts.append(texts)
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:8]


def build_zip(challenge, detail: dict[str, Any]):
    """kit.zip = video + card + the copy texts, cached per record checksum
    AND content key (blocking file I/O: callers run it in a thread)."""
    import uuid as _uuid
    import zipfile
    from pathlib import Path

    from .orchestrator import challenge_dir

    checksum = (detail.get("gate") or {}).get("checksum") or "nochecksum"
    paths = artefact_paths(challenge, detail)
    texts = kit_texts(detail)
    out = challenge_dir(challenge.id) / f"kit-{checksum[:12]}-{_zip_key(paths, texts)}.zip"
    if out.is_file():
        return out
    out.parent.mkdir(parents=True, exist_ok=True)
    for old in out.parent.glob(f"kit-{checksum[:12]}-*.zip"):
        try:
            old.unlink()
        except OSError:
            pass
    tmp = Path(f"{out}.{_uuid.uuid4().hex[:8]}.tmp")  # two parallel taps never share a temp file
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_STORED) as z:
            if paths["video.mp4"] is not None:
                z.write(paths["video.mp4"], download_filename(detail, "mp4"))
            if paths["card.png"] is not None:
                z.write(paths["card.png"], download_filename(detail, "png"))
            z.writestr("settings.txt", "\n\n".join(texts["settings"]) + "\n")
            z.writestr("alt-text.txt", texts["alt_text"] + "\n")
            z.writestr("facts.txt", "\n".join(texts["facts"]) + "\n")
            z.writestr("thread.txt", "\n".join(texts["thread_guide"]) + "\n")
            if texts.get("link"):
                z.writestr("link.txt", texts["link"] + "\n")
        tmp.replace(out)
    finally:
        tmp.unlink(missing_ok=True)
    return out


def kit(challenge, detail: dict[str, Any]) -> dict[str, Any]:
    gate = detail["gate"]
    files = kit_files(challenge, detail)
    finished = detail["state"] in ("done", "done_missing")
    downloadable = finished and gate["status"] in ("green", "amber") and any(f["ready"] for f in files)
    blocked = None
    if gate["status"] == "red":
        red = next((r for r in gate.get("reasons", []) if r["severity"] == "red"), None)
        blocked = {"code": "gate_red", "params": {"rule": red["rule"] if red else "R1"}}
    elif gate["status"] == "pending" or not finished:
        blocked = {"code": "runs_pending", "params": {}}
    elif not any(f["ready"] for f in files):
        blocked = {"code": "files_not_ready", "params": {}}
    return {
        "test_id": challenge.id,
        "gate": gate,
        "downloadable": downloadable,
        "blocked_reason": blocked,
        "files": files,
        "texts": kit_texts(detail),
        "footer": footer(),
        "truncation": detail.get("truncation"),
    }
