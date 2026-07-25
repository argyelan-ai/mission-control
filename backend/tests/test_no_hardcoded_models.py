"""Regression guard: no new hardcoded provider model-ID strings in EXECUTABLE code.

Context (docs/plans/2026-07-25-model-sanitation-and-catalog.md): several places
in this codebase used to set a fixed model name that silently overrode
``runtime.model_identifier`` — the ONE source of truth for which model an
agent actually runs (boss-host's ``ANTHROPIC_MODEL`` pin, the mc-agent-base
image's baked-in ``OPENAI_MODEL``, cli-bridge.py's provisioning default, ...).
Anthropic shipping ``claude-opus-5`` while the fleet quietly stayed pinned to
``claude-opus-4-8`` is the concrete incident this guards against. Leitprinzip:
fail fast beats running with the wrong model.

WHAT THIS TEST PROTECTS
------------------------
Scans ``backend/app/``, ``docker/``, ``scripts/`` for regex shapes that match
known provider model-ID families (``claude-opus-4-8``, ``grok-4``, ``kimi-k2``,
``glm-5``, ``gpt-5``, ``qwen3-coder``, ``minimax-m2``, ``nemotron-3``,
``deepseek-v3``, ...). Any NEW occurrence outside the allowlist below fails
the build — the idea being that a hardcoded model-ID literal appearing in
runtime/bootstrap/provisioning code is very likely a fresh instance of the
exact bug class this PR fixed.

COMMENTS AND DOCSTRINGS ARE NOT SCANNED
----------------------------------------
A model name mentioned in prose can't override anything at runtime, so prose
is stripped BEFORE the regex runs instead of being suppressed afterwards with
inline markers (an earlier revision needed ~35 such markers, which polluted
docstrings for zero safety gain). How the stripping works per file type:

* ``.py`` — the source is parsed twice. ``tokenize`` locates every COMMENT
  token and blanks the line from the ``#`` onwards (so an inline comment on a
  real code line is removed WITHOUT losing the code before it). ``ast`` then
  locates every docstring (``ast.get_docstring`` on Module / ClassDef /
  FunctionDef / AsyncFunctionDef) and blanks its full line range. What remains
  is executable code, including non-docstring string literals — a model ID in
  a dict value, default argument, or f-string still counts as a hit, which is
  the point.
  A file that fails to parse (SyntaxError / TokenizeError) is skipped for
  matching but recorded in ``UNPARSEABLE`` and reported loudly by
  ``test_no_unparseable_python_files`` — never silently swallowed.

* ``.sh`` / ``Dockerfile`` — only WHOLE comment lines are excluded, i.e. lines
  whose first non-whitespace character is ``#``. Deliberate boundary: shell has
  no tokenizer here, and stripping a trailing `` #`` would misfire on ``#``
  inside quotes, in ``${var#prefix}``, in ``$#``, or in a heredoc. Trailing
  comments on shell/Dockerfile code lines therefore still match, and the rare
  genuine case is handled with the opt-out marker below.

HOW TO LEGITIMATELY OPT OUT
-----------------------------
Put the literal marker ``model-catalog: allow`` anywhere on the offending
line. The marker is matched against the ORIGINAL line text (before comment
stripping), so a trailing ``# model-catalog: allow`` on a code line works in
every file type. With prose no longer scanned this should be needed only for
real executable code: verified dead code with zero callers (grep-verified, note
left alongside), a clearly labeled and loudly-logged intentional fallback (see
``docker/boss-host/start-claude.sh``'s legacy pin, which warns to stderr every
time it's hit), or a string literal that merely READS a model name rather than
selecting one. Do NOT use the marker to silence a hardcode that is still live
and still capable of silently overriding ``runtime.model_identifier`` — that
defeats the entire purpose of this test.

ALLOWLISTED PATHS (skipped entirely)
-------------------------------------
- backend/app/routers/models.py      — the model catalog itself (MODEL_METADATA)
- backend/alembic/                    — frozen migration history
- backend/tests/, any */tests/* dir   — test fixtures/expectations
- backend/config/model-catalog.json   — provider catalog manifest (PR B, may not exist yet)
- node_modules/, .git/                — vendored / VCS internals
- backend/app/routers/internal.py     — build_runtime_env, the one router that
  legitimately emits ``runtime.model_identifier`` as ANTHROPIC_MODEL/OPENAI_MODEL.
- backend/app/config.py               — jarvis_text_model/jarvis_stt_model are
  the Jarvis voice/text-brain's own OpenAI model settings, an independent
  subsystem that never routes through the agent-runtime table; spark_llm_model
  is a documented last-resort fallback (see its own comment) used only if
  ``runtime_model_resolver`` fails to reach the DB.
- backend/app/routers/system.py, backend/app/services/intelligence.py —
  ``IntelligenceConfig.ollama_model`` is the Intelligence-Analyst subsystem's
  own model knob (a local Ollama summarizer), unrelated to agent-runtime routing.
- backend/app/models/cost_event.py    — MODEL_COST_TABLE, a billing lookup
  dict keyed by model name (analogous to models.py's MODEL_METADATA) — reads
  a model name to look up a price, never sets one.
- backend/app/services/template_seeder.py — seeds ``AgentTemplate.default_model``,
  which only ever flows into the dead legacy ``agent.model`` free-text field
  (see spec: "agent.model wird nicht wiederbelebt"), never into
  ``runtime.model_identifier``.

KNOWN GAP (honestly disclosed, not fixed here — out of this PR's scope)
-------------------------------------------------------------------------
``backend/app/routers/agents.py`` (~line 310), ``cli_terminal.py`` (~lines 29,
415), ``skills.py`` (~line 318) and ``approvals.py`` (~line 598) all fall back
to a hardcoded model string (``"nvidia/nemotron-3-super"``, ``"glm-5.1:cloud"``,
``"minimax-m2.7"``) when the legacy ``agent.model`` free-text field is unset.
``cli_terminal.py``'s fallback in particular feeds the exact ``/provision/``
payload that ``scripts/cli-bridge.py`` now rejects when empty (this PR, task
4a) — meaning a caller going through this path never actually exercises that
guard, because it always supplies a non-empty (if stale) default first. These
are real instances of the same bug class this test protects against; they are
NOT marked with the opt-out and are deliberately left visible via
``KNOWN_UNFIXED_HARDCODES``, because hiding them would contradict the test's
purpose. Fixing them is tracked as follow-up work, not done in this PR.

SELF-TEST
---------
``test_scan_detects_hardcode`` and friends prove the detector isn't a no-op:
the same literal is planted (a) in real code — must be FOUND, (b) in a comment
— must be IGNORED, (c) in a docstring — must be IGNORED, plus the opt-out
marker and the path allowlist are each proven to suppress a hit.
"""
from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

# ── The pattern under guard ─────────────────────────────────────────────────
MODEL_ID_PATTERN = re.compile(
    r"claude-(opus|sonnet|haiku)-[0-9]"
    r"|grok-[0-9]"
    r"|kimi-k[0-9]"
    r"|glm-[0-9]"
    r"|gpt-[0-9]"
    r"|qwen[0-9.]*-"
    r"|minimax-m[0-9]"
    r"|nemotron-[0-9]"
    r"|deepseek-v[0-9]",
    re.IGNORECASE,
)

OPT_OUT_MARKER = "model-catalog: allow"

# Scan roots, relative to backend/ (this file's parent's parent == backend/,
# repo root is backend/..).
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SCAN_ROOTS = [
    REPO_ROOT / "backend" / "app",
    REPO_ROOT / "docker",
    REPO_ROOT / "scripts",
]

# File suffixes worth scanning. Dockerfiles have no suffix but are matched by
# name below.
SCAN_SUFFIXES = {".py", ".sh"}

# Paths (relative to REPO_ROOT, POSIX-style) skipped entirely.
ALLOWED_PATHS = {
    "backend/app/routers/models.py",
    "backend/app/routers/internal.py",
    "backend/app/config.py",
    "backend/app/routers/system.py",
    "backend/app/services/intelligence.py",
    "backend/app/models/cost_event.py",
    "backend/app/services/template_seeder.py",
    "backend/config/model-catalog.json",
}

# Directory name fragments that exempt an entire subtree.
ALLOWED_DIR_FRAGMENTS = {
    "/backend/alembic/",
    "/tests/",
    "/node_modules/",
    "/.git/",
}


def _is_scannable(path: Path) -> bool:
    if path.is_dir():
        return False
    posix = "/" + path.as_posix().strip("/") + "/"  # pad for fragment matching
    if any(frag in posix for frag in ALLOWED_DIR_FRAGMENTS):
        return False
    try:
        rel = path.relative_to(REPO_ROOT).as_posix()
    except ValueError:
        rel = str(path)
    if rel in ALLOWED_PATHS:
        return False
    if rel.startswith("backend/tests/"):
        return False
    if path.name == "Dockerfile":
        return True
    return path.suffix in SCAN_SUFFIXES


class UnparseableFile(Exception):
    """Raised internally when a .py file cannot be tokenized/parsed."""


def strip_python_prose(text: str) -> list[str]:
    """Return `text`'s lines with comments and docstrings blanked out.

    Comments are cut from the ``#`` to end-of-line (code before them survives);
    docstrings are blanked over their whole line range. Everything else — real
    string literals included — is left intact, because a model ID in a dict
    value or a default argument IS a live hardcode.

    Raises ``UnparseableFile`` if the source neither tokenizes nor parses.
    """
    lines = text.splitlines()

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, SyntaxError, IndentationError) as exc:
        raise UnparseableFile(f"tokenize failed: {exc}") from exc

    for tok in tokens:
        if tok.type == tokenize.COMMENT:
            row, col = tok.start
            if 1 <= row <= len(lines):
                lines[row - 1] = lines[row - 1][:col]

    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError) as exc:
        raise UnparseableFile(f"ast.parse failed: {exc}") from exc

    doc_owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, doc_owners):
            continue
        if ast.get_docstring(node, clean=False) is None:
            continue
        doc_expr = node.body[0]
        start = doc_expr.lineno
        end = getattr(doc_expr, "end_lineno", start) or start
        for row in range(start, end + 1):
            if 1 <= row <= len(lines):
                lines[row - 1] = ""

    return lines


def strip_shell_prose(text: str) -> list[str]:
    """Return `text`'s lines with WHOLE comment lines blanked out.

    Only lines whose first non-whitespace character is ``#`` are dropped.
    Trailing comments are intentionally NOT stripped — see the module
    docstring for why (quotes, ``${v#p}``, ``$#``, heredocs).
    """
    return ["" if line.lstrip().startswith("#") else line for line in text.splitlines()]


# Populated by scan_for_hardcoded_models(): .py files that could not be parsed.
UNPARSEABLE: list[tuple[str, str]] = []


def scan_for_hardcoded_models(roots: list[Path]) -> list[tuple[str, int, str]]:
    """Scan `roots` for un-allowlisted model-ID literals in executable code.

    Returns a list of (relative_path, line_no, original_line_text) findings.
    Side effect: resets and repopulates the module-level ``UNPARSEABLE`` list.
    """
    findings: list[tuple[str, int, str]] = []
    UNPARSEABLE.clear()

    for root in roots:
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if not _is_scannable(path):
                continue
            try:
                text = path.read_text(errors="ignore")
            except OSError:
                continue
            try:
                rel = path.relative_to(REPO_ROOT).as_posix()
            except ValueError:
                rel = str(path)

            original = text.splitlines()
            if path.suffix == ".py":
                try:
                    scannable = strip_python_prose(text)
                except UnparseableFile as exc:
                    UNPARSEABLE.append((rel, str(exc)))
                    continue
            else:
                scannable = strip_shell_prose(text)

            for i, line in enumerate(scannable, start=1):
                if not line.strip():
                    continue
                # Marker is checked against the ORIGINAL line so a trailing
                # "# model-catalog: allow" comment still opts a code line out.
                if OPT_OUT_MARKER in original[i - 1]:
                    continue
                if MODEL_ID_PATTERN.search(line):
                    findings.append((rel, i, original[i - 1].strip()))
    return findings


# ── Known, deliberately-not-fixed gap (see module docstring "KNOWN GAP") ────
# These are real pre-existing hardcodes outside this PR's explicit scope.
# Listed explicitly — NOT silenced via the opt-out marker in source — so this
# test documents them as a visible, tracked gap instead of hiding them. If any
# of these disappear (fixed) or new ones appear, this set must be updated by
# hand; it is not an allowlist, it is a to-do list with a name.
# Line numbers verified 2026-07-25: agents.py:310, cli_terminal.py:29+415,
# skills.py:318, approvals.py:598.
KNOWN_UNFIXED_HARDCODES = {
    ("backend/app/routers/agents.py", 'model=agent.model or "nvidia/nemotron-3-super"'),
    ("backend/app/routers/cli_terminal.py", 'model: str = "nvidia/nemotron-3-super"'),
    ("backend/app/routers/cli_terminal.py", 'or "nvidia/nemotron-3-super"'),
    ("backend/app/routers/skills.py", 'current_model = agent.model or "minimax-m2.7"'),
    ("backend/app/routers/approvals.py", '"glm-5.1:cloud"'),
}


def _is_known_unfixed(path: str, line: str) -> bool:
    return any(path == p and needle in line for p, needle in KNOWN_UNFIXED_HARDCODES)


def test_no_new_hardcoded_models():
    """Fails if a hardcoded model-ID literal appears in executable code.

    Comments and docstrings are stripped before matching (see module
    docstring), so a hit here is real code. Pre-existing, explicitly
    out-of-scope hardcodes (KNOWN_UNFIXED_HARDCODES) are reported separately
    as an expected, tracked gap rather than silently ignored.
    """
    findings = scan_for_hardcoded_models(SCAN_ROOTS)

    unexpected = [
        (path, line_no, line)
        for path, line_no, line in findings
        if not _is_known_unfixed(path, line)
    ]

    assert not unexpected, (
        "Hardcoded model-ID literal(s) found in executable code outside the "
        "allowlist — this silently overrides runtime.model_identifier, the "
        "single source of truth (docs/plans/2026-07-25-model-sanitation-and-"
        "catalog.md). Either fix it to read from runtime.model_identifier / "
        "fail fast, or if this line genuinely cannot override a live runtime "
        "(verified dead code, a labeled loud fallback), mark it with "
        "'# model-catalog: allow'.\n\n"
        + "\n".join(f"  {p}:{n}: {t}" for p, n, t in unexpected)
    )


def test_no_unparseable_python_files():
    """Every scanned .py file must actually parse.

    A file that neither tokenizes nor parses is skipped by the scanner, which
    would create a silent blind spot. Report it loudly instead.
    """
    scan_for_hardcoded_models(SCAN_ROOTS)
    assert not UNPARSEABLE, (
        "Python file(s) could not be parsed and were therefore NOT scanned for "
        "hardcoded model IDs — fix the syntax or explicitly allowlist the "
        "path:\n" + "\n".join(f"  {p}: {why}" for p, why in UNPARSEABLE)
    )


def test_known_gap_still_matches_expected_shape():
    """Sanity check on KNOWN_UNFIXED_HARDCODES itself.

    If this fails, either one of the tracked pre-existing hardcodes got fixed
    (great — remove it from KNOWN_UNFIXED_HARDCODES) or the line changed shape
    and the tracking needs updating. Either way it should not be silently
    absorbed into test_no_new_hardcoded_models going green for the wrong
    reason.
    """
    findings = scan_for_hardcoded_models(SCAN_ROOTS)
    known_paths = {p for p, _ in KNOWN_UNFIXED_HARDCODES}
    matched_paths = {path for path, _, line in findings if _is_known_unfixed(path, line)}
    missing = known_paths - matched_paths
    assert not missing, (
        f"Expected pre-existing hardcode(s) no longer found at: {sorted(missing)} — "
        "if fixed, remove them from KNOWN_UNFIXED_HARDCODES; if the line just "
        "changed shape, update the needle text there."
    )


# ── Self-test: proves the detector detects, and ignores only prose ──────────

def test_scan_detects_hardcode(tmp_path: Path):
    """(a) A hardcode in REAL code must be found — the detector is not a no-op."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    bad_file = target_dir / "fake_runtime_env.py"
    bad_file.write_text('ANTHROPIC_MODEL = "claude-opus-4-8"\n')

    findings = scan_for_hardcoded_models([tmp_path])

    assert findings, "scanner failed to detect a deliberately planted hardcode"
    assert any(bad_file.name == Path(p).name for p, _, _ in findings)
    assert any("claude-opus-4-8" in line.lower() for _, _, line in findings)


def test_scan_ignores_model_id_in_comment(tmp_path: Path):
    """(b) The SAME literal inside a comment must be ignored."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    (target_dir / "fake_comment.py").write_text(
        "# historically we pinned claude-opus-4-8 here; now it comes from the runtime\n"
        "MODEL = os.environ.get('ANTHROPIC_MODEL')  # was claude-opus-4-8 once\n"
    )

    findings = scan_for_hardcoded_models([tmp_path])

    assert not findings, f"a model ID inside a comment was reported: {findings}"


def test_scan_ignores_model_id_in_docstring(tmp_path: Path):
    """(c) The SAME literal inside module/class/function docstrings must be ignored."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    (target_dir / "fake_docstring.py").write_text(
        '"""Module doc mentioning claude-opus-4-8 and gpt-5 historically."""\n'
        "\n\n"
        "class Runner:\n"
        '    """Class doc: used to run grok-4.\n'
        "\n"
        "    Multi-line docstrings must be excluded over their FULL range,\n"
        "    including a line naming minimax-m2.5 in the middle.\n"
        '    """\n'
        "\n"
        "    def run(self):\n"
        '        """Func doc: previously kimi-k2, now runtime-driven."""\n'
        "        return None\n"
    )

    findings = scan_for_hardcoded_models([tmp_path])

    assert not findings, f"a model ID inside a docstring was reported: {findings}"


def test_scan_still_finds_code_on_a_line_with_a_trailing_comment(tmp_path: Path):
    """Stripping an inline comment must not lose the code before it."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    (target_dir / "fake_inline.py").write_text(
        'MODEL = "claude-opus-4-8"  # totally harmless looking comment\n'
    )

    findings = scan_for_hardcoded_models([tmp_path])

    assert findings, "inline-comment stripping wrongly swallowed the code before it"


def test_scan_ignores_shell_comment_but_finds_shell_code(tmp_path: Path):
    """Shell/Dockerfile: leading-# lines ignored, real assignments still found."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    (target_dir / "fake_ok.sh").write_text(
        "#!/bin/bash\n"
        "   # we used to export claude-opus-4-8 here\n"
        'echo "hello"\n'
    )
    (target_dir / "fake_bad.sh").write_text(
        "#!/bin/bash\n" 'export ANTHROPIC_MODEL="claude-opus-4-8"\n'
    )

    findings = scan_for_hardcoded_models([tmp_path])

    reported = {Path(p).name for p, _, _ in findings}
    assert "fake_ok.sh" not in reported, f"shell comment was reported: {findings}"
    assert "fake_bad.sh" in reported, "shell code hardcode was NOT reported"


def test_scan_respects_opt_out_marker(tmp_path: Path):
    """A code line carrying the opt-out marker must NOT be reported."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    marked_file = target_dir / "fake_marked.py"
    marked_file.write_text(
        'ANTHROPIC_MODEL = "claude-opus-4-8"  # model-catalog: allow (test fixture)\n'
    )

    findings = scan_for_hardcoded_models([tmp_path])

    assert not findings, f"opt-out marker did not suppress a finding: {findings}"


def test_scan_reports_unparseable_python(tmp_path: Path):
    """A .py file that cannot be parsed must be recorded, not silently skipped."""
    target_dir = tmp_path / "app"
    target_dir.mkdir()
    (target_dir / "broken.py").write_text('def f(:\n    MODEL = "claude-opus-4-8"\n')

    scan_for_hardcoded_models([tmp_path])

    assert any(p.endswith("broken.py") for p, _ in UNPARSEABLE), (
        f"unparseable file was swallowed silently: {UNPARSEABLE}"
    )


def test_scan_respects_path_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A file under an allowlisted relative path must be skipped, marker or not."""
    fake_models_dir = tmp_path / "backend" / "app" / "routers"
    fake_models_dir.mkdir(parents=True)
    fake_models_file = fake_models_dir / "models.py"
    fake_models_file.write_text('MODEL = "claude-opus-4-8"\n')

    monkeypatch.setattr("tests.test_no_hardcoded_models.REPO_ROOT", tmp_path)
    monkeypatch.setattr(
        "tests.test_no_hardcoded_models.ALLOWED_PATHS",
        {"backend/app/routers/models.py"},
    )

    findings = scan_for_hardcoded_models([tmp_path])

    assert not findings, f"path allowlist did not suppress a finding: {findings}"
