"""Bench compilation (phase 1, 30.10.2026): 2–4 finished runs → one 4:5 video
payload. Holds the honesty rules: a ratio only within the same brief and
harness, electricity only as labelled arithmetic with a configured price,
tokens of an agent build from the usage sum, rating/pick only when given,
privacy (R4) on every public word, and the payload is exactly what the
renderer's model accepts."""
from __future__ import annotations

import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

pytest.importorskip("app.verticals.bench_studio")

from app.verticals.bench_studio import w1_compilation as wc  # noqa: E402

RENDERER_DIR = Path(__file__).resolve().parents[2] / "docker" / "mc-playwright"
DS = "DeepSeek-V4.1-Flash-1017"  # model-catalog: allow (test data)
#: USD per kWh (every money figure in the video is USD).
ENERGY = wc.EnergyConfig(watts_per_node=240.0, price_per_kwh=0.35)


def _src(title, *, minutes, tokens=200_000, sha=None, harness="omp", run_class="B", model=DS,
         locality="local", nodes=2, n_runs=1, decode=None, cost_kind="local", cost_usd=None, gate="amber",
         video=True):
    return wc.source_from_rows(
        {"id": str(uuid.uuid4()), "title": title, "created_at": datetime(2026, 10, 27, 14, tzinfo=timezone.utc),
         "prompt_sha256": sha or uuid.uuid4().hex},
        {"id": str(uuid.uuid4()), "run_class": run_class, "harness": harness,
         "video_path": f"/shared-deliverables/bench-x/{title}/recording.mp4" if video else None,
         "screenshot_path": f"/shared-deliverables/bench-x/{title}/screenshot.png",
         "gate": {"status": gate, "checksum": uuid.uuid4().hex * 2},
         "receipt": {"run_class": run_class, "served_model_id": model, "locality": locality,
                     "device": "DGX Spark ×2" if locality == "local" else None, "nodes": nodes,
                     "provider": None if locality == "local" else "anthropic", "harness": harness,
                     "effort_requested": "default", "effort_observed": {"level": "high"}, "n_runs": n_runs,
                     "time_median": minutes * 60_000, "tokens_out_median": tokens,
                     "decode_tok_s_median": decode, "cost_kind": cost_kind, "cost_usd": cost_usd,
                     "network_blocked": True}},
    )


def three_builds():
    return [_src("Hourglass of Light Sand", minutes=79.488), _src("Lighthouse in a Storm", minutes=93.186),
            _src("Ribbon Highway Autopilot", minutes=59.666)]


def _validate(spec):
    sys.path.insert(0, str(RENDERER_DIR))
    try:
        import compilation
    finally:
        sys.path.remove(str(RENDERER_DIR))
    return compilation.CompilationSpec.model_validate(spec)


# ── the payload is the renderer's contract ───────────────────────────────


def test_payload_validates_against_the_renderer_model():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="@h")
    model = _validate(spec)
    assert [p.kind for p in model.parts] == ["hook", "entry", "entry", "entry", "ranking", "closing"]


def test_three_runs_last_between_29_and_40_seconds():
    for n in (2, 3, 4):
        sources = (three_builds() + [_src("Fourth", minutes=30)])[:n]
        total = sum(p["duration_s"] for p in wc.build_spec(sources, energy=ENERGY, handle="")["parts"])
        assert 29 <= total <= 40, (n, total)


@pytest.mark.parametrize("n", [0, 5])
def test_one_to_four_runs_only(n):
    sources = [_src(f"T{i}", minutes=10 + i) for i in range(n)]
    with pytest.raises(ValueError):
        wc.build_spec(sources, handle="")


def test_red_gate_is_refused():
    sources = three_builds()
    sources[1]["gate_status"] = "red"
    with pytest.raises(ValueError, match="gate"):
        wc.build_spec(sources, handle="")


def test_screenshot_is_the_art_when_there_is_no_recording():
    sources = three_builds()
    sources[0] = _src("Hourglass of Light Sand", minutes=79.5, video=False)
    spec = wc.build_spec(sources, energy=ENERGY, handle="")
    art = spec["parts"][1]["art"]
    assert art["kind"] == "image" and art["path"].endswith("screenshot.png")


# ── the headline ─────────────────────────────────────────────────────────


def test_different_briefs_never_get_a_ratio_headline():
    hook = wc.choose_hook(three_builds(), ENERGY)
    assert hook["kind"] == "power_total"
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    part = spec["parts"][0]
    assert "×" not in part["hero"].get("unit", "")
    assert "different briefs" in part["caption"]
    assert part["hero"]["measured"] is False          # arithmetic → never the accent colour


def test_same_brief_same_harness_gets_the_biggest_ratio():
    sha = "a" * 64
    sources = [_src("Pagoda Garden", minutes=10, sha=sha, model="Model-A", run_class="A", harness="api"),
               _src("Pagoda Garden", minutes=24, sha=sha, model="Model-B", run_class="A", harness="api")]
    hook = wc.choose_hook(sources, ENERGY)
    assert hook["kind"] == "ratio" and hook["metric"] == "time_ms"
    assert hook["winner"]["model"] == "Model-A"
    part = wc.build_spec(sources, energy=ENERGY, handle="")["parts"][0]
    assert part["hero"]["value"] == "2.4" and part["hero"]["unit"] == "×"
    assert part["hero_sub"] == "Model-A vs Model-B"


def test_no_ratio_across_harnesses_even_for_the_same_brief():
    sha = "b" * 64
    sources = [_src("Pagoda Garden", minutes=10, sha=sha, harness="omp"),
               _src("Pagoda Garden", minutes=40, sha=sha, harness="claude-code")]
    assert wc.choose_hook(sources, ENERGY)["kind"] != "ratio"


def test_small_differences_are_not_a_headline():
    sha = "c" * 64
    sources = [_src("P", minutes=10, sha=sha), _src("P", minutes=11, sha=sha)]
    assert wc.choose_hook(sources, ENERGY)["kind"] != "ratio"


# ── electricity ──────────────────────────────────────────────────────────


def test_electricity_is_the_plain_arithmetic():
    s = _src("H", minutes=79.488)
    # 2 nodes × 240 W × 79.488 min × $0.35 / kWh
    expected = 2 * 0.240 * (79.488 / 60) * 0.35
    assert wc.electricity(s, ENERGY) == pytest.approx(expected)
    assert wc.fmt_money(wc.electricity(s, ENERGY)) == "0.22"


def test_no_price_configured_means_no_electricity_anywhere():
    energy = wc.EnergyConfig.from_env({})
    assert energy.price_per_kwh is None and energy.watts_per_node == 240.0
    spec = wc.build_spec(three_builds(), energy=energy, handle="")
    words = " ".join(wc.public_strings(spec))
    assert "power" not in words.lower() and "$" not in words
    assert spec["parts"][0]["hero"]["unit"] == "min"


def test_energy_config_reads_the_env():
    e = wc.EnergyConfig.from_env({"MC_BENCH_POWER_W_PER_NODE": "200", "MC_BENCH_ENERGY_PRICE": "0.31",
                                  "MC_BENCH_ENERGY_CURRENCY": "EUR"})
    # The currency setting is gone: every money figure is USD.
    assert (e.watts_per_node, e.price_per_kwh) == (200.0, 0.31)
    assert not hasattr(e, "currency")
    assert wc.EnergyConfig.from_env({"MC_BENCH_ENERGY_PRICE": "nonsense"}).price_per_kwh is None


def test_electricity_is_labelled_arithmetic_in_segment_and_details():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    seg = spec["parts"][1]
    power = [st for st in seg["stats"] if st.get("unit") == "$" and "power" in st["label"]]
    assert power and power[0]["measured"] is False and "at most" in power[0]["label"]
    assert "arithmetic" in seg["footnote"].lower()
    details = " ".join(spec["parts"][-1]["details"])
    assert "240 W" in details and "$0.35/kWh (assumed)" in details and "not measured" in details


def test_cloud_run_shows_list_price_local_run_never_a_dollar():
    sha = "d" * 64
    sources = [_src("P", minutes=10, sha=sha, locality="cloud", cost_kind="api_price", cost_usd=1.234,
                    harness="api", run_class="A"),
               _src("P", minutes=30, sha=sha, harness="api", run_class="A")]
    spec = wc.build_spec(sources, energy=wc.EnergyConfig(240, None), handle="")
    cloud, local = spec["parts"][1], spec["parts"][2]
    assert any(st.get("unit") == "$" and st["value"] == "1.23" for st in cloud["stats"])
    assert not any(st.get("unit") == "$" for st in local["stats"])
    assert spec["parts"][1]["header"]["hardware"] == "Cloud · anthropic"


# ── tokens, hardware, words ──────────────────────────────────────────────


def test_agent_build_tokens_come_from_the_usage_sum():
    s = wc.source_from_rows({"id": "1", "title": "T"},
                            {"receipt": {"run_class": "B", "tokens_out_median": 236785}}, usage_tokens_out=263594)
    assert (s["tokens_out"], s["tokens_source"]) == (263594, "usage_events")
    a = wc.source_from_rows({"id": "1", "title": "T"},
                            {"receipt": {"run_class": "A", "tokens_out_median": 6600}}, usage_tokens_out=999999)
    assert (a["tokens_out"], a["tokens_source"]) == (6600, "receipt")


def test_hardware_line_names_nodes_and_memory():
    assert wc.hardware_line(_src("H", minutes=1)) == "2× DGX Spark · 256 GB"
    assert wc.hardware_line(_src("H", minutes=1, nodes=1)) == "DGX Spark · 128 GB"


def test_header_band_carries_model_harness_effort_date():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    h = spec["parts"][1]["header"]
    assert h["model"] == DS
    assert h["meta"] == "omp agent · effort high · Oct 27, 2026"


def test_entry_bars_highlight_their_own_run_and_scale_to_the_longest():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    for i in range(3):
        bars = spec["parts"][1 + i]["bars"]
        assert [b["highlight"] for b in bars] == [j == i for j in range(3)]
        assert max(b["fraction"] for b in bars) == 1.0
    assert spec["parts"][2]["bars"][1]["value"] == "93.2 min"


# ── rating and pick are only shown when given ────────────────────────────


def test_without_rating_or_pick_nothing_is_invented():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    assert all(not p.get("badge") for p in spec["parts"])
    ranking, closing = spec["parts"][-2], spec["parts"][-1]
    assert ranking["caption"] == "The numbers" and "rating" not in ranking["table"]["columns"]
    assert "pick" not in closing


def test_example_rating_and_pick_are_marked_as_example():
    sources = three_builds()
    ids = [s["test_id"] for s in sources]
    spec = wc.build_spec(sources, ratings={ids[0]: 8, ids[1]: 7.5, ids[2]: 9}, pick_test_id=ids[2],
                         pick_reason="Placeholder reason.", rating_is_example=True, energy=ENERGY, handle="")
    assert spec["parts"][1]["badge"] == "Example rating 8/10"
    ranking = spec["parts"][-2]
    assert ranking["table"]["columns"][:3] == ["#", "build", "rating"]
    assert [r["cells"][1] for r in ranking["table"]["rows"]][0] == "Ribbon Highway Autopilot"
    assert ranking["table"]["rows"][0]["highlight"] is True
    assert spec["parts"][-1]["pick"] == "Example pick: Ribbon Highway Autopilot"
    _validate(spec)


def test_real_rating_says_my_rating():
    sources = three_builds()
    spec = wc.build_spec(sources, ratings={sources[0]["test_id"]: 8.5}, energy=ENERGY, handle="")
    assert spec["parts"][1]["badge"] == "My rating 8.5/10"


def test_input_model_rules():
    with pytest.raises(Exception):
        wc.CompilationIn(entries=[{"test_id": "a"}, {"test_id": "a"}])
    with pytest.raises(Exception):
        wc.CompilationIn(entries=[{"test_id": "a"}, {"test_id": "b"}], pick_test_id="c")
    with pytest.raises(Exception):
        wc.CompilationIn(entries=[{"test_id": "a"}, {"test_id": "b"}], pick_reason="why")
    with pytest.raises(Exception):
        wc.CompilationIn(entries=[{"test_id": "a", "rating": 11}, {"test_id": "b"}])


# ── privacy (R4) ─────────────────────────────────────────────────────────


def test_a_private_word_stops_the_build():
    sources = three_builds()
    sources[0]["title"] = "Alphabot's hourglass"
    with pytest.raises(ValueError, match="privacy"):
        wc.build_spec(sources, energy=ENERGY, handle="", private_words=["Alphabot"])


def test_no_paths_or_ids_in_the_public_words():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    from app.verticals.bench_studio import w1_common as c

    assert c.privacy_hits(wc.public_strings(spec)) == []


# ── the DB loader ────────────────────────────────────────────────────────


async def test_load_sources_sums_usage_and_takes_the_video_run(session):
    from app.models.bench import BenchChallenge, BenchEntry
    from app.models.model_usage import ModelUsageEvent

    ch = BenchChallenge(title="Hourglass of Light Sand", prompt_text="x", protocol_version="v1",
                        prompt_sha256="e" * 64)
    session.add(ch)
    await session.flush()
    task_id = uuid.uuid4()
    other = BenchEntry(challenge_id=ch.id, model_label=DS, source_kind="agent", repeat_idx=2,
                       receipt={"run_class": "B", "time_median": 1}, video_path=None)
    video = BenchEntry(challenge_id=ch.id, model_label=DS, source_kind="agent", repeat_idx=1, task_id=task_id,
                       run_class="B", harness="omp",
                       receipt={"run_class": "B", "served_model_id": DS, "locality": "local", "nodes": 2,
                                "device": "DGX Spark ×2", "time_median": 4_769_324, "tokens_out_median": 236_785},
                       gate={"status": "amber", "checksum": "f" * 64},
                       video_path="/shared-deliverables/bench-1/a/recording.mp4")
    session.add_all([other, video])
    for n in (200_000, 63_594):
        session.add(ModelUsageEvent(task_id=task_id, harness="head-omp", model=DS, session_id="s",
                                    message_uuid=str(uuid.uuid4()), output_tokens=n, source_file="t.jsonl",
                                    ts=datetime.now(timezone.utc)))
    await session.commit()
    [s] = await wc.load_sources(session, wc.CompilationIn.model_construct(
        entries=[wc.CompilationEntryIn(test_id=str(ch.id))], pick_test_id=None, pick_reason=None,
        rating_is_example=False))
    assert s["entry_id"] == str(video.id)
    assert (s["tokens_out"], s["tokens_source"]) == (263_594, "usage_events")
    assert s["time_ms"] == 4_769_324 and s["title"] == "Hourglass of Light Sand"


def test_closing_shows_every_screenshot_and_the_ranking_totals():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    closing, ranking = spec["parts"][-1], spec["parts"][-2]
    assert [t.split("/")[-2] for t in closing["thumbs"]] == [
        "Hourglass of Light Sand", "Lighthouse in a Storm", "Ribbon Highway Autopilot"]
    assert ranking["footnote"].startswith("Total 232 min · ")
    assert "$0.65 power, arithmetic" in ranking["footnote"]


def test_hook_and_ranking_do_not_repeat_the_first_segment_picture():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    first_seg = spec["parts"][1]["art"]["path"]
    assert spec["parts"][0]["art"]["path"] != first_seg
    assert spec["parts"][-2]["art"]["path"] != first_seg


def test_rated_table_keeps_five_columns_at_most():
    sources = three_builds()
    ratings = {s["test_id"]: 7 for s in sources}
    table = wc.build_spec(sources, ratings=ratings, energy=ENERGY, handle="")["parts"][-2]["table"]
    assert table["columns"] == ["#", "build", "rating", "time", "power"] and table["name_col"] == 1


@pytest.mark.parametrize("status", [None, "red", "pending", "unknown"])
def test_only_green_or_amber_gates_are_publishable(status):
    sources = three_builds()
    sources[0]["gate_status"] = status
    with pytest.raises(ValueError, match="gate"):
        wc.build_spec(sources, handle="")


def test_a_run_without_measured_time_is_refused():
    sources = three_builds()
    sources[2]["time_ms"] = None
    with pytest.raises(ValueError, match="time"):
        wc.build_spec(sources, handle="")


def test_ratio_hook_bars_show_only_the_compared_runs_and_metric():
    sha = "f" * 64
    sources = [_src("Pagoda", minutes=30, sha=sha, model="M-A", run_class="A", harness="api", decode=30),
               _src("Pagoda", minutes=31, sha=sha, model="M-B", run_class="A", harness="api", decode=90),
               _src("Lighthouse", minutes=5, model="M-C", run_class="A", harness="api")]
    spec = wc.build_spec(sources, energy=ENERGY, handle="")
    hook = spec["parts"][0]
    assert hook["hero"]["value"] == "3.0" and hook["hero"]["label"] == "faster decode"
    assert [b["label"] for b in hook["bars"]] == ["M-A", "M-B"]          # not the other brief
    assert [b["value"] for b in hook["bars"]] == ["30 tok/s", "90 tok/s"]
    assert [b["highlight"] for b in hook["bars"]] == [False, True]


def test_mixed_cloud_and_power_column_says_cost():
    sha = "9" * 64
    sources = [_src("P", minutes=10, sha=sha, locality="cloud", cost_kind="api_price", cost_usd=1.5,
                    harness="api", run_class="A"),
               _src("P", minutes=30, sha=sha, harness="api", run_class="A")]
    table = wc.build_spec(sources, energy=ENERGY, handle="")["parts"][-2]["table"]
    assert table["columns"][-1] == "cost"
    assert [r["cells"][-1] for r in table["rows"]] == ["$1.50", "$0.08"]  # 2 × 240 W × 30 min × $0.35


def test_unknown_locality_never_shows_a_dollar_or_cloud():
    s = _src("P", minutes=10, locality=None, cost_kind="api_price", cost_usd=2.0)
    s["locality"] = None
    assert wc.money_stat(s, ENERGY) is None
    assert wc.hardware_line(s) == "Setup not recorded"


def test_rating_zero_ranks_below_rated_runs_but_above_unrated():
    sources = three_builds()
    ids = [s["test_id"] for s in sources]
    table = wc.build_spec(sources, ratings={ids[0]: 0, ids[1]: 5}, energy=ENERGY, handle="")["parts"][-2]["table"]
    assert [r["cells"][1] for r in table["rows"]] == [
        "Lighthouse in a Storm", "Hourglass of Light Sand", "Ribbon Highway Autopilot"]
    assert [r["cells"][2] for r in table["rows"]] == ["5/10", "0/10", "–"]


def test_different_models_never_crowd_the_shared_header():
    sha = "8" * 64
    sources = [_src("P", minutes=10, sha=sha, model="Model-Alpha-Long-Name-1", harness="api", run_class="A"),
               _src("P", minutes=30, sha=sha, model="Model-Beta-Long-Name-2", harness="api", run_class="A")]
    spec = wc.build_spec(sources, energy=ENERGY, handle="")
    assert spec["parts"][0]["header"]["model"] == "2 models"
    assert spec["parts"][1]["header"]["model"] == "Model-Alpha-Long-Name-1"


def test_a_new_rating_is_a_new_file(monkeypatch, tmp_path):
    from app.verticals.bench_studio import orchestrator as orch

    monkeypatch.setattr(orch, "SHARED_DELIVERABLES", tmp_path)
    sources = three_builds()
    a = wc.output_path(wc.build_spec(sources, energy=ENERGY, handle=""))
    b = wc.output_path(wc.build_spec(sources, ratings={sources[0]["test_id"]: 9}, energy=ENERGY, handle=""))
    assert a != b and a.endswith(".mp4") and "/bench-compilations/" in a


def test_entry_caption_is_the_brief_title_whole():
    spec = wc.build_spec(three_builds(), energy=ENERGY, handle="")
    assert [p["caption"] for p in spec["parts"][1:4]] == [
        "Hourglass of Light Sand", "Lighthouse in a Storm", "Ribbon Highway Autopilot"]



# ── USD only ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("n", [1, 3])
def test_every_money_figure_is_usd(n):
    spec = wc.build_spec(three_builds()[:n], energy=ENERGY, handle="")
    words = " ".join(wc.public_strings(spec))
    for other in ("CHF", "EUR", "€", "£"):
        assert other not in words, other
    units = [st.get("unit") for p in spec["parts"] for st in ([p["hero"]] if p.get("hero") else []) + p.get("stats", [])]
    assert "$" in units


# ── solo: one run ────────────────────────────────────────────────────────


def solo(**kw):
    s = _src("Hourglass of Light Sand", minutes=79.488, tokens=263_594)
    s["time_limit_min"] = 120
    return wc.build_spec([s], energy=ENERGY, handle="@h", **kw), s


def test_solo_is_hook_build_details_without_ranking_and_validates():
    spec, _ = solo()
    assert [p["kind"] for p in spec["parts"]] == ["hook", "entry", "closing"]
    total = sum(p["duration_s"] for p in spec["parts"])
    assert 20 <= total <= 25, total
    assert spec["parts"][1]["duration_s"] >= 12
    _validate(spec)


def test_solo_never_compares_with_nothing():
    spec, _ = solo()
    hook, build, closing = spec["parts"]
    assert not hook.get("bars")
    # the one bar is the run against its own time limit, not against other runs
    assert build["bars"] == [{"label": "of its 120-min time limit", "value": "66%", "fraction": 0.6624,
                              "highlight": True}]
    words = " ".join(wc.public_strings(spec)).lower()
    for word in ("ranking", " vs ", "faster", "different briefs", "race"):
        assert word not in words, word


def test_solo_hook_hero_is_the_power_cost_in_usd_marked_as_arithmetic():
    spec, _ = solo()
    hero = spec["parts"][0]["hero"]
    assert (hero["value"], hero["unit"], hero["measured"]) == ("0.22", "$", False)
    assert "at most" in hero["label"]
    assert spec["parts"][0]["hero_sub"] == "79.5 min of agent work · 264k output tokens"
    assert "arithmetic" in spec["parts"][0]["footnote"]


def test_solo_without_price_leads_with_the_time():
    s = _src("H", minutes=79.488)
    spec = wc.build_spec([s], energy=wc.EnergyConfig(240, None), handle="")
    assert spec["parts"][0]["hero"]["unit"] == "min" and "$" not in " ".join(wc.public_strings(spec))


def test_solo_build_shows_time_tokens_power_effort_runs():
    spec, _ = solo()
    build = spec["parts"][1]
    assert [st["label"] for st in build["stats"]] == ["agent time", "output tokens", "power, at most"]
    assert {f["label"]: f["value"] for f in build["facts"]} == {"effort": "high", "runs": "1",
                                                               "harness": "omp agent"}
    assert build["footnote"] == "One run · power is arithmetic, not measured"


def test_solo_rating_only_when_given_and_marked_example():
    spec, s = solo()
    assert not spec["parts"][1].get("badge") and "pick" not in spec["parts"][-1]
    spec2, s2 = solo(ratings=None)
    spec3 = wc.build_spec([s], energy=ENERGY, handle="", ratings={s["test_id"]: 8.5}, rating_is_example=True,
                          pick_reason="Placeholder.")
    assert spec3["parts"][1]["badge"] == "Example rating 8.5/10"
    assert spec3["parts"][-1]["pick"] == "Example rating: 8.5/10"
    assert spec3["parts"][-1]["pick_reason"] == "Placeholder."


def test_solo_details_and_one_thumbnail():
    spec, _ = solo()
    closing = spec["parts"][-1]
    assert len(closing["thumbs"]) == 1
    assert not any("Different briefs" in d for d in closing["details"])
    assert any("$0.35/kWh (assumed)" in d for d in closing["details"])
