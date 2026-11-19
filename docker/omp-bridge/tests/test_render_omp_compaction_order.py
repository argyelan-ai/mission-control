#!/usr/bin/env python3
"""omp compacts with a text summary first, on every container start (fix 19.11.2026).

THE BUG
-------
omp 18.14.2's default ``compaction.methodOrder`` is remote, snapcompact,
handoff, shake, soft. ``remote`` needs server compaction, which MC's
``mc-openai`` provider (openai-completions) does not have, so on a vision
runtime ``snapcompact`` runs first: the history becomes dense bitmap frames
the model reads back. Measured on a local vision omp agent, 13.11.2026: 3/7 facts readable after
snapcompact, a wrong start time in the run report; omp's ``soft`` text summary
kept 12/12 probe answers. The order soft, shake, handoff, snapcompact was set
by hand in the profile's config.yml — and lost on every recreate, because
render-omp-config.sh rewrites the profile on every start (live 19.11.2026:
block missing after a recreate).

THE FIX
-------
render-omp-config.sh writes the order with ``omp config set`` (the only write
omp honours), default soft-first, overridable per container with
OMP_COMPACTION_METHOD_ORDER (comma list, or ``omp-default`` for omp's own
order). omp takes ANY array and silently drops unknown names when it reads
it — an order of only unknown names means no compaction at all — so the
script drops unknown names itself, loudly, and falls back to the default.

No docker: the fake omp of test_render_omp_model_roles.py keeps a JSON store.

  pytest docker/omp-bridge/tests/test_render_omp_compaction_order.py -v
"""
from __future__ import annotations

import json

from test_render_omp_model_roles import _render  # noqa: E402

DEFAULT_ORDER = ["soft", "shake", "handoff", "snapcompact"]


def _warns(out) -> list[str]:
    return [ln for ln in out.stdout.splitlines() if "WARN" in ln]


def test_every_start_writes_the_soft_first_order(tmp_path):
    """The incident: a fresh profile (container recreate) gets soft first."""
    data, out = _render(tmp_path)
    assert data.get("compaction.methodOrder") == DEFAULT_ORDER
    assert "compaction.methodOrder" in out.stdout
    assert _warns(out) == []


def test_vision_and_text_runtimes_get_the_same_order(tmp_path):
    """The cause is the method, not the model: both kinds get soft first."""
    data, _ = _render(tmp_path, vision=True)
    assert data.get("compaction.methodOrder") == DEFAULT_ORDER


def test_a_stale_order_is_overwritten(tmp_path):
    """omp's own image-first order in the store does not survive a render."""
    store = tmp_path / "omp-settings.json"
    store.write_text(json.dumps({"compaction.methodOrder": [
        "remote", "snapcompact", "handoff", "shake", "soft"]}))
    data, _ = _render(tmp_path)
    assert data.get("compaction.methodOrder") == DEFAULT_ORDER


def test_the_order_can_be_set_per_container(tmp_path):
    data, out = _render(tmp_path, extra={
        "OMP_COMPACTION_METHOD_ORDER": "handoff, soft"})
    assert data.get("compaction.methodOrder") == ["handoff", "soft"]
    assert _warns(out) == []


def test_omp_default_restores_omps_own_order(tmp_path):
    store = tmp_path / "omp-settings.json"
    store.write_text(json.dumps({"compaction.methodOrder": DEFAULT_ORDER}))
    data, out = _render(tmp_path, extra={
        "OMP_COMPACTION_METHOD_ORDER": "omp-default"})
    assert "compaction.methodOrder" not in data
    assert "omp-default" in out.stdout


def test_unknown_methods_are_dropped_loudly(tmp_path):
    """omp would store them and skip them; an order of only unknown names
    would switch compaction off without a word."""
    data, out = _render(tmp_path, extra={
        "OMP_COMPACTION_METHOD_ORDER": "soft,bogus,soft,shake"})
    assert data.get("compaction.methodOrder") == ["soft", "shake"]
    warns = _warns(out)
    assert len(warns) == 1 and "bogus" in warns[0], warns


def test_an_order_without_a_known_method_falls_back_to_the_default(tmp_path):
    data, out = _render(tmp_path, extra={
        "OMP_COMPACTION_METHOD_ORDER": "bogus"})
    assert data.get("compaction.methodOrder") == DEFAULT_ORDER
    assert len(_warns(out)) == 2, _warns(out)


def test_a_failed_write_is_loud_and_does_not_block_the_start(tmp_path):
    data, out = _render(tmp_path, extra={
        "FAKE_OMP_FAIL": "compaction.methodOrder"})
    assert "compaction.methodOrder" not in data
    warns = _warns(out)
    assert len(warns) == 1 and "compaction.methodOrder" in warns[0], warns
    # the other writes still landed
    assert data.get("modelRoles")
