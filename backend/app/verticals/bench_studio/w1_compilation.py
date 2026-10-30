"""Bench compilation (phase 1, 30.10.2026) — 1–4 FINISHED bench runs in one
4:5 video. No new runs: only the recordings and the frozen receipts.

Sequence (``build_spec``), 2–4 runs:
    hook (the one honest headline) → one segment per run (header band, the
    untouched recording, stats + racing bars + footnote below it) → the
    numbers / ranking → the details (pick + how it was measured).
Solo (one run): hook (one hero number) → the build, longer (stats, the
    share of its time limit, effort/runs/harness) → the details. No
    comparison bars against nothing, no ranking of one.

The renderer (mc-playwright ``POST /compilation``, docker/mc-playwright/
compilation.py) only lays out and encodes — every public word is built
HERE, in English, and privacy-checked like the W1 kit (R4: no agent names,
IPs, hosts, paths, UUIDs).

Honesty rules of this format:
- A headline ratio ("2.4× faster") only between runs of the SAME brief,
  the same run class and the same harness — never across different tasks,
  never across harnesses (W1 rule). Different briefs get totals instead.
- Electricity is arithmetic, never a meter reading: nodes × the power
  supply rating × wall time × price per kWh, and it says so. Rating and
  price are configuration (``MC_BENCH_POWER_W_PER_NODE``,
  ``MC_BENCH_ENERGY_PRICE`` in USD per kWh); without a price no
  electricity number is printed at all.
- Every money figure is USD (operator decision 30.10.2026): the power
  estimate and a cloud run's API list price. The power figure is always
  labelled "power, at most" + "arithmetic" — never a spent amount. The API
  list price of a LOCAL run is comparison only and stays out of the video.
- Output tokens of an agent build are the SUM of its model_usage_events
  (the receipt holds a snapshot taken at harvest time, which misses calls
  that land later); a direct API run keeps its receipt median.
- Rating and pick are the operator's opinion. They are render input, not
  data (phase 1 has no storage for them): absent → the badge, the ranking
  order and the pick are simply left out. A placeholder is marked
  ``rating_is_example`` and printed as "example rating".
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping, Optional

from pydantic import BaseModel, Field, model_validator

from . import w1_common as c
from .w1_kit import EFFORT_WORDS, HARNESS_WORDS, footer

MIN_ENTRIES = 1
MAX_ENTRIES = 4
#: Seconds per part (the reference posts run 29–40 s; 3 runs = 35 s).
HOOK_S = 3.5
RANKING_S = 5.0
CLOSING_S = 6.0
ENTRY_S = {2: 8.5, 3: 7.0, 4: 6.0}
#: Solo: hook 3.5 + the build 13 + details 6 = 22.5 s.
SOLO_ENTRY_S = 13.0
#: Where in the (20 s) recording each part starts — the hook and the
#: ranking show a later moment than the run's own segment.
ENTRY_ART_START_S = 1.0
HOOK_ART_START_S = 10.0
RANKING_ART_START_S = 14.0
#: A ratio below this is not a headline.
MIN_HEADLINE_RATIO = 1.2

POWER_ENV = "MC_BENCH_POWER_W_PER_NODE"
PRICE_ENV = "MC_BENCH_ENERGY_PRICE"
#: DGX Spark: 240 W power supply (NVIDIA spec) — a ceiling, the box draws less.
DEFAULT_POWER_W_PER_NODE = 240.0
#: Every money figure in the video is USD (operator decision 30.10.2026).
USD = "$"

#: Published unified memory per node, for the hardware line ("2× DGX Spark ·
#: 256 GB"). A device not listed simply gets no memory figure.
DEVICE_MEMORY_GB = {"dgx spark": 128}

FOOTER_LINE = "AI-generated pages from real runs"
REASON_MAX = 140
COMPILATIONS_DIR = "bench-compilations"


# ── Input ────────────────────────────────────────────────────────────────


class CompilationEntryIn(BaseModel):
    test_id: str
    #: Optional: one exact run of the test (default: the video run).
    entry_id: Optional[str] = None
    rating: Optional[float] = Field(default=None, ge=0, le=10)


class CompilationIn(BaseModel):
    """What the operator picks (phase 2 sends this from the bench page)."""
    entries: list[CompilationEntryIn] = Field(min_length=MIN_ENTRIES, max_length=MAX_ENTRIES)
    pick_test_id: Optional[str] = None
    pick_reason: Optional[str] = Field(default=None, max_length=REASON_MAX)
    #: True = the ratings/pick are placeholders and print as "example".
    rating_is_example: bool = False

    @model_validator(mode="after")
    def _check(self) -> "CompilationIn":
        ids = [e.test_id for e in self.entries]
        if len(set(ids)) != len(ids):
            raise ValueError("each test only once")
        if self.pick_test_id is not None and self.pick_test_id not in ids:
            raise ValueError("the pick must be one of the entries")
        if self.pick_reason and self.pick_test_id is None:
            raise ValueError("a reason needs a pick")
        return self


@dataclass(frozen=True)
class EnergyConfig:
    watts_per_node: float
    #: USD per kWh.
    price_per_kwh: Optional[float]

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "EnergyConfig":
        env = os.environ if env is None else env

        def num(key: str) -> Optional[float]:
            raw = (env.get(key) or "").strip()
            try:
                value = float(raw)
            except ValueError:
                return None
            return value if value > 0 else None

        return cls(
            watts_per_node=num(POWER_ENV) or DEFAULT_POWER_W_PER_NODE,
            price_per_kwh=num(PRICE_ENV),
        )


# ── One run, flattened from its DB rows ──────────────────────────────────


def source_from_rows(
    challenge: Mapping[str, Any], entry: Mapping[str, Any], usage_tokens_out: Optional[int] = None
) -> dict[str, Any]:
    """Everything the video needs about ONE run, from the challenge row, the
    entry row (with its frozen receipt) and the summed usage events. Pure:
    the DB loader and the offline example render use the same function."""
    rec = dict(entry.get("receipt") or {})
    metrics = dict(entry.get("metrics") or {})
    gate = dict(entry.get("gate") or {})
    run_class = rec.get("run_class") or entry.get("run_class") or "A"
    tokens = rec.get("tokens_out_median")
    tokens_source = "receipt"
    if run_class == "B" and usage_tokens_out:
        tokens, tokens_source = int(usage_tokens_out), "usage_events"
    effort_obs = rec.get("effort_observed") or metrics.get("effort_observed") or {}
    return {
        "test_id": str(challenge["id"]),
        "entry_id": str(entry.get("id") or ""),
        "title": challenge.get("title") or "Untitled",
        "created_at": challenge.get("created_at"),
        "prompt_sha": challenge.get("prompt_sha256") or rec.get("prompt_sha256"),
        "run_class": run_class,
        "harness": rec.get("harness") or entry.get("harness") or "api",
        "model": rec.get("served_model_id") or rec.get("display_name") or entry.get("model_label") or "",
        "locality": rec.get("locality") or metrics.get("locality"),
        "device": rec.get("device"),
        "nodes": rec.get("nodes"),
        "provider": rec.get("provider"),
        "quant": rec.get("quant"),
        "effort_requested": rec.get("effort_requested") or entry.get("effort_requested") or "default",
        "effort_level": effort_obs.get("level") if isinstance(effort_obs, dict) else None,
        "n_runs": int(rec.get("n_runs") or 1),
        "time_ms": rec.get("time_median"),
        "time_limit_min": rec.get("time_limit_min"),
        "tokens_out": tokens,
        "tokens_source": tokens_source,
        "decode_tok_s": rec.get("decode_tok_s_median"),
        "cost_kind": rec.get("cost_kind"),
        "cost_usd": rec.get("cost_usd"),
        "cost_hypothetical_usd": rec.get("cost_hypothetical_usd"),
        "network_blocked": rec.get("network_blocked"),
        "video_path": entry.get("video_path"),
        "screenshot_path": entry.get("screenshot_path"),
        "gate_status": gate.get("status"),
        "checksum": gate.get("checksum") or rec.get("_checksum"),
    }


# ── Words and numbers ────────────────────────────────────────────────────


def fmt_min(ms: Optional[float]) -> Optional[str]:
    """'79.5' below 100 minutes, '119' above — the unit is printed apart."""
    if ms is None:
        return None
    minutes = ms / 60_000
    if minutes < 10:
        return f"{minutes:.1f}"
    return f"{minutes:.1f}" if minutes < 100 else str(int(round(minutes)))


def fmt_money(v: Optional[float]) -> Optional[str]:
    if v is None:
        return None
    return "<0.01" if v < 0.01 else f"{v:.2f}"


def _date(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return c.as_aware(value)
    if isinstance(value, str) and value:
        try:
            return c.as_aware(datetime.fromisoformat(value.replace("Z", "+00:00")))
        except ValueError:
            return None
    return None


def fmt_day(value: Any) -> str:
    d = _date(value)
    return f"{d:%b} {d.day}, {d.year}" if d else ""


def date_span(sources: list[dict[str, Any]]) -> str:
    days = sorted({_date(s["created_at"]).date() for s in sources if _date(s["created_at"])})
    if not days:
        return ""
    a, b = days[0], days[-1]
    if a == b:
        return f"{a:%b} {a.day}, {a.year}"
    if a.year == b.year and a.month == b.month:
        return f"{a:%b} {a.day}–{b.day}, {a.year}"
    return f"{a:%b} {a.day} – {b:%b} {b.day}, {b.year}"


def device_base(device: Optional[str]) -> str:
    return re.sub(r"\s*[×x]\s*\d+\s*$", "", device or "").strip()


def hardware_line(s: dict[str, Any]) -> str:
    """'2× DGX Spark · 256 GB' (local) · 'Cloud · anthropic' (API)."""
    if s.get("locality") == "cloud":
        return f"Cloud · {s['provider']}" if s.get("provider") else "Cloud"
    if s.get("locality") != "local":
        return "Setup not recorded"
    base = device_base(s.get("device")) or "Local"
    nodes = int(s.get("nodes") or 1)
    line = f"{nodes}× {base}" if nodes > 1 else base
    mem = DEVICE_MEMORY_GB.get(base.casefold())
    return f"{line} · {nodes * mem} GB" if mem else line


def effort_text(s: dict[str, Any]) -> str:
    if s.get("run_class") == "B":
        return f"effort {s['effort_level']}" if s.get("effort_level") else "session default effort"
    return EFFORT_WORDS.get(s.get("effort_requested") or "default", "default effort")


def harness_text(s: dict[str, Any]) -> str:
    return HARNESS_WORDS.get(s.get("harness") or "api", s.get("harness") or "")


def kind_text(s: dict[str, Any]) -> str:
    return "agent build" if s.get("run_class") == "B" else "same-prompt run"


def time_label(s: dict[str, Any]) -> str:
    if s.get("run_class") == "B":
        return "agent time"
    # "median of N" lives in the footnote: the stat label stays one short line.
    return "compute time"


def electricity(s: dict[str, Any], energy: Optional[EnergyConfig]) -> Optional[float]:
    """nodes × supply rating × wall time × price — local runs only, only
    with a configured price. Arithmetic ceiling, never a measurement."""
    if energy is None or energy.price_per_kwh is None or s.get("locality") != "local" or not s.get("time_ms"):
        return None
    kwh = int(s.get("nodes") or 1) * energy.watts_per_node / 1000 * (s["time_ms"] / 3_600_000)
    return kwh * energy.price_per_kwh


def money_stat(s: dict[str, Any], energy: Optional[EnergyConfig]) -> Optional[dict[str, Any]]:
    power = electricity(s, energy)
    if power is not None:
        return {"value": fmt_money(power), "unit": USD, "unit_before": True,
                "label": "power, at most", "measured": False}
    if s.get("locality") == "cloud" and s.get("cost_kind") == "api_price" and s.get("cost_usd") is not None:
        return {"value": fmt_money(s["cost_usd"]), "unit": "$", "unit_before": True,
                "label": "API list price", "measured": True}
    return None


def _same(sources: list[dict[str, Any]], key) -> bool:
    return len({key(s) for s in sources}) == 1


def _name(s: dict[str, Any], sources: list[dict[str, Any]]) -> str:
    """How a run is called in bars and tables: the brief title when the
    titles differ, else the model (same brief, different models)."""
    titles_differ = len({x["title"] for x in sources}) == len(sources)
    return s["title"] if titles_differ or _same(sources, lambda x: x["model"]) else s["model"]


# ── The headline ─────────────────────────────────────────────────────────


def choose_hook(sources: list[dict[str, Any]], energy: Optional[EnergyConfig]) -> dict[str, Any]:
    """The one honest headline. Runs of the same brief, class and harness →
    the biggest meaningful ratio (time, decode speed, output tokens).
    Otherwise → totals (electricity of all runs when priced, else time)."""
    groups: dict[tuple, list[dict[str, Any]]] = {}
    for s in sources:
        if s.get("prompt_sha"):
            groups.setdefault((s["prompt_sha"], s["run_class"], s["harness"]), []).append(s)
    best: Optional[dict[str, Any]] = None
    for members in groups.values():
        if len(members) < 2:
            continue
        for metric, better_low, word in (("time_ms", True, "faster"),
                                         ("decode_tok_s", False, "faster decode"),
                                         ("tokens_out", True, "fewer output tokens")):
            vals = [(m, m.get(metric)) for m in members if m.get(metric)]
            if len(vals) < 2:
                continue
            lo = min(vals, key=lambda x: x[1])
            hi = max(vals, key=lambda x: x[1])
            ratio = hi[1] / lo[1]
            if ratio < MIN_HEADLINE_RATIO or (best and ratio <= best["ratio"]):
                continue
            winner, loser = (lo, hi) if better_low else (hi, lo)
            best = {"kind": "ratio", "metric": metric, "ratio": ratio, "word": word,
                    "winner": winner[0], "loser": loser[0], "members": members}
    if best:
        return best
    total_ms = sum(s["time_ms"] for s in sources if s.get("time_ms"))
    powers = [electricity(s, energy) for s in sources]
    priced = [p for p in powers if p is not None]
    if priced and len(priced) == len(sources):
        return {"kind": "power_total", "power": sum(priced), "time_ms": total_ms}
    return {"kind": "time_total", "time_ms": total_ms}


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _hook_part(sources, hook, energy, header, art_src, bars) -> dict[str, Any]:
    n = len(sources)
    kinds = {kind_text(s) for s in sources}
    noun = "agent builds" if kinds == {"agent build"} else "runs"
    same_brief = _same(sources, lambda s: s.get("prompt_sha"))
    bits = [f"{n} {noun}", "same model" if _same(sources, lambda s: s["model"]) else f"{len({s['model'] for s in sources})} models",
            "same brief" if same_brief else "different briefs"]
    total_tokens = sum(s["tokens_out"] or 0 for s in sources)
    if hook["kind"] == "ratio":
        w, l = hook["winner"], hook["loser"]
        hero = {"value": f"{hook['ratio']:.1f}", "unit": "×", "label": hook["word"], "measured": True}
        sub = f"{_name(w, sources)} vs {_name(l, sources)}"
        single = all(s.get("n_runs", 1) <= 1 for s in hook["members"])
        note = "Same brief, same harness" + (" · single run each" if single else " · medians")
    elif hook["kind"] == "power_total":
        hero = {"value": fmt_money(hook["power"]), "unit": USD, "unit_before": True,
                "label": f"power for all {n} {noun}, at most", "measured": False}
        sub = f"{fmt_min(hook['time_ms'])} min of work · {c.fmt_tokens(total_tokens)} output tokens"
        note = ("Different briefs: no race, just the bill · arithmetic" if not same_brief
                else "Arithmetic ceiling, not a meter reading")
    else:
        hero = {"value": fmt_min(hook["time_ms"]), "unit": "min", "label": f"of work, {n} {noun}"}
        sub = f"{c.fmt_tokens(total_tokens)} output tokens"
        note = "Different briefs: no race, the time each one took" if not same_brief else ""
    return {"kind": "hook", "duration_s": HOOK_S, "header": header,
            "art": _art(art_src, HOOK_ART_START_S), "caption": " · ".join(bits),
            "hero": hero, "hero_sub": sub, "bars": bars, "footnote": note}


def _art(s: dict[str, Any], start: float) -> dict[str, Any]:
    if s.get("video_path"):
        return {"path": s["video_path"], "kind": "video", "start_s": start}
    return {"path": s["screenshot_path"], "kind": "image", "start_s": 0.0}


def _header(sources: list[dict[str, Any]]) -> dict[str, str]:
    hw = {hardware_line(s) for s in sources}
    models = {s["model"] for s in sources}
    harness = {harness_text(s) for s in sources}
    effort = {effort_text(s) for s in sources}
    meta = [x for x in (harness.pop() if len(harness) == 1 else "",
                        effort.pop() if len(effort) == 1 else "", date_span(sources)) if x]
    return {"hardware": hw.pop() if len(hw) == 1 else f"{len(sources)} setups, one video",
            # Several ids do not fit one header line — each segment names its own.
            "model": models.pop() if len(models) == 1 else f"{len(models)} models",
            "meta": " · ".join(meta)}


def _entry_header(s: dict[str, Any]) -> dict[str, str]:
    meta = [harness_text(s), effort_text(s)]
    if s.get("quant"):
        meta.insert(0, s["quant"])
    meta.append(fmt_day(s.get("created_at")))
    return {"hardware": hardware_line(s), "model": s["model"], "meta": " · ".join(m for m in meta if m)}


def _rating_text(r: Optional[float]) -> Optional[str]:
    if r is None:
        return None
    return f"{r:g}/10"


def hook_bars(sources: list[dict[str, Any]], hook: dict[str, Any]) -> list[dict[str, Any]]:
    """The hook's bars show what the headline says: a ratio → that metric,
    over the runs it compares only (never the other briefs); totals → time."""
    if hook["kind"] != "ratio":
        return time_bars(sources, None)
    members, metric = hook["members"], hook["metric"]
    top = max(m[metric] for m in members if m.get(metric)) or 1
    fmt = {"time_ms": lambda v: f"{fmt_min(v)} min", "decode_tok_s": lambda v: f"{c.fmt_rate(v)} tok/s",
           "tokens_out": lambda v: f"{c.fmt_tokens(v)} tok"}[metric]
    return [{"label": _name(m, sources), "value": fmt(m[metric]) if m.get(metric) else "n/a",
             "fraction": round((m.get(metric) or 0) / top, 4), "highlight": m is hook["winner"]}
            for m in members[:4]]


def time_bars(sources: list[dict[str, Any]], highlight: Optional[int]) -> list[dict[str, Any]]:
    top = max((s["time_ms"] or 0) for s in sources) or 1
    return [{"label": _name(s, sources), "value": f"{fmt_min(s['time_ms'])} min" if s.get("time_ms") else "n/a",
             "fraction": round((s["time_ms"] or 0) / top, 4), "highlight": i == highlight}
            for i, s in enumerate(sources)]


# ── The whole video ──────────────────────────────────────────────────────


def build_spec(
    sources: list[dict[str, Any]],
    *,
    ratings: Optional[Mapping[str, float]] = None,
    pick_test_id: Optional[str] = None,
    pick_reason: Optional[str] = None,
    rating_is_example: bool = False,
    energy: Optional[EnergyConfig] = None,
    handle: Optional[str] = None,
    private_words: Iterable[str] = (),
) -> dict[str, Any]:
    """The renderer payload (``compilation.CompilationSpec``) for 1–4 runs."""
    if not MIN_ENTRIES <= len(sources) <= MAX_ENTRIES:
        raise ValueError(f"a compilation shows {MIN_ENTRIES} to {MAX_ENTRIES} runs")
    for s in sources:
        # Allow-list like the W1 kit: only a green or amber gate is publishable.
        if s.get("gate_status") not in ("green", "amber"):
            raise ValueError(f"{s['title']}: the gate is {s['gate_status']}, not publishable")
        if not s.get("time_ms"):
            raise ValueError(f"{s['title']}: no measured time")
        if not (s.get("video_path") or s.get("screenshot_path")):
            raise ValueError(f"{s['title']}: no recording and no screenshot")
    ratings = dict(ratings or {})
    rating_word = "Example rating" if rating_is_example else "My rating"
    if len(sources) == 1:
        parts = _solo_parts(sources[0], ratings, rating_word, pick_reason, rating_is_example, energy)
        return _finish(parts, sources, handle, private_words)
    header = _header(sources)
    hook = choose_hook(sources, energy)
    # The hook shows the winner of a ratio; otherwise the LAST run, so the
    # first segment does not open on the picture the hook just showed.
    hero_src = hook["winner"] if hook["kind"] == "ratio" else sources[-1]
    parts = [_hook_part(sources, hook, energy, header, hero_src, hook_bars(sources, hook))]
    single = all(s.get("n_runs", 1) <= 1 for s in sources)
    for i, s in enumerate(sources):
        stats, money = _entry_stats(s, energy)
        note = ["single run" if s.get("n_runs", 1) <= 1 else f"median of {s['n_runs']}"]
        if money and not money["measured"]:
            note.append("power is arithmetic, not measured")
        rating = _rating_text(ratings.get(s["test_id"]))
        parts.append({
            "kind": "entry", "duration_s": ENTRY_S[len(sources)], "header": _entry_header(s),
            "art": _art(s, ENTRY_ART_START_S), "caption": s["title"],
            "badge": f"{rating_word} {rating}" if rating else None,
            "stats": stats, "bars": time_bars(sources, i), "footnote": " · ".join(note).capitalize(),
        })
    parts.append(_ranking_part(sources, ratings, rating_word, pick_test_id, energy, header))
    parts.append(_closing_part(sources, pick_test_id, pick_reason, rating_is_example, energy, header, single))
    return _finish(parts, sources, handle, private_words)


def _finish(parts, sources, handle, private_words) -> dict[str, Any]:
    spec = {"shape": "tall", "parts": parts, "footer": FOOTER_LINE,
            "handle": footer() if handle is None else handle,
            "checksum": c.checksum([s.get("checksum") for s in sources])}
    hits = c.privacy_hits(public_strings(spec), private_words)
    if hits:
        raise ValueError(f"privacy: {', '.join(hits)} in the public words")
    return spec


def _entry_stats(s: dict[str, Any], energy: Optional[EnergyConfig]) -> tuple[list[dict[str, Any]], Optional[dict]]:
    stats = [{"value": fmt_min(s["time_ms"]) or "n/a", "unit": "min", "label": time_label(s)},
             {"value": c.fmt_tokens(s["tokens_out"]) or "n/a", "label": "output tokens"}]
    money = money_stat(s, energy)
    if money:
        stats.append(money)
    elif s.get("decode_tok_s"):
        stats.append({"value": c.fmt_rate(s["decode_tok_s"]), "label": "tok/s decode"})
    return stats, money


def _solo_parts(s, ratings, rating_word, pick_reason, example, energy) -> list[dict[str, Any]]:
    """One run: hook → the build (longer) → details. Nothing is compared
    with anything: the one bar is the run against its own time limit."""
    header = _entry_header(s)
    stats, money = _entry_stats(s, energy)
    runs = "one run" if s.get("n_runs", 1) <= 1 else f"median of {s['n_runs']} runs"
    power = electricity(s, energy)
    if power is not None:
        hero = {"value": fmt_money(power), "unit": USD, "unit_before": True,
                "label": "power for the whole build, at most", "measured": False}
        sub = f"{fmt_min(s['time_ms'])} min of {'agent work' if s['run_class'] == 'B' else 'compute'} · " \
              f"{c.fmt_tokens(s['tokens_out'])} output tokens"
        hook_note = f"{runs.capitalize()} · power is arithmetic, not measured"
    else:
        hero = {"value": fmt_min(s["time_ms"]), "unit": "min", "label": time_label(s) + ", one build"}
        sub = f"{c.fmt_tokens(s['tokens_out'])} output tokens"
        hook_note = runs.capitalize()
    rating = _rating_text(ratings.get(s["test_id"]))
    bars = []
    limit = s.get("time_limit_min")
    if limit and s.get("time_ms"):
        share = min(1.0, s["time_ms"] / (limit * 60_000))
        bars = [{"label": f"of its {limit:g}-min time limit", "value": f"{round(share * 100)}%",
                 "fraction": round(share, 4), "highlight": True}]
    facts = [{"label": "effort", "value": effort_text(s).removeprefix("effort ")},
             {"label": "runs", "value": str(s.get("n_runs") or 1)},
             {"label": "harness", "value": harness_text(s)}]
    note = [runs.capitalize()]
    if money and not money["measured"]:
        note.append("power is arithmetic, not measured")
    parts = [
        {"kind": "hook", "duration_s": HOOK_S, "header": header, "art": _art(s, HOOK_ART_START_S),
         "caption": f"{s['title']} · {kind_text(s)}", "hero": hero, "hero_sub": sub, "footnote": hook_note},
        {"kind": "entry", "duration_s": SOLO_ENTRY_S, "header": header, "art": _art(s, ENTRY_ART_START_S),
         "caption": s["title"], "badge": f"{rating_word} {rating}" if rating else None,
         "stats": stats, "bars": bars, "facts": facts, "footnote": " · ".join(note)},
    ]
    closing = _closing_part([s], None, None, example, energy, header, s.get("n_runs", 1) <= 1)
    if rating:
        closing["pick"] = f"{rating_word}: {rating}"
        closing["pick_reason"] = pick_reason or None
    parts.append(closing)
    return parts


def _ranking_part(sources, ratings, rating_word, pick_test_id, energy, header) -> dict[str, Any]:
    rated = bool(ratings)
    order = list(range(len(sources)))
    if rated:
        def _r(i: int) -> float:
            value = ratings.get(sources[i]["test_id"])
            return -1.0 if value is None else float(value)

        order.sort(key=lambda i: (-_r(i), sources[i]["time_ms"] or 0))
    has_money = any(money_stat(s, energy) for s in sources)
    cols = (["#"] if rated else []) + ["build" if all(s["run_class"] == "B" for s in sources) else "run"]
    # Five columns at most on 1080 px: a rated table trades the token
    # column (each segment shows it) for the rating.
    cols += (["rating", "time"] if rated else ["time", "tokens"])
    # "power" only when every money cell IS power; a cloud list price
    # in the same column makes it "cost".
    all_power = all(electricity(s, energy) is not None for s in sources)
    cols += (["power" if all_power else "cost"] if has_money else [])
    rows = []
    for rank, i in enumerate(order, 1):
        s = sources[i]
        cells = ([str(rank)] if rated else []) + [_name(s, sources)]
        if rated:
            cells.append(_rating_text(ratings.get(s["test_id"])) or "–")
        cells.append(f"{fmt_min(s['time_ms'])} min" if s.get("time_ms") else "n/a")
        if not rated:
            cells.append(c.fmt_tokens(s["tokens_out"]) or "n/a")
        if has_money:
            m = money_stat(s, energy)
            cells.append(f"{m['unit']}{m['value']}" if m else "–")
        rows.append({"cells": cells, "highlight": s["test_id"] == pick_test_id})
    pick = next((s for s in sources if s["test_id"] == pick_test_id), None)
    # The pick, else the top of the ranking, else a middle run (the hook
    # showed the last one, the first segment the first one).
    art_src = pick or (sources[order[0]] if rated else sources[len(sources) // 2])
    caption = f"Ranking · {rating_word.lower()} first, then time" if rated else "The numbers"
    return {"kind": "ranking", "duration_s": RANKING_S, "header": header,
            "art": _art(art_src, RANKING_ART_START_S), "caption": caption,
            "table": {"columns": cols, "rows": rows, "name_col": 1 if rated else 0},
            "footnote": _totals(sources, energy)}


def _totals(sources, energy) -> str:
    total_ms = sum(s["time_ms"] or 0 for s in sources)
    bits = [f"Total {fmt_min(total_ms)} min", f"{c.fmt_tokens(sum(s['tokens_out'] or 0 for s in sources))} tokens"]
    powers = [electricity(s, energy) for s in sources]
    if powers and all(p is not None for p in powers):
        bits.append(f"{USD}{fmt_money(sum(powers))} power, arithmetic")
    return " · ".join(bits)


def _closing_part(sources, pick_test_id, pick_reason, example, energy, header, single) -> dict[str, Any]:
    pick = next((s for s in sources if s["test_id"] == pick_test_id), None)
    details: list[str] = []
    if _same(sources, lambda s: (s["model"], s["harness"])):
        s0 = sources[0]
        details.append(f"{s0['model']} · {harness_text(s0)} · {effort_text(s0)}.")
    if all(s["run_class"] == "B" for s in sources):
        one = " One run." if len(sources) == 1 else " One run each."
        details.append("Time = active agent time of the run; tools allowed." + (one if single else ""))
    else:
        details.append("Time = compute time" + (", one run each." if single else ", median of the runs."))
    if any(s["tokens_source"] == "usage_events" for s in sources):
        details.append("Tokens = output tokens summed over every model call of the run.")
    if energy and energy.price_per_kwh and any(s.get("locality") == "local" for s in sources):
        details.append(f"Power = nodes × {energy.watts_per_node:g} W supply rating × time × "
                       f"{USD}{energy.price_per_kwh:g}/kWh (assumed). Arithmetic ceiling, not measured; "
                       "hardware not counted.")
    if all(s.get("network_blocked") for s in sources):
        details.append("Page recorded from the saved file, network off." if len(sources) == 1
                       else "Pages recorded from the saved files, network off.")
    if len(sources) > 1 and not _same(sources, lambda s: s.get("prompt_sha")):
        details.append("Different briefs: the numbers describe each build, not a race.")
    part: dict[str, Any] = {"kind": "closing", "duration_s": CLOSING_S, "header": header,
                            "caption": "How this was measured", "details": details[:6],
                            "thumbs": [s["screenshot_path"] for s in sources if s.get("screenshot_path")]}
    if pick:
        word = "Example pick" if example else "My pick"
        part["pick"] = f"{word}: {_name(pick, sources)}"
        part["pick_reason"] = pick_reason or None
    return part


def public_strings(spec: Mapping[str, Any]) -> list[str]:
    """Every word that lands in the video (scanned for R4 before render)."""
    out: list[str] = [spec.get("footer") or ""]
    for p in spec["parts"]:
        h = p["header"]
        out += [h["hardware"], h["model"], h["meta"], p.get("caption") or "", p.get("badge") or "",
                p.get("hero_sub") or "", p.get("footnote") or "", p.get("pick") or "", p.get("pick_reason") or ""]
        for st in ([p["hero"]] if p.get("hero") else []) + list(p.get("stats") or []):
            out += [st["value"], st["label"], st.get("unit") or ""]
        for b in p.get("bars") or []:
            out += [b["label"], b["value"]]
        for f in p.get("facts") or []:
            out += [f["label"], f["value"]]
        if p.get("table"):
            out += list(p["table"]["columns"])
            for r in p["table"]["rows"]:
                out += list(r["cells"])
        out += list(p.get("details") or [])
    return [x for x in out if x]


# ── DB + renderer (the async edge; phase 2 wires a route to it) ──────────


async def load_sources(session, req: CompilationIn) -> list[dict[str, Any]]:
    """The runs in the operator's order: the chosen entry (or the video run)
    of each test, plus the sum of its usage events."""
    import uuid as _uuid

    from sqlalchemy import func
    from sqlmodel import select

    from app.models.bench import BenchChallenge, BenchEntry
    from app.models.model_usage import ModelUsageEvent

    out: list[dict[str, Any]] = []
    for item in req.entries:
        challenge = await session.get(BenchChallenge, _uuid.UUID(item.test_id))
        if challenge is None:
            raise LookupError(f"test {item.test_id} not found")
        rows = (await session.exec(select(BenchEntry).where(BenchEntry.challenge_id == challenge.id))).all()
        if item.entry_id:
            rows = [e for e in rows if str(e.id) == item.entry_id]
        rows = sorted(rows, key=lambda e: (e.repeat_idx != c.VIDEO_RUN_INDEX, not e.video_path, e.position))
        if not rows:
            raise LookupError(f"test {item.test_id}: no run")
        entry = rows[0]
        usage = None
        if entry.task_id:
            usage = (await session.exec(
                select(func.sum(ModelUsageEvent.output_tokens)).where(ModelUsageEvent.task_id == entry.task_id)
            )).one()
            usage = usage[0] if isinstance(usage, tuple) else usage
        out.append(source_from_rows(
            {"id": challenge.id, "title": challenge.title, "created_at": challenge.created_at,
             "prompt_sha256": challenge.prompt_sha256},
            {"id": entry.id, "receipt": entry.receipt, "metrics": entry.metrics, "gate": entry.gate,
             "run_class": entry.run_class, "harness": entry.harness, "model_label": entry.model_label,
             "effort_requested": entry.effort_requested, "video_path": entry.video_path,
             "screenshot_path": entry.screenshot_path},
            int(usage) if usage else None,
        ))
    return out


async def build_for(session, req: CompilationIn, *, energy: Optional[EnergyConfig] = None) -> dict[str, Any]:
    from .w1_receipt import private_words

    sources = await load_sources(session, req)
    ratings = {e.test_id: e.rating for e in req.entries if e.rating is not None}
    return build_spec(sources, ratings=ratings, pick_test_id=req.pick_test_id, pick_reason=req.pick_reason,
                      rating_is_example=req.rating_is_example, energy=energy or EnergyConfig.from_env(),
                      private_words=await private_words(session))


def output_path(spec: Mapping[str, Any]) -> str:
    from . import orchestrator as orch

    # The whole payload names the file: a new rating or pick is a new file,
    # never an overwrite of the earlier cut.
    return str(orch.SHARED_DELIVERABLES / COMPILATIONS_DIR / f"compilation-{c.checksum(dict(spec))[:12]}.mp4")


async def render(session, req: CompilationIn) -> dict[str, Any]:
    """Build the payload and let the renderer encode it. Needs a renderer
    that announces ``bench_compilation_v1``."""
    from . import orchestrator as orch
    from .w1_runner import _post_renderer

    if FEATURE_COMPILATION not in await orch.renderer_features():
        raise RuntimeError("the renderer image is older than the compilation format")
    spec = await build_for(session, req)
    return await _post_renderer("/compilation", {"spec": spec, "output_path": output_path(spec)})


FEATURE_COMPILATION = "bench_compilation_v1"
