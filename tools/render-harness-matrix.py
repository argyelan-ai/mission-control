#!/usr/bin/env python3
"""Render docs/harness-matrix.md from docs/harness-matrix.json.

The JSON file is the ONE machine-readable source of what every harness can do
in Mission Control: the per-(harness, place) dev-ring declarations
(docs/harness-dev-ring.md) and the per-harness capability matrix. This script
only renders it; it never decides anything.

    python3 tools/render-harness-matrix.py           # rewrite the doc
    python3 tools/render-harness-matrix.py --check   # exit 1 when the doc is stale

The guard is backend/tests/test_harness_matrix.py (column set vs. code,
generated doc vs. this renderer, cited evidence exists). Standard library only;
runs on Python 3.9+.
"""
from __future__ import annotations

# rule: R-harness-matrix-current - the doc is rendered, never hand-edited; the guard compares

import argparse
import json
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCE = REPO_ROOT / "docs" / "harness-matrix.json"
OUTPUT = REPO_ROOT / "docs" / "harness-matrix.md"
REGEN_CMD = "python3 tools/render-harness-matrix.py"

STATUSES = ("yes", "partial", "no", "open")
PLACE_SHORT = {"container": "C", "host": "H"}

LEGEND = {
    "yes": "works today; the cell cites code (`path:line`) or a dated live measurement",
    "partial": "works with a named limit (one place only, behind a switch, …); evidence cited",
    "no": "absent today — by design, not applicable, or a known gap; carries `since` (decided or first recorded) + `recheck` (how we would notice it changed)",
    "open": "unproven — a counted gap, never a guess",
}


def load(path: Path = SOURCE) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ── evidence ──────────────────────────────────────────────────────────────────

def parse_evidence(item: str) -> Tuple[str, Optional[int], Optional[int]]:
    """'path:12' / 'path:12-30' / 'path' → (path, start, end). 'live:…' → ('live:…', None, None)."""
    if item.startswith("live:"):
        return item, None, None
    path, sep, rng = item.rpartition(":")
    if sep and rng and rng.replace("-", "").isdigit():
        if "-" in rng:
            a, b = rng.split("-", 1)
            return path, int(a), int(b)
        return path, int(rng), int(rng)
    return item, None, None


def _evidence_link(item: str) -> str:
    path, start, end = parse_evidence(item)
    if path.startswith("live:"):
        return "live measurement — " + _esc(path[len("live:"):].strip())
    anchor = ""
    if start is not None:
        anchor = f"#L{start}" if start == end else f"#L{start}-L{end}"
    return f"[`{_esc(item)}`](../{path}{anchor})"


# ── cells ─────────────────────────────────────────────────────────────────────

def _esc(text: str) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ")


def ring_rows(data: dict) -> List[dict]:
    return data["ring"]["rows"]


def ring_lookup(data: dict) -> Dict[Tuple[str, str, str], dict]:
    return {(r["harness"], r["place"], r["cap"]): r for r in ring_rows(data)}


def resolve_cell(data: dict, row: dict, column: dict) -> dict:
    """The effective cell of a matrix row for one column.

    Explicit cells win. A row with ``ring`` derives the cell from the dev-ring
    declarations of every place the column runs in.
    """
    cid = column["id"]
    if cid in row.get("cells", {}):
        return dict(row["cells"][cid])
    cap = row.get("ring")
    if not cap:
        raise KeyError(f"matrix row '{row['id']}' has no cell for column '{cid}'")
    lookup = ring_lookup(data)
    parts, statuses, evidence, places = [], [], [], []
    for place in column.get("places", []):
        decl = lookup.get((cid, place, cap))
        if decl is None:
            raise KeyError(f"no dev-ring declaration for {cid}/{place}/{cap}")
        statuses.append(decl["status"])
        places.append(place)
        parts.append(f"{PLACE_SHORT[place]}: {decl['status']}")
        for ev in decl.get("evidence", []):
            if ev not in evidence:
                evidence.append(ev)
    if not statuses:
        raise KeyError(f"column '{cid}' has no places for ring row '{row['id']}'")
    if all(s == "yes" for s in statuses):
        status = "yes"
    elif all(s == "no" for s in statuses):
        status = "no"
    elif "open" in statuses:
        status = "open"
    else:
        status = "partial"
    return {"status": status, "note": " · ".join(parts), "evidence": evidence, "derived": True}


class _EvidenceIndex:
    def __init__(self) -> None:
        self.items: List[str] = []

    def ref(self, item: str) -> int:
        if item not in self.items:
            self.items.append(item)
        return self.items.index(item) + 1


def _cell_text(cell: dict, idx: _EvidenceIndex) -> str:
    text = f"**{cell['status']}**"
    if cell.get("value"):
        text += f" {cell['value']}"
    if cell.get("note"):
        text += f" — {cell['note']}"
    if cell["status"] == "no" and cell.get("since"):
        text += f" (since {cell['since']})"
    refs = [idx.ref(e) for e in cell.get("evidence", [])]
    if refs:
        text += " " + "".join(f"[{n}]" for n in refs)
    return _esc(text)


# ── counting ──────────────────────────────────────────────────────────────────

def matrix_rows(data: dict):
    for group in data["matrix"]:
        for row in group["rows"]:
            yield group, row


def open_counts(data: dict) -> Dict[str, int]:
    counts = {c["id"]: 0 for c in data["columns"]}
    for _group, row in matrix_rows(data):
        for col in data["columns"]:
            if resolve_cell(data, row, col)["status"] == "open":
                counts[col["id"]] += 1
    return counts


def ring_summary(data: dict, harness: str, place: str) -> str:
    rows = [r for r in ring_rows(data) if r["harness"] == harness and r["place"] == place]
    if not rows:
        return ""
    c = {s: sum(1 for r in rows if r["status"] == s) for s in ("yes", "no", "open")}
    return f"{PLACE_SHORT[place]}: {c['yes']} yes / {c['no']} no / {c['open']} open"


# ── render ────────────────────────────────────────────────────────────────────

def render(data: dict) -> str:
    cols = data["columns"]
    idx = _EvidenceIndex()
    out: List[str] = []
    w = out.append
    w("<!-- GENERATED FILE — DO NOT EDIT BY HAND. -->")
    w(f"<!-- Source: docs/harness-matrix.json · regenerate: {REGEN_CMD} -->")
    w("<!-- Guard: backend/tests/test_harness_matrix.py fails when this file and the source drift. -->")
    w("")
    w("# Harness matrix")
    w("")
    for line in data["intro"]:
        w(line)
    w("")
    w("**Status words**")
    w("")
    for s in STATUSES:
        w(f"- `{s}` — {LEGEND[s]}")
    w("")
    w("Places: `C` = container (cli-bridge image), `H` = host (launchd adapter). "
      "Numbers in brackets point to the evidence list at the end.")
    w("")

    header = "| Capability | " + " | ".join(c["label"] for c in cols) + " |"
    sep = "|---|" + "---|" * len(cols)

    # Column facts first: kind, places, maturity, last verified.
    w("## Columns")
    w("")
    w(header)
    w(sep)
    w("| Code name | " + " | ".join(f"`{c['id']}`" for c in cols) + " |")
    w("| Kind | " + " | ".join(_esc(c["kind"]) for c in cols) + " |")
    w("| Places | " + " | ".join(
        _esc(", ".join(c.get("places", [])) or "—") for c in cols) + " |")
    w("| **Maturity** | " + " | ".join(
        _esc(f"**{c['maturity']['level']}** — {c['maturity']['reason']}") for c in cols) + " |")
    w("| Last verified | " + " | ".join(_esc(c["last_verified"]) for c in cols) + " |")
    w("| Dev-ring score | " + " | ".join(
        _esc("; ".join(s for s in (ring_summary(data, c["id"], p) for p in c.get("places", [])) if s)
             or "not in the ring") for c in cols) + " |")
    counts = open_counts(data)
    w("| Open cells (this table) | " + " | ".join(str(counts[c["id"]]) for c in cols) + " |")
    w("")

    for group in data["matrix"]:
        w(f"## {group['group']}")
        w("")
        w(header)
        w(sep)
        for row in group["rows"]:
            cells = [_cell_text(resolve_cell(data, row, c), idx) for c in cols]
            label = _esc(row["label"])
            if row.get("ring"):
                label += f" (dev ring `{row['ring']}`)"
            w(f"| {label} | " + " | ".join(cells) + " |")
        w("")

    w("## Maturity levels")
    w("")
    for level, meaning in data["maturity_levels"].items():
        w(f"- `{level}` — {meaning}")
    w("")
    w("## How to change this")
    w("")
    for line in data["how_to_change"]:
        w(line)
    w("")
    w("## Evidence")
    w("")
    for n, item in enumerate(idx.items, 1):
        w(f"{n}. {_evidence_link(item)}")
    w("")
    return "\n".join(out)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true", help="exit 1 if the doc is stale")
    args = ap.parse_args(argv)
    text = render(load())
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        if current != text:
            print(f"docs/harness-matrix.md is stale — run: {REGEN_CMD}", file=sys.stderr)
            return 1
        print("docs/harness-matrix.md is up to date")
        return 0
    OUTPUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
