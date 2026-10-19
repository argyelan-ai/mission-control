#!/usr/bin/env python3
"""gate-audit: is every gate in this repository honest?

A gate is anything that is supposed to turn red and stop a change: a CI job,
a Claude Code hook script, a guard script. An honest gate has a documented
red case ("blocks when") and a sabotage probe or a recorded red run that
proves the red case really fires. The register is tools/guards/GATES.md.

Red (exit 1) when:
  R1  a workflow job (.github/workflows/*.yml) is not in GATES.md
  R2  a hook / guard script (tools/hooks/*.sh, tools/guards/*.py|*.sh) is not
      in GATES.md
  R3  a row of kind Gate or Hinweis has no red case ("blockt bei") or no
      sabotage probe / red-run evidence
  R4  a row points at a file or workflow job that does not exist (stale row)
  R5  a script registered as Gate has no path that can exit non-zero
      (e.g. a placeholder that is only `exit 0`)
  R6  a soft-fail in a workflow (`continue-on-error: true`, `|| true`,
      `|| :`, `exit 0` in a run block) has no `# gate-audit: soft - <reason>`
      marker on the same line or in the comment block directly above it
  R7  a job with `continue-on-error: true` is registered as Gate (it can never
      block - register it as Hinweis and say why)
  R5  also: a job with `if: false` / `if: ${{ false }}` registered as Gate
  R8  a CI job of kind Gate whose Merge-Pflicht is "nein" (the merge queue
      ignores it - say so: kind "Gate (nicht Pflicht)"), or the reverse
  R9  (only with --verify-runs, needs the GitHub API) a run id in the
      evidence column does not exist, belongs to another workflow, or is
      called "Rot-Lauf" but did not fail (run, or the row's own job)

Honest limits: this is a static read of the tree. A script that hides its
exit code (`exit "$?"` after a command that cannot fail) passes R5; whether
a hook is REGISTERED lives in private Claude Code settings this job cannot
see. The hook tests (real sabotage probes) are the stronger proof.

Stdlib only (runs on a bare runner and on python 3.9). Sabotage probe:
tools/guards/tests/test_gate_audit.py (a dummy gate with `exit 0` -> red).
"""
import argparse
import re
import sys
from pathlib import Path

GATE_KINDS = {"gate", "gate (nicht pflicht)"}
KINDS = GATE_KINDS | {"hinweis", "helfer"}
EMPTY = {"", "-", "—", "–", "?", "nie", "never", "n/a"}
MISSING_PREFIXES = ("offen", "keine", "fehlt", "todo", "none", "missing")
SOFT_MARKER = re.compile(r"#\s*gate-audit:\s*soft\s*[-—–:]+\s*\S")

SH_RED = re.compile(r"\bexit\s+(\$|\"\$|[1-9])|\bset\s+-[a-z]*e|\breturn\s+[1-9]|\bfalse\s*$")
PY_RED = re.compile(r"sys\.exit\(\s*(?!0\s*\))\S|\braise\b|SystemExit|\bexit\(\s*[1-9]")
PLACEHOLDER_LINES = {"exit", "exit 0", "true", ":", "set -e", "set -u", "set -eu", "set -euo pipefail"}


def strip_sh(text):
    out = []
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        out.append(s)
    return out


def script_has_red_path(path):
    text = path.read_text(errors="replace")
    if path.suffix == ".py":
        code = "\n".join(l for l in text.splitlines() if not l.strip().startswith("#"))
        return bool(PY_RED.search(code))
    lines = strip_sh(text)
    if all(l in PLACEHOLDER_LINES for l in lines):
        return False
    return any(SH_RED.search(l) for l in lines)


DISABLED_IF = re.compile(r"""^(?:false|'false'|"false"|\$\{\{\s*false\s*\}\})\s*(?:#.*)?$""")


def workflow_jobs(path):
    """Job ids under the top-level `jobs:` key. The indentation is taken from
    the first key under `jobs:` (2 or 4 spaces both work), job properties
    from the first key under each job."""
    jobs, in_jobs, current = {}, False, None
    job_indent, prop_indent = None, None
    for i, line in enumerate(path.read_text().splitlines()):
        if re.match(r"^jobs:\s*$", line):
            in_jobs = True
            continue
        if in_jobs and re.match(r"^\S", line):
            in_jobs = False
        if not in_jobs or not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        if job_indent is None:
            job_indent = indent
        if indent == job_indent:
            m = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(?:#.*)?$", line)
            if m:
                current = m.group(1)
                jobs[current] = {"line": i + 1, "continue_on_error": False, "disabled": False, "name": ""}
                prop_indent = None
            continue
        if current is None or indent < job_indent:
            continue
        if prop_indent is None:
            prop_indent = indent
        if indent != prop_indent:
            continue
        m = re.match(r"^\s*([A-Za-z0-9_-]+):\s*(.*?)\s*$", line)
        if not m:
            continue
        key, val = m.group(1), m.group(2)
        if key == "continue-on-error" and re.match(r"true\b", val):
            jobs[current]["continue_on_error"] = True
        elif key == "if" and DISABLED_IF.match(val):
            jobs[current]["disabled"] = True
        elif key == "name":
            jobs[current]["name"] = val.strip("'\"")
    return jobs


def soft_fail_findings(path, rel):
    findings = []
    lines = path.read_text().splitlines()
    for i, line in enumerate(lines):
        code = line.split(" #", 1)[0] if not line.strip().startswith("#") else ""
        hit = None
        if re.search(r"continue-on-error:\s*true\b", code):
            hit = "continue-on-error: true"
        elif re.search(r"\|\|\s*(true|:)(\s|;|$)", code):
            hit = "'|| true'"
        elif re.search(r"(^|[;&|]\s*|\s)exit\s+0\b", code):
            hit = "'exit 0' in a run block"
        if not hit:
            continue
        # The line itself plus the comment block directly above it.
        window, j = [line], i - 1
        while j >= 0 and lines[j].strip().startswith("#"):
            window.append(lines[j])
            j -= 1
        if not any(SOFT_MARKER.search(w) for w in window):
            findings.append(
                f"R6 {rel}:{i + 1}: {hit} without '# gate-audit: soft - <reason>' "
                "(a soft-fail must say why it may never block)"
            )
    return findings


def parse_register(path):
    rows = []
    for n, line in enumerate(path.read_text().splitlines(), 1):
        if not line.startswith("| `"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 6:
            rows.append({"line": n, "error": f"expected 6 columns, got {len(cells)}"})
            continue
        gid = cells[0].strip("`")
        rows.append({
            "line": n, "id": gid, "kind": cells[1].lower(), "what": cells[2],
            "blocks": cells[3], "probe": cells[4], "required": cells[5],
        })
    return rows


def missing(value):
    v = value.strip().lower()
    return v in EMPTY or v.startswith(MISSING_PREFIXES)


def audit(root):
    root = Path(root)
    register = root / "tools/guards/GATES.md"
    findings = []
    if not register.exists():
        return ["R0 tools/guards/GATES.md is missing - no register, no honest gates"], {}
    rows = parse_register(register)
    by_id = {}
    for r in rows:
        if "error" in r:
            findings.append(f"R3 GATES.md:{r['line']}: {r['error']}")
            continue
        if r["kind"] not in KINDS:
            findings.append(
                f"R3 GATES.md:{r['line']}: kind '{r['kind']}' is not Gate / Gate (nicht Pflicht) / Hinweis / Helfer")
        if r["id"] in by_id:
            findings.append(f"R3 GATES.md:{r['line']}: duplicate id {r['id']}")
        by_id[r["id"]] = r

    # R1 + R7: workflow jobs
    wf_ids = set()
    for wf in sorted((root / ".github/workflows").glob("*.y*ml")):
        rel = f".github/workflows/{wf.name}"
        findings += soft_fail_findings(wf, rel)
        for job, info in workflow_jobs(wf).items():
            gid = f"ci:{wf.name}#{job}"
            wf_ids.add(gid)
            row = by_id.get(gid)
            if not row:
                findings.append(f"R1 {rel}:{info['line']}: job '{job}' is not in GATES.md (id `{gid}`)")
            elif info["continue_on_error"] and row["kind"] in GATE_KINDS:
                findings.append(
                    f"R7 GATES.md:{row['line']}: {gid} has continue-on-error and can never block - "
                    "register it as Hinweis"
                )
            elif info["disabled"] and row["kind"] in GATE_KINDS:
                findings.append(
                    f"R5 GATES.md:{row['line']}: {gid} has `if: false` at job level and never runs - "
                    "it cannot turn red"
                )

    # R2: hook and guard scripts
    script_ids = set()
    candidates = list((root / "tools/hooks").glob("*.sh")) + [
        p for p in (root / "tools/guards").glob("*") if p.suffix in (".py", ".sh")
    ]
    for p in sorted(candidates):
        rel = p.relative_to(root).as_posix()
        gid = f"script:{rel}"
        script_ids.add(gid)
        if gid not in by_id:
            findings.append(f"R2 {rel}: script is not in GATES.md (id `{gid}`)")

    # R3 / R4 / R5 per row
    for gid, r in by_id.items():
        where = f"GATES.md:{r['line']} {gid}"
        if gid.startswith("ci:"):
            if gid not in wf_ids:
                findings.append(f"R4 {where}: no such workflow job (stale row)")
        elif gid.startswith(("script:", "test:")):
            p = root / gid.split(":", 1)[1]
            if not p.exists():
                findings.append(f"R4 {where}: file does not exist (stale row)")
            elif r["kind"] in GATE_KINDS and gid.startswith("script:") and not script_has_red_path(p):
                findings.append(f"R5 {where}: registered as Gate but the script can never exit non-zero")
        elif not gid.startswith("privat:"):
            findings.append(f"R3 {where}: id must start with ci:, script:, test: or privat:")
        if gid.startswith("ci:"):
            req = r["required"].strip().lower()
            if r["kind"] == "gate" and req.startswith(("nein", "no")):
                findings.append(
                    f"R8 {where}: Gate without merge duty is invisible to the merge queue - "
                    "register it as 'Gate (nicht Pflicht)' or make it a required check"
                )
            elif r["kind"] == "gate (nicht pflicht)" and req.startswith(("ja", "yes")):
                findings.append(f"R8 {where}: 'Gate (nicht Pflicht)' but Merge-Pflicht says ja")
        if r["kind"] in GATE_KINDS | {"hinweis"}:
            if missing(r["blocks"]):
                findings.append(f"R3 {where}: no red case in 'blockt bei'")
            if missing(r["probe"]):
                findings.append(f"R3 {where}: no sabotage probe / red-run evidence")
        elif missing(r["what"]):
            findings.append(f"R3 {where}: Helfer row must say what it does and why it is no gate")

    stats = {
        "rows": len(by_id),
        "gates": sum(1 for r in by_id.values() if r["kind"] == "gate"),
        "optional": sum(1 for r in by_id.values() if r["kind"] == "gate (nicht pflicht)"),
        "hints": sum(1 for r in by_id.values() if r["kind"] == "hinweis"),
        "no_red_case": sum(1 for f in findings if f.startswith(("R5", "R7")) or "no red case" in f),
        "not_required": sum(1 for f in findings if f.startswith("R8")),
        "no_probe": sum(1 for f in findings if "no sabotage probe" in f),
        "unregistered": sum(1 for f in findings if f.startswith(("R1", "R2"))),
        "unjustified_soft": sum(1 for f in findings if f.startswith("R6")),
    }
    return findings, stats


RUN_REF = re.compile(r"\b(Rot-Lauf|Lauf)\s+(\d{6,})")


def gh_fetch(path):
    """GET a GitHub API path with `gh api` (needs GH_TOKEN in CI)."""
    import json
    import subprocess
    p = subprocess.run(["gh", "api", path], capture_output=True, text=True, timeout=30)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) else "gh api failed")
    return json.loads(p.stdout)


def verify_runs(root, fetch, repo="argyelan-ai/mission-control"):
    """R9: every run id in the evidence column must exist. 'Rot-Lauf <id>'
    must be a FAILED run of the row's own workflow, and if that job ran in
    it under its display name, the job itself must have failed. 'Lauf <id>'
    (no 'Rot-') only has to exist in that workflow (for Hinweis rows that
    are red-but-green by design, or a job that was skipped)."""
    root = Path(root)
    rows = [r for r in parse_register(root / "tools/guards/GATES.md") if "error" not in r]
    names = {}
    for wf in (root / ".github/workflows").glob("*.y*ml"):
        for job, info in workflow_jobs(wf).items():
            names[f"ci:{wf.name}#{job}"] = info["name"]
    out = []
    for r in rows:
        gid = r["id"]
        wf_name = gid[3:].split("#")[0] if gid.startswith("ci:") else None
        for kind, rid in RUN_REF.findall(r["probe"]):
            where = f"GATES.md:{r['line']} {gid} run {rid}"
            try:
                run = fetch(f"repos/{repo}/actions/runs/{rid}")
            except Exception as e:  # noqa: BLE001 - any failure means unverified
                out.append(f"R9 {where}: cannot be verified ({e})")
                continue
            path = run.get("path", "")
            if wf_name and not path.endswith(f"/{wf_name}"):
                out.append(f"R9 {where}: belongs to {path or '?'}, not {wf_name}")
                continue
            if kind != "Rot-Lauf":
                continue
            if run.get("conclusion") != "failure":
                out.append(f"R9 {where}: 'Rot-Lauf' but the run ended '{run.get('conclusion')}'")
                continue
            display = names.get(gid, "")
            if not display or "${{" in display:
                continue
            try:
                jobs = fetch(f"repos/{repo}/actions/runs/{rid}/jobs?per_page=100").get("jobs", [])
            except Exception as e:  # noqa: BLE001
                out.append(f"R9 {where}: jobs cannot be verified ({e})")
                continue
            mine = [j for j in jobs if j.get("name") == display or j.get("name", "").startswith(display + " (")]
            if mine and not any(j.get("conclusion") == "failure" for j in mine):
                out.append(f"R9 {where}: job '{display}' did not fail in that run (other jobs did)")
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--root", default=Path(__file__).resolve().parents[2])
    ap.add_argument("--verify-runs", action="store_true",
                    help="R9: check every run id in the evidence column against the GitHub API (needs gh + token)")
    args = ap.parse_args(argv)
    findings, stats = audit(args.root)
    if stats and args.verify_runs:
        findings += verify_runs(args.root, gh_fetch)
    for f in findings:
        print(f"RED  {f}")
    if stats:
        print(
            f"gate-audit: {stats['rows']} rows ({stats['gates']} Gate, {stats['optional']} Gate nicht Pflicht, "
            f"{stats['hints']} Hinweis) | without red case: {stats['no_red_case']} | "
            f"without probe: {stats['no_probe']} | unregistered: {stats['unregistered']} | "
            f"unjustified soft-fails: {stats['unjustified_soft']} | Gate not required: {stats['not_required']}"
            + (f" | unverified runs: {sum(1 for f in findings if f.startswith('R9'))}" if args.verify_runs else "")
        )
    print("gate-audit: FAIL" if findings else "gate-audit: ok")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
