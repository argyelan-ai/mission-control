"""Harness matrix guard — keeps docs/harness-matrix.md true without goodwill.

One machine-readable source, docs/harness-matrix.json, holds BOTH the dev-ring
declarations (per harness x place x task capability, guarded in
test_harness_dev_ring.py) and the per-harness capability matrix. The human
table docs/harness-matrix.md is generated from it by
tools/render-harness-matrix.py.

This guard fails when
  (a) a harness known in code has no column, or a column names no known
      harness (non-harness columns must point at an existing code anchor);
  (b) the committed doc differs from the renderer output (drift);
  (c) a cited evidence path does not exist, or its line range runs past the
      end of the file; a yes/partial cell cites nothing; a "no" cell lacks
      since/recheck; a last-verified date is malformed or in the future.
"""
from __future__ import annotations

import datetime as _dt
import importlib.util
import re
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND_ROOT.parent
sys.path.insert(0, str(BACKEND_ROOT))

from app.services import host_harness_adapter as hha  # noqa: E402
from app.services.compose_renderer import HARNESS_IMAGES  # noqa: E402
from app.services.harness_compat import (  # noqa: E402
    HARNESS_CAPABILITIES,
    HARNESS_PROTOCOLS,
    HARNESSES,
)

REGEN_CMD = "python3 tools/render-harness-matrix.py"


def _load_renderer():
    path = REPO_ROOT / "tools" / "render-harness-matrix.py"
    spec = importlib.util.spec_from_file_location("render_harness_matrix", path)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


R = _load_renderer()
DATA = R.load()
COLUMNS = {c["id"]: c for c in DATA["columns"]}

#: Every registry in code that admits a harness. A harness entering any of
#: them without a matrix column turns this guard red.
CODE_REGISTRIES = {
    "harness_compat.HARNESSES": set(HARNESSES),
    "harness_compat.HARNESS_PROTOCOLS": set(HARNESS_PROTOCOLS),
    "harness_compat.HARNESS_CAPABILITIES": set(HARNESS_CAPABILITIES),
    "host_harness_adapter.HOST_ADAPTERS": set(hha.HOST_ADAPTERS),
    "compose_renderer.HARNESS_IMAGES": set(HARNESS_IMAGES),
}
KNOWN_HARNESSES = set().union(*CODE_REGISTRIES.values())


# ── (a) columns vs. code ─────────────────────────────────────────────────────

def test_every_harness_known_in_code_has_a_column():
    harness_cols = {c["id"] for c in DATA["columns"] if c["kind"] == "harness"}
    for source, members in CODE_REGISTRIES.items():
        missing = members - harness_cols
        assert not missing, (
            f"{source} knows harness(es) {sorted(missing)} but docs/harness-matrix.json "
            "has no column for them. Add a column (kind 'harness') with a cell for "
            f"every matrix row, then run: {REGEN_CMD}"
        )


def test_every_harness_column_is_a_known_harness():
    ghosts = {c["id"] for c in DATA["columns"] if c["kind"] == "harness"} - KNOWN_HARNESSES
    assert not ghosts, (
        f"docs/harness-matrix.json has harness column(s) {sorted(ghosts)} that no code "
        f"registry knows ({', '.join(CODE_REGISTRIES)}). Remove the column or fix its id."
    )


def test_non_harness_columns_point_at_existing_code():
    for col in DATA["columns"]:
        if col["kind"] == "harness":
            continue
        anchor = col.get("anchor")
        assert anchor, f"column '{col['id']}' (kind {col['kind']}) needs an 'anchor' path in code"
        assert (REPO_ROOT / anchor).exists(), (
            f"column '{col['id']}' anchors to '{anchor}', which does not exist — "
            "the thing this column describes is gone; remove the column"
        )


def test_column_places_match_code():
    """Container place iff the harness has an image; host place iff a host adapter."""
    for cid, col in COLUMNS.items():
        if col["kind"] != "harness":
            continue
        places = set(col.get("places", []))
        assert ("container" in places) == (cid in HARNESS_IMAGES), (
            f"column '{cid}': places {sorted(places)} disagree with "
            f"compose_renderer.HARNESS_IMAGES (container image: {cid in HARNESS_IMAGES})"
        )
        assert ("host" in places) == (cid in hha.HOST_ADAPTERS), (
            f"column '{cid}': places {sorted(places)} disagree with "
            f"host_harness_adapter.HOST_ADAPTERS (host adapter: {cid in hha.HOST_ADAPTERS})"
        )


def test_every_row_has_a_cell_for_every_column():
    for group, row in R.matrix_rows(DATA):
        for col in DATA["columns"]:
            try:
                R.resolve_cell(DATA, row, col)
            except KeyError as exc:  # pragma: no cover - message is the point
                pytest.fail(f"[{group['group']}] {exc}")
        unknown = set(row.get("cells", {})) - set(COLUMNS)
        assert not unknown, f"row '{row['id']}' has cells for unknown columns {sorted(unknown)}"


def test_row_ids_are_unique():
    ids = [row["id"] for _g, row in R.matrix_rows(DATA)]
    dupes = sorted({i for i in ids if ids.count(i) > 1})
    assert not dupes, f"duplicate matrix row ids: {dupes}"


def test_ring_rows_used_by_the_matrix_exist():
    caps = set(DATA["ring"]["capabilities"])
    for _g, row in R.matrix_rows(DATA):
        if row.get("ring"):
            assert row["ring"] in caps, f"row '{row['id']}' derives from unknown ring cap '{row['ring']}'"


# ── (b) generated doc equals the renderer ────────────────────────────────────

def test_generated_doc_matches_source():
    doc = REPO_ROOT / "docs" / "harness-matrix.md"
    assert doc.exists(), f"docs/harness-matrix.md is missing — run: {REGEN_CMD}"
    expected = R.render(DATA)
    actual = doc.read_text(encoding="utf-8")
    assert actual == expected, (
        "docs/harness-matrix.md is stale or was edited by hand — it must equal the "
        f"renderer output of docs/harness-matrix.json. Run: {REGEN_CMD}"
    )


def test_generated_doc_carries_do_not_edit_header():
    first = (REPO_ROOT / "docs" / "harness-matrix.md").read_text(encoding="utf-8").splitlines()[0]
    assert "DO NOT EDIT BY HAND" in first


# ── (c) evidence and cell shape ──────────────────────────────────────────────

def _all_cells():
    """Every explicit matrix cell and every ring declaration, with a label."""
    for group, row in R.matrix_rows(DATA):
        for cid, cell in row.get("cells", {}).items():
            yield f"matrix {row['id']}/{cid}", cell
    for r in DATA["ring"]["rows"]:
        yield f"ring {r['harness']}/{r['place']}/{r['cap']}", r


_LIVE = re.compile(r"^live:(\d{4}-\d{2}-\d{2}) \S")


def _check_evidence_item(label: str, item: str) -> None:
    if item.startswith("live:"):
        m = _LIVE.match(item)
        assert m, f"{label}: live evidence must read 'live:YYYY-MM-DD <what was measured>', got {item!r}"
        day = _dt.date.fromisoformat(m.group(1))
        assert day <= _dt.date.today(), f"{label}: live evidence dated in the future ({item!r})"
        return
    path, start, end = R.parse_evidence(item)
    target = REPO_ROOT / path
    assert target.exists(), (
        f"{label}: cited evidence '{path}' does not exist — the code moved. Re-find "
        f"the fact, update docs/harness-matrix.json, then run: {REGEN_CMD}"
    )
    if start is None:
        return
    assert target.is_file(), f"{label}: '{path}' is a directory but cites lines"
    n_lines = len(target.read_text(encoding="utf-8", errors="replace").splitlines())
    assert 1 <= start <= end <= n_lines, (
        f"{label}: cited lines {start}-{end} of '{path}' are outside the file "
        f"(it has {n_lines} lines) — the code moved. Re-find the fact, update "
        f"docs/harness-matrix.json, then run: {REGEN_CMD}"
    )


@pytest.mark.parametrize("label,cell", list(_all_cells()), ids=lambda v: v if isinstance(v, str) else "")
def test_cell_shape_and_evidence(label: str, cell: dict):
    status = cell["status"]
    assert status in R.STATUSES, f"{label}: status {status!r} not in {R.STATUSES}"
    evidence = cell.get("evidence", [])
    for item in evidence:
        _check_evidence_item(label, item)
    is_ring = label.startswith("ring ")
    if status in ("yes", "partial") and not is_ring:
        assert evidence, f"{label}: a '{status}' cell must cite evidence (path:line or live:DATE …)"
    if status == "partial":
        assert cell.get("note"), f"{label}: a 'partial' cell must name its limit in 'note'"
    if status == "no":
        assert cell.get("since") and cell.get("recheck"), (
            f"{label}: a 'no' cell needs since=ISO date and recheck=<how we would notice it changed>"
        )
    if status == "open" and not is_ring:
        assert cell.get("note"), f"{label}: an 'open' cell says what is unknown in 'note'"


def test_ring_rows_rendered_in_the_matrix_cite_evidence():
    """Ring declarations surfaced in the human table must carry checkable evidence."""
    used = {row["ring"] for _g, row in R.matrix_rows(DATA) if row.get("ring")}
    bare = [
        f"{r['harness']}/{r['place']}/{r['cap']}"
        for r in DATA["ring"]["rows"]
        if r["cap"] in used and r["status"] == "yes" and not r.get("evidence")
    ]
    assert not bare, f"ring rows shown in the matrix without evidence: {bare}"


def test_last_verified_dates():
    today = _dt.date.today()
    for col in DATA["columns"]:
        day = _dt.date.fromisoformat(col["last_verified"])
        assert day <= today, f"column '{col['id']}': last_verified {day} is in the future"
        assert col["maturity"]["level"] in DATA["maturity_levels"], (
            f"column '{col['id']}': maturity {col['maturity']['level']!r} not in maturity_levels"
        )
        assert col["maturity"].get("reason"), f"column '{col['id']}': maturity needs a one-line reason"
