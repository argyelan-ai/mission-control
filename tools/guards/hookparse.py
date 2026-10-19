"""hookparse.py — one parser for the git calls in a live Bash command.

Used by bash-hook.sh (before the command) and post-push-hook.sh (after it), so
both hooks see the same pushes. Before this file each hook had its own regex,
and both missed the same shapes:

  * `git push --force-with-lease 2>&1 | tail -1` — the `2` of `2>&1` was read as
    the remote (or as a refspec), so the proof looked at the wrong thing and
    skipped silently. This is the exact command of the incident.
  * `git -c key=value push …` — only `-C <dir>` was allowed before `push`.
  * `git -C "$WT" push …` / `cd "$WT" && git push …` — a variable path was
    skipped without a word.

Rules here:
  * Redirections (`2>&1`, `>file`, `&>file`, `<file`) are removed before the
    words are read. A trailing `# comment` is not part of the command.
  * Git options before the sub-command may be `-C <dir>`, `-c <k=v>`,
    `--git-dir <p>`, `--work-tree <p>`, `--namespace <n>` or any `-x`/`--xy=z`.
    Several `-C` add up the way git does it.
  * A path with `$VAR` is resolved from assignments earlier in the same
    command (`WT=/x; git -C "$WT" push`). If it still has a `$` or a backquote,
    the target is UNRESOLVED — the caller must report that, never skip it.

Stdlib only.
"""
from __future__ import annotations

import os
import re
import shlex

HEREDOC = re.compile(r"<<-?\s*['\"]?(\w+)['\"]?[^\n]*\n.*?\n\s*\1\b", re.S)

# Git options that take a separate value word.
_GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env")
_WORD = r"(?:\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|<>()]+)"
GIT_OPTS = r"(?:\s+(?:" + "|".join(re.escape(o) for o in _GIT_VALUE_OPTS) + r")\s+" + _WORD + r"|\s+-[^\s;&|<>()]+)*"


def git_re(subcommands: str) -> re.Pattern:
    """`git <options> <subcommand>`; group 1 = the options text, group 2 = the sub-command."""
    return re.compile(r"(?<![\w./-])git(" + GIT_OPTS + r")\s+(" + subcommands + r")\b")


PUSH_RE = git_re("push")
_CD_RE = re.compile(r"(?:^|&&|\|\||;|\n|\()\s*cd\s+(" + _WORD + r")")
_ASSIGN_RE = re.compile(r"(?:^|[;&|\n(]|\s)(?:export\s+|local\s+|declare\s+)?([A-Za-z_]\w*)=(\"[^\"\n]*\"|'[^'\n]*'|[^\s;&|]*)")
_VAR_RE = re.compile(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))")
_CONTROL = {";", "&&", "||", "|", "&", "|&", ";;", "(", ")", "\n"}


def strip_heredocs(cmd: str) -> str:
    return HEREDOC.sub("<<HEREDOC-STRIPPED", cmd)


def _unquote(w: str) -> str:
    if len(w) >= 2 and w[0] == w[-1] and w[0] in "'\"":
        return w[1:-1]
    return w


def _assignments(text: str) -> dict:
    out: dict = {}
    for name, val in _ASSIGN_RE.findall(text):
        out[name] = _unquote(val)
    return out


def expand(path: str, before: str) -> str | None:
    """Expand $VAR from assignments in `before`; None when something stays unknown."""
    env = _assignments(before)

    def sub(m):
        name = m.group(1) or m.group(2)
        if name in env and "$" not in env[name] and "`" not in env[name]:
            return env[name]
        if name == "HOME":
            return os.path.expanduser("~")
        return m.group(0)

    p = _VAR_RE.sub(sub, _unquote(path))
    p = os.path.expanduser(p)
    if "$" in p or "`" in p:
        return None
    return p


def tokens(text: str) -> list[str]:
    """Shell words up to the end of the simple command, redirections removed."""
    text = text.split("\n", 1)[0]
    try:
        lex = shlex.shlex(text, posix=True, punctuation_chars=";&|()<>")
        lex.whitespace_split = True
        raw = list(lex)
    except ValueError:
        raw = re.split(r"\s+", text.strip())
    out: list[str] = []
    skip = False
    for t in raw:
        if skip:
            skip = False
            continue
        if t in _CONTROL or (t and set(t) <= set(";&|()") ):
            break
        if t and set(t) <= set("<>&|"):
            # a redirection operator: drop it, the fd number in front and its target
            if out and out[-1].isdigit():
                out.pop()
            skip = True
            continue
        out.append(t)
    return out


def git_opts(text: str) -> tuple[list[str], list[str]]:
    """(-C values in order, -c values) from the options text between `git` and the sub-command."""
    try:
        words = shlex.split(text, comments=False)
    except ValueError:
        words = text.split()
    dirs: list[str] = []
    confs: list[str] = []
    i = 0
    while i < len(words):
        w = words[i]
        if w in _GIT_VALUE_OPTS and i + 1 < len(words):
            if w == "-C":
                dirs.append(words[i + 1])
            elif w in ("-c", "--config-env"):
                confs.append(words[i + 1])
            i += 2
            continue
        i += 1
    return dirs, confs


def target_dir(cmd: str, m: re.Match, cwd: str) -> tuple[str | None, str]:
    """(resolved directory or None, the raw text it came from) for the git call at m."""
    before = cmd[: m.start()]
    base, raw = cwd, cwd
    for c in _CD_RE.findall(before):
        p = expand(c, before)
        raw = c
        if p is None:
            return None, f"cd {c}"
        base = p if os.path.isabs(p) else os.path.join(base, p)
    dirs, _ = git_opts(m.group(1))
    for d in dirs:
        p = expand(d, before)
        raw = d
        if p is None:
            return None, f"-C {d}"
        base = p if os.path.isabs(p) else os.path.join(base, p)
    return os.path.normpath(base), raw


SKIP_FLAGS = {"--dry-run", "-n", "--delete", "-d", "--tags", "--all", "--mirror", "--prune"}
WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}


def pushes(cmd: str, cwd: str) -> list[dict]:
    """Every `git … push` in the command.

    Each item: status ('check' | 'unresolved' | 'skipped'), dir, raw, remote
    ('' = git picks it), refspecs (list), reason (for skipped/unresolved).
    """
    cmd = strip_heredocs(cmd)
    out = []
    for m in PUSH_RE.finditer(cmd):
        d, raw = target_dir(cmd, m, cwd)
        words = tokens(cmd[m.end():])
        item = {"dir": d or "", "raw": raw, "remote": "", "refspecs": [], "reason": ""}
        if d is None:
            item.update(status="unresolved", reason=f"push target not resolvable ({raw})")
            out.append(item)
            continue
        flags = [w for w in words if w.startswith("-")]
        hit = next((w for w in flags if w in SKIP_FLAGS or w.startswith("--delete")), None)
        if hit:
            item.update(status="skipped", reason=f"push with {hit}: nothing to compare with HEAD")
            out.append(item)
            continue
        pos, skip_next = [], False
        for w in words:
            if skip_next:
                skip_next = False
                continue
            if w in WITH_VALUE:
                skip_next = True
                continue
            if w.startswith("-"):
                continue
            pos.append(w)
        refspecs = [r.lstrip("+") for r in pos[1:]]
        if any(r.startswith(":") for r in refspecs):
            item.update(status="skipped", reason="push deletes a remote branch")
            out.append(item)
            continue
        item.update(status="check", remote=pos[0] if pos else "", refspecs=refspecs)
        out.append(item)
    return out
