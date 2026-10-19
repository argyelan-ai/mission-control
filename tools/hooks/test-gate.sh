#!/usr/bin/env bash
# test-gate — Claude Code PreToolUse hook (matcher: Bash).
#
# Blocks a push or a PR creation while a test AFFECTED by the branch is red.
# "A failing test first, then the change" (AGENTS.md) only means something if
# the tests are green when the branch leaves the machine.
#
# Two ways to run it:
#   PreToolUse hook (default): reads the command line BEFORE it runs.
#   --pre-push: git's own pre-push hook (refs on stdin, cwd = the pushing
#     checkout). git runs it for EVERY spelling of a push - this is the choke
#     point; the command reader below can never be complete.
# What the reader counts as a push / PR creation (see cmdparse):
#   git push with git -c k=v / --no-pager / -C dir / --git-dir, "git" or
#   /usr/bin/git, behind env/command/timeout/if..then, inside ( ), $( ),
#   backticks, bash -c / -lc / -ec '...', eval, a heredoc / here-string / pipe
#   / < file fed to a shell, a script file run with bash/sh/source/./x.sh,
#   python -c / node -e code, find -exec, awk system(), git aliases (also -c
#   alias.x=push) and git send-pack; gh pr create / gh pr new, gh aliases, gh
#   api .../pulls with fields, curl POST to .../pulls.
#   Blocked as unreadable ("hidden"): git/gh behind a variable or run by
#   xargs/parallel, a shell fed from a pipe that mentions git, python/node
#   code that starts programs and glues "push" together.
#   Known gaps (no blacklist reader closes them): curl ... | sh, a compiled
#   binary, code that builds the word from bytes. --pre-push closes them.
# Which checkout is tested: the one the command really targets - after cd /
#   pushd / -C / GIT_DIR (a cd in ( ), $( ), bash -c or a pipe stays inside;
#   cd -, popd and a cd that may not have run make it unknown = block), the
#   worktree that has the pushed branch checked out (git push origin
#   other-branch), the worktree of --head (gh pr create --head x from
#   anywhere, also -R owner/repo from outside a checkout).
#
# Red case (exit 2 = Claude Code blocks the tool call, stderr goes to the agent):
#   - an affected test is red (and it is not red on origin/main too):
#       changed test files; backend/app/a/b.py -> backend tests named test_b*
#       or referencing app.a.b; component code (docker/x, scripts/mc-cli,
#       tools/x, scripts/*.sh) -> that component's tests that name or import
#       it, plus backend tests naming the file; frontend-v2/src -> vitest
#       related (the import graph), config/setup change -> whole vitest run;
#       scripts/tests/test_*.sh -> bash
#   - the checkout cannot be told (path behind a variable, unknown cd,
#     branch not checked out anywhere, gh pr create -R x outside a checkout,
#     or the command seems to run outside a checkout while the session is in one)
#   - git/gh the reader cannot see ($G push, xargs git, echo .. | sh)
#   - the reader itself fails on a command that mentions push/pr
#   - tracked files changed but not committed (tests would see other code than
#     what is pushed) - unless the same command commits first
#   - a test file deleted without a "Removed-Test: <path> - <reason>" trailer
#   - push --all / --mirror (push one branch at a time)
#   - frontend changed, node_modules missing although the shared one exists
#   - the time budget is used up (a hook killed by Claude Code would NOT block)
# Green: no affected test, or all affected tests pass.
# Honest limits (stated on stderr, never silent):
#   - at most MC_TEST_GATE_MAX_FILES (80) test files per python suite, closest
#     first (changed tests, then same name, then importers); CI runs the rest
#   - failures that also fail on the merge base are reported, not blocked
#     (checked in a throwaway worktree that is removed again)
#   - no python with pytest / no node_modules anywhere -> that suite is skipped
#   - origin/main unknown -> skipped; weakened asserts are the reviewer's job
#   - only reaches Claude Code sessions that register this hook; CI reaches all
# Budget: MC_TEST_GATE_BUDGET (270 s, below the 300 s hook timeout),
#   per suite MC_TEST_GATE_TIMEOUT (240 s).
# Sabotage probes: tools/hooks/tests/test_test_gate.py, test_cmdparse.py.
#
# Input: the hook JSON on stdin ({"tool_input":{"command":...},"cwd":...}).
set -u
mode=hook
if [ "${1:-}" = "--pre-push" ]; then mode=pre-push; fi
command -v python3 >/dev/null 2>&1 || { echo "test-gate: python3 missing - gate skipped" >&2; exit 0; }
payload="$(cat)"
MC_HOOK_MODE="$mode" MC_HOOK_PAYLOAD="$payload" exec python3 - <<'PY'
import io as _io, os as _os_boot, sys as _sys_boot
_sys_boot.stdin = _io.StringIO(_os_boot.environ.get("MC_HOOK_PAYLOAD", ""))
# >>> cmdparse (shared by test-gate.sh and secrets-check.sh - keep both copies identical)
# Reads a shell command line the way a shell would (quotes, ;, &&, |, $( ),
# backticks, heredocs, cd, wrappers like env/xargs/timeout, bash -c, eval,
# script files, git/gh aliases) and returns what it would do to git:
#   ("push",   {"dir", "git_dir", "work_tree", "refs", "flags", "remote"})
#   ("commit", {"dir", "git_dir", "work_tree", "all", "paths"})
#   ("add",    {"dir"})
#   ("pr",     {"dir", "head", "repo"})
#   ("bypass", {"why"})      - skips a hook (--no-verify, core.hooksPath, ...)
#   ("hidden", {"why"})      - git/gh whose words the reader cannot see (a
#                              variable, xargs input, a pipe into a shell,
#                              assembled strings in python/node code)
# "dir" is None when the folder cannot be known for sure: a cd behind a
# variable, cd - / popd, a cd that may or may not have run (false && cd x; ...).
# ( ), $( ), bash -c and pipeline parts are subshells: their cd stays inside.
# This is a blacklist reader and cannot be complete (python with split
# strings, curl | sh, a binary). The pre-push mode of test-gate.sh is the
# choke point git itself runs for every spelling.
import os as _os
import re as _re
import subprocess as _sp

_SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
_INTERP = _re.compile(r"^(python[0-9.]*|node|perl|ruby)$")
_WRAPPERS = {"env", "command", "builtin", "exec", "nohup", "time", "nice", "sudo", "doas",
             "xargs", "timeout", "gtimeout", "caffeinate", "stdbuf", "ionice", "watch",
             "noglob", "nocorrect", "unbuffer", "flock", "chronic", "then", "do", "else",
             "elif", "if", "while", "until", "!", "{", "}", "parallel", "script"}
_GIT_VERBS = set("""add am annotate apply archive bisect blame branch bundle cat-file check-attr
check-ignore check-ref-format checkout cherry cherry-pick citool clean clone commit commit-tree
config count-objects describe diff diff-files diff-index diff-tree difftool fetch filter-branch
format-patch fsck gc grep gui hash-object help init instaweb log ls-files ls-remote ls-tree
merge merge-base mergetool mv notes pull push range-diff rebase reflog remote repack replace
request-pull reset restore rev-list rev-parse revert rm send-email send-pack shortlog show
show-branch show-ref sparse-checkout stash status submodule switch symbolic-ref tag
update-index update-ref var verify-commit verify-tag whatchanged worktree write-tree maintenance
for-each-ref name-rev prune prune-packed read-tree mktree pack-objects unpack-objects
credential lfs fsmonitor--daemon scalar backfill""".split())
_GH_TOP = set("""agent-task alias api attestation auth browse cache co codespace completion config
copilot extension gist gpg-key help issue label org pr preview project release repo ruleset run
search secret ssh-key status variable workflow""".split())
_GIT_OPT_ARG = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix",
                "--config-env", "--exec-path", "--list-cmds", "--attr-source"}
_HEREDOC = _re.compile(r"<<-?[ \t]*(['\"]?)([A-Za-z_][\w-]*)\1([^\n]*)\n(.*?)\n[ \t]*\2[ \t]*(?=\n|$)", _re.S)
_MAX_FILE = 256 * 1024


class _W(str):
    """A word; .dyn is True when it contains an unquoted/double-quoted $ expansion.
    .subs holds the $( ) / backtick bodies inside it (run before the command)."""
    dyn = False
    subs = ()


class _Tok(str):
    """A marker that is neither a word nor a separator: .body is its text."""
    body = ""


class _HD(_Tok):
    """stdin from a heredoc or here-string."""


class _RIN(_Tok):
    """stdin from a file (< file)."""


class _PS(_Tok):
    """process substitution <( ) / >( ) - runs in a subshell."""


def _tok(cls, body):
    t = cls(cls.__name__)
    t.body = body
    return t


_KNOWN_SUBS = ("pwd", "pwd -P", "git rev-parse --show-toplevel")


def _subst(body):
    b = " ".join(body.split())
    return list("$(" + b + ")") if b in _KNOWN_SUBS else list("$(...)")


def _word(chars, dyn, subs=()):
    w = _W("".join(chars))
    w.dyn = dyn
    w.subs = tuple(subs)
    return w


def _matching(s, i, open_c="(", close_c=")"):
    depth, q = 0, None
    while i < len(s):
        c = s[i]
        if q:
            if c == "\\" and q == '"':
                i += 2
                continue
            if c == q:
                q = None
        elif c in "'\"":
            q = c
        elif c == open_c:
            depth += 1
        elif c == close_c:
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return len(s)


def _lex(s, heredocs=()):
    """-> (tokens, substitutions). Separators are plain str tokens ';' '&&' '||'
    '|' '&' '(' ')'; words are _W; stdin sources and process substitutions are
    _Tok markers. Output redirections (> file, 2>&1, &>x) are dropped."""
    toks, subs, cur, dyn, wsubs, i, n = [], [], None, False, [], 0, len(s)

    def flush():
        nonlocal cur, dyn, wsubs
        if cur is not None:
            toks.append(_word(cur, dyn, wsubs))
        cur, dyn, wsubs = None, False, []

    while i < n:
        c = s[i]
        if c in " \t\r":
            flush(); i += 1
        elif c == "\x01":  # a heredoc taken out by _strip_heredocs
            e = s.find("\x01", i + 1)
            e = n if e < 0 else e
            flush()
            k = s[i + 1:e]
            body = heredocs[int(k)] if k.isdigit() and int(k) < len(heredocs) else ""
            toks.append(_tok(_HD, body)); i = e + 1
        elif c == "\\" and i + 1 < n:
            if s[i + 1] == "\n":
                i += 2
                continue
            cur = (cur or []) + [s[i + 1]]; i += 2
        elif c in "\n;":
            flush(); toks.append(";"); i += 1
        elif c == "#" and cur is None:
            while i < n and s[i] != "\n":
                i += 1
        elif c in "<>" or (c == "&" and s[i + 1:i + 2] == ">" and cur is None):
            # redirection: keep stdin sources, drop the rest
            if cur is not None and all(ch.isdigit() for ch in cur):
                cur, dyn = None, False
            flush()
            j = i
            while j < n and s[j] in "<>&|-":
                j += 1
            op = s[i:j]
            if op.startswith("<(") or (j < n and s[j] == "(" and op in ("<", ">")):
                k = _matching(s, j)
                subs.append(s[j + 1:k]); toks.append(_tok(_PS, s[j + 1:k])); i = k + 1
                continue
            while j < n and s[j] in " \t":
                j += 1
            k, q = j, None
            while k < n:
                ch = s[k]
                if q:
                    if ch == q:
                        q = None
                elif ch in "'\"":
                    q = ch
                elif ch in " \t\n;&|()<>":
                    break
                k += 1
            if op in ("<", "<<<"):
                tw = [t for t in _lex(s[j:k])[0] if isinstance(t, _W)]
                target = tw[0] if tw else _W("")
                toks.append(_tok(_RIN if op == "<" else _HD, target))
            i = k
        elif c in "&|":
            if c == "&" and cur and cur[-1] in "<>":
                cur.append(c); i += 1
                continue
            flush()
            j = i
            while j < n and s[j] in "&|":
                j += 1
            toks.append(s[i:j]); i = j
        elif c in "()":
            flush(); toks.append(c); i += 1
        elif c == "'":
            k = s.find("'", i + 1)
            k = n if k < 0 else k
            cur = (cur or []) + list(s[i + 1:k]); i = k + 1
        elif c == '"':
            cur = cur or []
            k = i + 1
            while k < n and s[k] != '"':
                if s[k] == "\\" and k + 1 < n:
                    cur.append(s[k + 1]); k += 2
                    continue
                if s[k] == "$" and k + 1 < n and s[k + 1] == "(":
                    e = _matching(s, k + 1)
                    subs.append(s[k + 2:e]); wsubs.append(s[k + 2:e])
                    cur += _subst(s[k + 2:e]); dyn = True; k = e + 1
                    continue
                if s[k] == "`":
                    e = s.find("`", k + 1)
                    e = n if e < 0 else e
                    subs.append(s[k + 1:e]); wsubs.append(s[k + 1:e])
                    cur += list("$(...)"); dyn = True; k = e + 1
                    continue
                if s[k] == "$":
                    dyn = True
                cur.append(s[k]); k += 1
            i = k + 1
        elif c == "$" and i + 1 < n and s[i + 1] == "(":
            e = _matching(s, i + 1)
            subs.append(s[i + 2:e]); wsubs.append(s[i + 2:e])
            cur = (cur or []) + _subst(s[i + 2:e]); dyn = True; i = e + 1
        elif c == "`":
            e = s.find("`", i + 1)
            e = n if e < 0 else e
            subs.append(s[i + 1:e]); wsubs.append(s[i + 1:e])
            cur = (cur or []) + list("$(...)"); dyn = True; i = e + 1
        else:
            if c == "$":
                dyn = True
            cur = (cur or []) + [c]; i += 1
    flush()
    return toks, subs


def _strip_heredocs(s):
    """Takes heredoc bodies out of the text and leaves a marker where the
    << was, so the body becomes the stdin of exactly that command."""
    bodies = []

    def repl(m):
        bodies.append(m.group(4))
        return "\x01%d\x01 %s\n" % (len(bodies) - 1, m.group(3))
    return _HEREDOC.sub(repl, s), bodies


def _abspath(p, cwd):
    """None when the path cannot be known (a variable, or relative to an unknown dir)."""
    if p is None or getattr(p, "dyn", False):
        return None
    p = _os.path.expanduser(p)
    if not _os.path.isabs(p) and cwd is None:
        return None
    return _os.path.normpath(p if _os.path.isabs(p) else _os.path.join(cwd or ".", p))


def _read_small(path):
    try:
        if _os.path.isfile(path) and _os.path.getsize(path) <= _MAX_FILE:
            with open(path, "rb") as fh:
                data = fh.read()
            if b"\0" not in data[:4096]:
                return data.decode("utf-8", "replace")
    except OSError:
        pass
    return None


def _git_config(cwd, key):
    try:
        r = _sp.run(["git", "-C", cwd or ".", "config", "--get", key],
                    capture_output=True, text=True, timeout=5)
        return r.stdout.strip() if r.returncode == 0 else None
    except Exception:
        return None


def _gh_alias(name):
    try:
        r = _sp.run(["gh", "alias", "list"], capture_output=True, text=True, timeout=5)
    except Exception:
        return None
    for line in r.stdout.splitlines():
        k, _, v = line.partition(":")
        if k.strip() == name:
            return v.strip().strip("'\"")
    return None


_CODE_PUSH = _re.compile(r"""\bgit\b[^\n]{0,120}?\bpush\b|['"]git['"]\s*,[^\n]{0,80}?['"]push['"]""")
_CODE_COMMIT = _re.compile(r"""\bgit\b[^\n]{0,120}?\bcommit\b|['"]git['"]\s*,[^\n]{0,80}?['"]commit['"]""")
_CODE_PR = _re.compile(r"""\bgh\b[^\n]{0,40}?\bpr\b[^\n]{0,20}?\b(create|new)\b|['"]gh['"]\s*,\s*['"]pr['"]\s*,\s*['"](create|new)['"]""")
_CODE_PULLS_POST = _re.compile(r"/pulls\b(?![/\w])")


class _Ctx:
    def __init__(self, cwd, env):
        self.cwd = cwd
        self.env = dict(env)
        self.events = []
        self.seen_files = set()
        self.strict = 0  # > 0 while reading text that eval / sh -c / <( ) run as code


_KEEP = object()


def _sub(ctx, fn, *args, cwd=_KEEP):
    """Run fn in a subshell: a cd or export inside does not leak out."""
    saved = (ctx.cwd, dict(ctx.env))
    if cwd is not _KEEP:
        ctx.cwd = cwd
    try:
        fn(*args)
    finally:
        ctx.cwd, ctx.env = saved


def analyze(cmd, cwd, depth=0, _ctx=None):
    """Events for a command line, in order."""
    ctx = _ctx or _Ctx(cwd, {})
    if depth > 6 or not cmd:
        return ctx.events
    text, bodies = _strip_heredocs(cmd)
    toks, _ = _lex(text, bodies)
    _run(toks, ctx, depth)
    return ctx.events


def _fresh(cwd):
    return {"start": cwd, "cond": False, "or": False, "pend": False}


def _run(toks, ctx, depth):
    """Walks the command list like a shell: ( ) is a subshell, a pipeline part
    is a subshell, and a cd that may not have run makes the folder unknown."""
    stack, words, extras, prev, up = [], [], [], None, None
    st = _fresh(ctx.cwd)
    for t in list(toks) + [";"]:
        if isinstance(t, _W):
            words.append(t)
            continue
        if isinstance(t, _Tok):
            extras.append(t)
            continue
        if words or extras:
            if st["pend"]:
                # "cd x || <this>": unless <this> leaves, it runs in the old folder
                if not (words and words[0] in ("exit", "return")):
                    ctx.cwd = None
                st["pend"] = False
            before = ctx.cwd
            _command(words, extras, ctx, depth, up if prev in ("|", "|&") else None)
            if ctx.cwd != before:
                if t in ("|", "|&") or prev in ("|", "|&") or prev == "||" or st["or"]:
                    ctx.cwd = None
                elif prev == "&&":
                    st["cond"] = True  # true for the rest of this && chain only
                elif t == "||":
                    st["pend"] = True
            up = (words, extras)
        words, extras = [], []
        if t in ("|", "|&"):
            prev = t
            continue
        up = None
        if t in ("&&", "||"):
            st["or"] = st["or"] or t == "||"
            prev = t
            continue
        # end of an and-or list: ; & newline ( )
        if t == "&":
            ctx.cwd = st["start"]  # the list ran in the background, in a subshell
        elif st["cond"] and ctx.cwd != st["start"]:
            ctx.cwd = None
        if t == "(":
            stack.append((ctx.cwd, dict(ctx.env)))
        elif t == ")" and stack:
            ctx.cwd, ctx.env = stack.pop()
        st = _fresh(ctx.cwd)
        prev = None


def _command(words, extras, ctx, depth, up):
    for w in words:  # $( ) and backticks run first, each in a subshell
        for body in getattr(w, "subs", ()):
            _sub(ctx, analyze, body, ctx.cwd, depth + 1, ctx)
    stdin, ps = None, []
    for x in extras:
        if isinstance(x, _PS):
            ps.append(x.body)
            _sub(ctx, analyze, x.body, ctx.cwd, depth + 1, ctx)
        elif isinstance(x, _HD):
            stdin = ("text", x.body)
        elif isinstance(x, _RIN):
            stdin = ("file", x.body)
    if stdin is None and up is not None:
        stdin = ("pipe", up)
    if words:
        _simple(words, ctx, depth, stdin, ps)


_EXEC = {"git", "gh", "curl", "eval", "source", ".", "cd", "pushd", "popd", "find", "awk", "gawk",
         "mawk", "nawk", "export"}
_RUNNERS = {"xargs", "parallel"}


_ASSIGN = _re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# wrapper -> (options that take a value, plain flags, positional words before the command)
_TIMEOUT = ({"-s", "--signal", "-k", "--kill-after"}, {"--foreground", "--preserve-status", "-v", "--verbose"}, 1)
_WRAP_OPTS = {
    "env": ({"-u", "--unset", "-C", "--chdir", "-S", "--split-string", "-P", "-a", "--argv0",
             "--default-signal", "--ignore-signal", "--block-signal"},
            {"-i", "-0", "-v", "--ignore-environment", "--null", "--debug", "-"}, 0),
    "sudo": ({"-u", "--user", "-g", "--group", "-D", "--chdir", "-C", "--close-from", "-h", "--host",
              "-p", "--prompt", "-r", "--role", "-t", "--type", "-U", "--other-user", "-T",
              "--command-timeout", "-R", "--chroot"},
             {"-A", "-b", "-E", "-H", "-k", "-K", "-n", "-P", "-S", "-B", "-N", "--preserve-env",
              "--non-interactive", "--set-home", "--askpass", "--stdin", "--reset-timestamp",
              "--remove-timestamp", "--background", "--bell", "--no-update"}, 0),
    "doas": ({"-u", "-C"}, {"-n", "-s", "-L"}, 0),
    "command": (set(), {"-p", "-v", "-V"}, 0),
    "exec": ({"-a"}, {"-c", "-l"}, 0),
    "nice": ({"-n", "--adjustment"}, set(), 0),
    "ionice": ({"-c", "--class", "-n", "--classdata", "-p", "--pid", "-P", "--pgid", "-u", "--uid"},
               {"-t", "--ignore"}, 0),
    "timeout": _TIMEOUT, "gtimeout": _TIMEOUT,
    "stdbuf": ({"-i", "-o", "-e", "--input", "--output", "--error"}, set(), 0),
    "caffeinate": ({"-t", "-w"}, {"-d", "-i", "-m", "-s", "-u"}, 0),
    "time": ({"-f", "--format", "-o", "--output"}, {"-p", "-v", "-a", "-l", "--verbose", "--portability",
                                                    "--append", "-q", "--quiet"}, 0),
    "flock": ({"-w", "--timeout", "--wait", "-E", "--conflict-exit-code"},
              {"-s", "-x", "-e", "-n", "-u", "-o", "-F", "--shared", "--exclusive", "--nonblock", "--nb",
               "--unlock", "--close", "--no-fork", "--verbose", "--fcntl"}, 1),
    "watch": ({"-n", "--interval", "-q", "--equexit"}, {"-b", "-c", "-d", "-e", "-g", "-p", "-r", "-t",
                                                        "-x", "-w", "--beep", "--color", "--differences",
                                                        "--errexit", "--chgexit", "--precise",
                                                        "--no-title", "--exec", "--no-wrap"}, 0),
    "script": ({"-F", "-t", "-T", "-I", "-O", "-B", "-E", "-m"}, {"-a", "-d", "-e", "-f", "-k", "-p", "-q",
                                                              "-r", "--append", "--flush", "--quiet",
                                                              "--return"}, 1),
    "xargs": ({"-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-d", "-E", "-a", "--arg-file",
               "--delimiter", "--max-args", "--max-procs", "--max-lines", "--replace", "--max-chars",
               "--eof", "--process-slot-var"},
              {"-0", "-o", "-p", "-r", "-t", "-x", "--null", "--open-tty", "--interactive",
               "--no-run-if-empty", "--verbose", "--exit"}, 0),
    "parallel": ({"-j", "--jobs", "-S", "--sshlogin", "-a", "--arg-file", "-I", "--colsep", "-d",
                  "--delimiter", "--joblog", "--tmpdir", "--results", "-N", "-n", "--max-args"},
                 {"-k", "--keep-order", "-q", "--quote", "-0", "--null", "-v", "--halt", "--bar",
                  "--progress", "--eta", "--will-cite", "--dry-run", "-X", "-m"}, 0),
}
_WRAP_CHDIR = {("env", "-C"), ("env", "--chdir"), ("sudo", "-D"), ("sudo", "--chdir")}
# options whose value is a whole command line
_WRAP_CODE = {("env", "-S"), ("env", "--split-string"), ("flock", "-c"), ("flock", "--command"),
              ("script", "-c"), ("script", "--command")}


def _wrapped(words):
    """-> (index of the word really executed, or None; info). Skips VAR=x and
    wrappers WITH their option values (env -C dir, sudo -u x, timeout -s KILL 5,
    flock file). info: "chdir" (folders the wrapper moves to), "code" (a command
    line given as an option value: env -S / flock -c / script -c, and the index
    of the words after it), "unsure" (an option the reader does not know: its
    value might have been taken for the command)."""
    info = {"chdir": [], "code": None, "unsure": False}
    i = 0
    while i < len(words) and _ASSIGN.match(words[i]):
        i += 1
    while i < len(words):
        name = _os.path.basename(words[i])
        if name not in _WRAPPERS:
            return i, info
        takes, flags, npos = _WRAP_OPTS.get(name, (set(), set(), 0))
        i += 1

        def options(i):
            while i < len(words):
                a = words[i]
                if a == "--":
                    return i + 1
                if name == "env" and _ASSIGN.match(a):
                    i += 1
                    continue
                if not a.startswith("-") or a in ("-", "!") and name != "env" or getattr(a, "dyn", False):
                    return i
                opt, val = a, None
                if a.startswith("--") and "=" in a:
                    opt, val = a.split("=", 1)
                elif not a.startswith("--") and len(a) > 2 and a[:2] in takes:
                    opt, val = a[:2], a[2:]
                if opt in takes or (name, opt) in _WRAP_CODE:
                    if val is None:
                        val = words[i + 1] if i + 1 < len(words) else None
                        i += 2
                    else:
                        i += 1
                    if (name, opt) in _WRAP_CHDIR:
                        info["chdir"].append(val)
                    if (name, opt) in _WRAP_CODE and val is not None:
                        info["code"] = (val, i)
                    continue
                if opt in flags or _re.match(r"^-\d+$", a) or (
                        not a.startswith("--") and all("-" + c in flags for c in a[1:])):
                    i += 1
                    continue
                info["unsure"] = True
                i += 1
            return i
        i = options(i)
        for _ in range(npos):
            if i < len(words) and info["code"] is None:
                i += 1
        i = options(i)  # flock file -c '...': options after the positional
        if info["code"] is not None:
            return None, info
    return None, info


def _effective(words):
    """Index of the word that is really executed (skips VAR=x and wrappers)."""
    return _wrapped(words)[0]


_RUNS = _re.compile(r"subprocess|os\.system|os\.exec|os\.spawn|popen|pty\.spawn|child_process|execSync|"
                    r"spawnSync|execFile|\bsystem\s*\(|\bexec\s*\(|`|Runtime\.getRuntime|IPC::|qx", _re.I)


def _scan_code(code, ctx, inline=False):
    n0 = len(ctx.events)
    if _CODE_PUSH.search(code):
        ctx.events.append(("push", {"dir": ctx.cwd, "git_dir": None, "work_tree": None,
                                    "refs": [], "flags": set(), "remote": None, "via": "code"}))
    if _CODE_COMMIT.search(code):
        ctx.events.append(("commit", {"dir": ctx.cwd, "git_dir": None, "work_tree": None,
                                      "all": True, "paths": [], "via": "code"}))
    if _CODE_PR.search(code) or (_CODE_PULLS_POST.search(code) and _re.search(r"\bPOST\b|\.post\(", code)):
        ctx.events.append(("pr", {"dir": ctx.cwd, "head": None, "repo": None, "via": "code"}))
    if inline and len(ctx.events) == n0 and _RUNS.search(code):
        # 'g'+'it', ['gi','t'].join(''): glue the pieces and look again
        glued = _re.sub(r"""['"`+,\s\[\]()]|\.join""", "", code)
        if _re.search(r"push|commit|send-?pack|prcreate|prnew", glued, _re.I):
            ctx.events.append(("hidden", {"why": "code that starts programs and names push/commit, "
                                                 "but not as a plain git command"}))


def _shell_args(args):
    """-> (code, script): the -c string (also in clusters -lc / -ec, after --),
    or the script file; (None, None) when the shell reads stdin."""
    i, has_c, stdin = 0, False, False
    while i < len(args):
        a = args[i]
        if a == "--":
            i += 1
            break
        if a == "-":
            stdin = True
            i += 1
            break
        if a in ("-o", "+o", "-O", "+O", "--rcfile", "--init-file"):
            i += 2
            continue
        if a.startswith("--"):
            i += 1
            continue
        if len(a) > 1 and a[0] in "-+":
            if a[0] == "-" and "c" in a[1:]:
                has_c = True
            if a[0] == "-" and "s" in a[1:]:
                stdin = True
            i += 1
            continue
        break
    rest = args[i:]
    if has_c:
        return (rest[0] if rest else _W("")), None
    if stdin:
        return None, None
    return None, (rest[0] if rest else None)


def _producer(words, extras, ctx):
    """What the left side of a pipe prints, when it can be told (cat/echo/printf)."""
    exe = _effective(words)
    if exe is None:
        return None
    name, args = _os.path.basename(words[exe]), words[exe + 1:]
    if name == "cat":
        for x in extras:
            if isinstance(x, _HD):
                return x.body
            if isinstance(x, _RIN):
                p = _abspath(x.body, ctx.cwd)
                return _read_small(p) if p else None
        texts = []
        for a in args:
            if a.startswith("-"):
                continue
            p = _abspath(a, ctx.cwd)
            t = _read_small(p) if p else None
            if t:
                texts.append(t)
        return "\n".join(texts) or None
    if name in ("echo", "printf"):
        a = [x for x in args if not (name == "echo" and x in ("-n", "-e", "-E", "-ne", "-en"))]
        return " ".join(a).replace("\\n", "\n")
    return None


def _stdin(stdin, ctx, depth, kind):
    """A shell or interpreter that reads its program from stdin."""
    if stdin is None:
        return
    how, what = stdin
    upline, code = "", None
    if how == "text":
        code = what
    elif how == "file":
        p = _abspath(what, ctx.cwd)
        if kind == "shell":
            _sub(ctx, _script, p, ctx, depth, True)
            return
        code = _read_small(p) if p else None
    else:
        uw, ux = what
        upline = " ".join(uw)
        code = _producer(uw, ux, ctx)
    n0 = len(ctx.events)
    if code is not None:
        if kind == "shell":
            _sub(ctx, analyze, code, ctx.cwd, depth + 1, ctx)
        else:
            _scan_code(code, ctx, inline=True)
    if how == "pipe" and not any(k in ("push", "commit", "pr", "hidden") for k, _ in ctx.events[n0:]):
        if _re.search(r"\b(git|gh|push|commit|send-pack)\b", upline + "\n" + (code or "")):
            ctx.events.append(("hidden", {"why": f"a {kind} reads its commands from a pipe "
                                                 f"({' '.join(uw[:3])} | ...)"}))


_TELL = _re.compile(r"^(push|commit|pr|create|new|send-pack)$")


_VAR = _re.compile(r"\$(?:\{([A-Za-z_][A-Za-z0-9_]*)\}|([A-Za-z_][A-Za-z0-9_]*))")


def _expand(w, ctx):
    """$NAME / ${NAME} from earlier assignments in the same command line (or the
    hook's own environment). What cannot be resolved stays dynamic."""
    if not w.dyn or "$(...)" in w:
        return w
    if "$(git rev-parse --show-toplevel)" in w:
        try:
            top = _sp.run(["git", "-C", ctx.cwd or ".", "rev-parse", "--show-toplevel"],
                          capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            top = ""
        if top:
            w = _W(w.replace("$(git rev-parse --show-toplevel)", top))
    w = _W(w.replace("$(pwd -P)", ctx.cwd or "").replace("$(pwd)", ctx.cwd or ""))

    def rep(m):
        k = m.group(1) or m.group(2)
        v = ctx.env.get(k, _os.environ.get(k))
        return m.group(0) if v is None else v
    out = _VAR.sub(rep, w)
    nw = _W(out)
    nw.dyn = "$" in out
    return nw


_MENTION = _re.compile(r"\b(git|gh|push|commit|send-pack)\b")


def _src_key(name):
    return "\0src:" + name  # never a valid variable name, so _expand never reads it


def _sources(w, ctx):
    """The command text behind the dynamic parts of a word: its $( ) / backtick
    bodies and what earlier assignments of the variables it uses came from."""
    out = list(getattr(w, "subs", ()))
    for m in _VAR.finditer(w):
        out += ctx.env.get(_src_key(m.group(1) or m.group(2)), [])
    return out


def _sub_output(body, ctx):
    """What a $( ) body prints, when the reader can tell (echo / printf / cat
    of a readable file); None otherwise."""
    text, bodies = _strip_heredocs(body)
    toks, _ = _lex(text, bodies)
    while toks and toks[-1] == ";":
        toks = toks[:-1]
    if not toks or any(not isinstance(t, (_W, _Tok)) for t in toks):
        return None
    words = [t for t in toks if isinstance(t, _W)]
    extras = [t for t in toks if not isinstance(t, _W)]
    if any(w.subs for w in words):
        return None
    words = [_expand(w, ctx) for w in words]
    if any(w.dyn for w in words):
        return None
    out = _producer(words, extras, ctx)
    return None if out is None else out.rstrip("\n")


def _resolve(w, ctx):
    """A word with its $( ) and $VAR filled in, or None when a part is unknown."""
    if not w.dyn:
        return str(w)
    text = str(w)
    for body in w.subs:
        if " ".join(body.split()) in _KNOWN_SUBS:
            continue
        out = _sub_output(body, ctx)
        if out is None or "$(...)" not in text:
            return None
        text = text.replace("$(...)", out, 1)
    nw = _W(text)
    nw.dyn = True
    r = _expand(nw, ctx)
    return None if r.dyn else str(r)


def _hide(ctx, why):
    ctx.events.append(("hidden", {"why": why}))


def _run_text(ws, ctx, depth, what, sub):
    """eval / sh -c / env -S / flock -c: words that are run as shell code.
    Text built at run time is filled in when the reader can compute it;
    otherwise it is blocked when it names git/gh/push/commit (or is a bare
    unknown variable, which could be anything)."""
    parts, srcs, dyn = [], [], False
    for w in ws:
        dyn = dyn or w.dyn
        r = _resolve(w, ctx)
        if r is None:
            srcs += _sources(w, ctx)
            parts.append(str(_expand(w, ctx)))
        else:
            parts.append(r)
    text = " ".join(parts)
    if any(_MENTION.search(x) for x in srcs):
        _hide(ctx, f"{what} runs text built at run time from a command that names git ({srcs[0][:40]})")
    n0 = len(ctx.events)
    ctx.strict += 1
    try:
        if sub:
            _sub(ctx, analyze, text, ctx.cwd, depth + 1, ctx)
        else:
            analyze(text, ctx.cwd, depth + 1, ctx)
    finally:
        ctx.strict -= 1
    if dyn and _MENTION.search(text) and \
            not any(k in ("push", "commit", "pr", "hidden") for k, _ in ctx.events[n0:]):
        _hide(ctx, f"{what} runs text built at run time that names git ({text[:40]})")


def _ps_code(bodies, ctx, depth, kind, sub):
    """bash <(...) / source <(...): the shell reads a program a command prints."""
    for body in bodies:
        out = _sub_output(body, ctx)
        if out is None:
            if _MENTION.search(body):
                _hide(ctx, f"a {kind} runs what <({body[:30]}) prints - the gate cannot see it")
            continue
        n0 = len(ctx.events)
        if kind == "shell":
            if sub:
                _sub(ctx, analyze, out, ctx.cwd, depth + 1, ctx)
            else:
                analyze(out, ctx.cwd, depth + 1, ctx)
        else:
            _scan_code(out, ctx, inline=True)
        if _MENTION.search(out) and \
                not any(k in ("push", "commit", "pr", "hidden") for k, _ in ctx.events[n0:]):
            _hide(ctx, f"a {kind} runs what <({body[:30]}) prints and it names git")


def _assign(ctx, orig, value):
    """NAME=value: remember the value - filled in when the reader can compute a
    $( ) in it - and, when it cannot, which command text it came from."""
    m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=", orig)
    name = m.group(1)
    if getattr(orig, "dyn", False):
        val = _W(orig[m.end():])
        val.dyn, val.subs = True, orig.subs
        r = _resolve(val, ctx)
        if r is not None:
            ctx.env[name] = r
            ctx.env.pop(_src_key(name), None)
            return
        ctx.env[_src_key(name)] = _sources(val, ctx)
    else:
        ctx.env.pop(_src_key(name), None)
    ctx.env[name] = value


def _simple(words, ctx, depth, stdin=None, ps=()):
    orig = list(words)
    words = [_expand(w, ctx) for w in words]
    for o, w in zip(orig, words):
        m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", w)
        if not m:
            break
        _assign(ctx, o, m.group(2))
    for w in words:
        if _re.match(r"^MC_LEAK_SCAN_SKIP=", w):
            ctx.events.append(("bypass", {"why": "MC_LEAK_SCAN_SKIP switches off the pre-push leak scan"}))
    exe, info = _wrapped(words)
    if info["unsure"] and any(_TELL.match(a) for a in words):
        _hide(ctx, f"a wrapper option the gate does not know ({' '.join(words[:3])} ...) comes before "
                   f"push/commit - its value may be taken for the command")
    saved = ctx.cwd
    if info["chdir"]:  # env -C dir / sudo -D dir: the command runs in dir
        for d in info["chdir"]:
            dw = d if isinstance(d, _W) else _W(d or "")
            if not isinstance(d, _W) and "$" in dw:
                dw.dyn = True
            ctx.cwd = _abspath(dw, ctx.cwd) if d else None
    try:
        if info["code"] is not None:
            val, j = info["code"]
            vw = val if isinstance(val, _W) else _W(val)
            if not isinstance(val, _W) and "$" in vw:
                vw.dyn = True
            rest = [orig[k] for k in range(j, len(orig))]
            _run_text([vw] + rest, ctx, depth, f"{_os.path.basename(words[0])} with a command option", True)
            return
        if exe is None:
            return
        _exec(orig, words, exe, ctx, depth, stdin, ps)
    finally:
        if info["chdir"]:
            ctx.cwd = saved


def _exec(orig, words, exe, ctx, depth, stdin, ps):
    head, args = words[exe], words[exe + 1:]
    name = _os.path.basename(head)
    if head.dyn or (orig[exe].dyn and _re.search(r"\s", head)):
        r = _resolve(orig[exe], ctx)
        if r is not None:  # $c / $(echo git push): word splitting gives a new command line
            analyze(" ".join([r] + [str(a) for a in args]), ctx.cwd, depth + 1, ctx)
            return
        srcs = _sources(orig[exe], ctx)
        if any(_TELL.match(a) for a in args):
            _hide(ctx, f"command behind a variable ({head}) with '{' '.join(args[:3])}'")
        elif any(_MENTION.search(x) for x in srcs):
            _hide(ctx, f"command built at run time from a command that names git ({srcs[0][:40]})")
        elif ctx.strict and not srcs:
            _hide(ctx, f"code run by eval / sh -c is an unknown variable ({head})")
        return
    runner = next((_os.path.basename(w) for w in words[:exe] if _os.path.basename(w) in _RUNNERS), None)
    if runner and name in ("git", "gh"):
        verbs = _GIT_VERBS if name == "git" else _GH_TOP
        verb = next((a for a in args if a in verbs), None)
        if verb is None or verb in ("push", "commit", "pr", "send-pack", "api") or any("{}" in a for a in args):
            ctx.events.append(("hidden", {"why": f"{name} run by {runner}: its arguments come from input "
                                                 f"the gate cannot see"}))
    if name in ("cd", "pushd", "popd"):
        pos = [a for a in args if a == "-" or not a.startswith("-")]
        if name == "popd" or (name == "pushd" and not pos) or (pos and (pos[0] == "-" or pos[0].startswith("+"))):
            ctx.cwd = None  # a folder from the directory stack: unknown here
        else:
            ctx.cwd = _abspath(pos[0] if pos else "~", ctx.cwd)
        return
    if name == "export":
        for o, w in zip(orig[exe + 1:], args):
            m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", w)
            if m:
                _assign(ctx, o, m.group(2))
                if m.group(1) == "MC_LEAK_SCAN_SKIP":
                    ctx.events.append(("bypass", {"why": "MC_LEAK_SCAN_SKIP switches off the pre-push leak scan"}))
        return
    if name == "git":
        _git(args, ctx, depth)
    elif name == "gh":
        _gh(args, ctx, depth)
    elif name == "curl":
        joined = " ".join(args)
        if _re.search(r"api\.github\.com/repos/[^\s]+/pulls/?(\s|$|\?)", joined + " ") and \
                _re.search(r"(^|\s)(-X\s*POST|--request\s+POST|-d|--data\S*|--json|-F|--form)(\s|=|$)", joined):
            ctx.events.append(("pr", {"dir": ctx.cwd, "head": None, "repo": None, "via": "curl"}))
    elif name == "eval":
        _run_text(orig[exe + 1:], ctx, depth, "eval", False)
    elif name in _SHELLS:
        code, script = _shell_args(args)
        if code is not None:
            ocode = _shell_args(orig[exe + 1:])[0]
            if "$(...)" in code and _resolve(ocode, ctx) is None:
                _hide(ctx, f"{name} -c with a command the gate cannot compute ({code[:40]})")
            elif code.dyn and _re.search(r"\$\{?[A-Za-z_]", code) and \
                    _re.search(r"\b(push|commit|send-pack)\b|\bpr\s+(create|new)\b", code):
                _hide(ctx, f"{name} -c with a command behind a variable ({code[:40]})")
            _run_text([ocode], ctx, depth, f"{name} -c", True)
        elif script:
            _sub(ctx, _script, _abspath(script, ctx.cwd), ctx, depth)
        elif ps and stdin is None:
            _ps_code(ps, ctx, depth, "shell", True)
        else:
            _stdin(stdin, ctx, depth, "shell")
    elif name in ("source", "."):
        if args:
            _script(_abspath(args[0], ctx.cwd), ctx, depth)
        elif ps:
            _ps_code(ps, ctx, depth, "shell", False)
    elif _INTERP.match(name):
        for flag in ("-c", "-e", "--eval", "-p"):
            if flag in args:
                i = args.index(flag)
                if i + 1 < len(args):
                    _scan_code(args[i + 1], ctx, inline=True)
                return
        script = next((a for a in args if not a.startswith("-")), None)
        if "-m" in args:
            return
        if script:
            p = _abspath(script, ctx.cwd)
            if p and p not in ctx.seen_files:
                ctx.seen_files.add(p)
                code = _read_small(p)
                if code:
                    _scan_code(code, ctx)
        elif ps and stdin is None:
            _ps_code(ps, ctx, depth, "interp", True)
        else:
            _stdin(stdin, ctx, depth, "interp")
    elif name == "find":
        i = 0
        while i < len(args):
            if args[i] in ("-exec", "-execdir", "-ok", "-okdir"):
                j, sub = i + 1, []
                while j < len(args) and args[j] not in (";", "+"):
                    sub.append(args[j])
                    j += 1
                if sub:  # -execdir runs in each file's folder: unknown
                    _sub(ctx, _simple, sub, ctx, depth + 1, None,
                         cwd=ctx.cwd if args[i] in ("-exec", "-ok") else None)
                i = j
            i += 1
    elif name in ("awk", "gawk", "mawk", "nawk"):
        code = " ".join(args)
        for j, a in enumerate(args):
            if a == "-f" and j + 1 < len(args):
                p = _abspath(args[j + 1], ctx.cwd)
                code += "\n" + ((_read_small(p) or "") if p else "")
        _scan_code(code, ctx, inline=True)
    elif "/" in head:
        _sub(ctx, _script, _abspath(head, ctx.cwd), ctx, depth)


def _script(path, ctx, depth, force_shell=False):
    if not path or path in ctx.seen_files:
        return
    ctx.seen_files.add(path)
    code = _read_small(path)
    if code is None:
        return
    first = code.split("\n", 1)[0]
    if not force_shell and first.startswith("#!") and _re.search(r"python|node|perl|ruby", first):
        _scan_code(code, ctx)
    else:
        analyze(code, ctx.cwd, depth + 1, ctx)


def _git(args, ctx, depth, cfg=None):
    cfg = dict(cfg or {})
    gdir, wtree = ctx.env.get("GIT_DIR"), ctx.env.get("GIT_WORK_TREE")
    cwd, explicit = ctx.cwd, False
    for k, v in ctx.env.items():
        if (k.startswith("GIT_CONFIG") and "hookspath" in v.lower()) or \
                (k == "GIT_CONFIG_PARAMETERS" and "hookspath" in v.lower()):
            ctx.events.append(("bypass", {"why": f"{k} overrides core.hooksPath"}))
    i = 0
    while i < len(args):
        a = args[i]
        if not a.startswith("-"):
            break
        key, _, val = a.partition("=")
        if key in _GIT_OPT_ARG and not val and a == key:
            val = args[i + 1] if i + 1 < len(args) else ""
            i += 1
        if key == "-C" and val:
            cwd = _abspath(val, cwd)
            explicit = True
        elif key == "-c":
            k2, _, v2 = val.partition("=")
            cfg[k2.lower()] = v2
        elif key == "--git-dir":
            gdir = val
        elif key == "--work-tree":
            wtree = val
        i += 1
    if i >= len(args):
        return
    verb, rest = args[i], args[i + 1:]
    loc = {"dir": cwd, "git_dir": _abspath(gdir, cwd) if gdir else None,
           "work_tree": _abspath(wtree, cwd) if wtree else None, "explicit": explicit or bool(gdir)}
    if verb.dyn:
        ctx.events.append(("hidden", {"why": f"git verb behind a variable ({verb})"}))
        return
    if "core.hookspath" in cfg and verb in ("push", "commit", "merge", "rebase", "am", "cherry-pick"):
        ctx.events.append(("bypass", {"why": "git -c core.hooksPath=... skips the git hooks"}))
    if verb not in _GIT_VERBS:
        alias = cfg.get("alias." + verb.lower()) or _git_config(cwd, "alias." + verb)
        if alias and depth < 5:
            if alias.startswith("!"):
                analyze(alias[1:] + " " + " ".join(_shq(r) for r in rest), cwd, depth + 1, ctx)
            else:
                toks, _ = _lex(alias)
                more = [t for t in toks if isinstance(t, _W)]
                sub = _Ctx(cwd, ctx.env)
                sub.events = ctx.events
                sub.seen_files = ctx.seen_files
                _git(list(args[:i]) + more + list(rest), sub, depth + 1, cfg)
        return
    if verb == "push":
        flags, pos, j = set(), [], 0
        while j < len(rest):
            a = rest[j]
            if a in ("--repo", "-o", "--push-option", "--receive-pack", "--exec"):
                j += 2
                continue
            if a.startswith("--"):
                flags.add(a.split("=", 1)[0])
            elif a.startswith("-") and len(a) > 1:
                flags.update("-" + ch for ch in a[1:])
            else:
                pos.append(a)
            j += 1
        if "--no-verify" in flags:
            ctx.events.append(("bypass", {"why": "git push --no-verify skips the pre-push hook"}))
        ctx.events.append(("push", dict(loc, refs=pos[1:], remote=pos[0] if pos else None,
                                        flags=flags, via="git")))
    elif verb == "commit":
        all_, nov, paths, j = False, False, [], 0
        while j < len(rest):
            a = rest[j]
            if a in ("-m", "-F", "-c", "-C", "--message", "--file", "--author", "--date",
                     "--reuse-message", "--reedit-message", "--fixup", "--squash", "-t", "--template",
                     "--trailer", "--cleanup", "-S", "--gpg-sign", "--pathspec-from-file"):
                j += 2
                continue
            if a == "--":
                paths += list(rest[j + 1:])  # pathspecs follow
                break
            if a == "--all":
                all_ = True
            elif a == "--no-verify":
                nov = True
            elif a.startswith("--"):
                pass
            elif not a.startswith("-") or a == "-":
                paths.append(a)  # git commit <path>: commits the file as it is NOW, staged or not
            elif len(a) > 1:
                cluster = a[1:]
                for idx, ch in enumerate(cluster):
                    if ch == "a":
                        all_ = True
                    if ch == "n":
                        nov = True
                    if ch in "mFcCt":  # value follows (rest of cluster or next word)
                        if idx == len(cluster) - 1:
                            j += 1
                        break
            j += 1
        if nov:
            ctx.events.append(("bypass", {"why": "git commit --no-verify / -n skips the commit hooks"}))
        ctx.events.append(("commit", dict(loc, all=all_, paths=paths, via="git")))
    elif verb == "add":
        ctx.events.append(("add", dict(loc, via="git")))
    elif verb == "config":
        low = [r.lower() for r in rest]
        if "core.hookspath" in low:
            reading = any(x in low for x in ("--get", "--get-all", "--get-regexp", "get", "-l", "--list",
                                              "--show-origin", "--show-scope"))
            pos = [r for r in low if not r.startswith("-")]
            writes = any(x in low for x in ("--unset", "--unset-all", "--replace-all", "--add", "set", "unset"))
            if writes or (not reading and len(pos) >= 2):
                ctx.events.append(("bypass", {"why": "changing core.hooksPath switches the git hooks off"}))
    elif verb == "send-pack":
        ctx.events.append(("push", dict(loc, refs=[], remote=None, flags=set(), via="send-pack")))


def _shq(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _gh(args, ctx, depth):
    i = 0
    while i < len(args) and args[i].startswith("-"):
        i += 2 if args[i] in ("-R", "--repo") else 1
    if i >= len(args):
        return
    sub, rest = args[i], args[i + 1:]
    if sub.dyn:
        ctx.events.append(("hidden", {"why": f"gh subcommand behind a variable ({sub})"}))
        return
    if sub not in _GH_TOP and depth < 5:
        alias = _gh_alias(sub)
        if alias:
            if alias.startswith("!"):
                analyze(alias[1:] + " " + " ".join(_shq(r) for r in rest), ctx.cwd, depth + 1, ctx)
            else:
                toks, _ = _lex(alias)
                _gh([t for t in toks if isinstance(t, _W)] + list(rest), ctx, depth + 1)
        return
    if sub == "pr" and rest and rest[0] in ("create", "new"):
        head = repo = None
        r = rest[1:]
        for j, a in enumerate(r):
            nxt = r[j + 1] if j + 1 < len(r) else None
            if a in ("-H", "--head"):
                head = nxt
            elif a.startswith("--head="):
                head = a.split("=", 1)[1]
            elif a in ("-R", "--repo"):
                repo = nxt
            elif a.startswith("--repo="):
                repo = a.split("=", 1)[1]
        for j, a in enumerate(args[:i]):
            if a in ("-R", "--repo") and j + 1 < len(args):
                repo = args[j + 1]
        if head and ":" in head:
            head = head.split(":", 1)[1]
        ctx.events.append(("pr", {"dir": ctx.cwd, "head": head, "repo": repo, "via": "gh"}))
    elif sub == "api":
        joined = " ".join(rest)
        path = next((a for a in rest if not a.startswith("-") and "pulls" in a), "")
        if _re.search(r"repos/[^/\s]+/[^/\s]+/pulls/?$", path):
            method = None
            for j, a in enumerate(rest):
                if a in ("-X", "--method") and j + 1 < len(rest):
                    method = rest[j + 1].upper()
                elif a.startswith("--method="):
                    method = a.split("=", 1)[1].upper()
            has_body = _re.search(r"(^|\s)(-f|-F|--field|--raw-field|--input)(\s|=|$)", joined)
            if method == "POST" or (method is None and has_body):
                head = None
                for j, a in enumerate(rest):
                    if a in ("-f", "-F", "--field", "--raw-field") and j + 1 < len(rest) \
                            and rest[j + 1].startswith("head="):
                        head = rest[j + 1].split("=", 1)[1].split(":")[-1]
                ctx.events.append(("pr", {"dir": ctx.cwd, "head": head, "repo": None, "via": "gh api"}))
# <<< cmdparse

# ---------------------------------------------------------------- test-gate
import json
import shutil
import sys
import tempfile
import time

LIMIT = int(_os.environ.get("MC_TEST_GATE_TIMEOUT", "240") or 240)
BUDGET = int(_os.environ.get("MC_TEST_GATE_BUDGET", "270") or 270)
MAX_FILES = int(_os.environ.get("MC_TEST_GATE_MAX_FILES", "80") or 80)
STARTED = time.time()
ANSI = _re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def say(*lines):
    for line in lines:
        print(line, file=sys.stderr)


def git(top, *args, ok=False):
    r = _sp.run(["git", "-C", top, *args], capture_output=True, text=True)
    if ok:
        return r
    return r.stdout if r.returncode == 0 else ""


def toplevel(d):
    if not d or not _os.path.isdir(d):
        return None
    r = _sp.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def toplevel_from_gitdir(gdir, wtree, cwd):
    args = ["git", "--git-dir", gdir] + (["--work-tree", wtree] if wtree else [])
    r = _sp.run(args + ["rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=cwd or None)
    if r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    # a linked worktree's git dir names its checkout in the file "gitdir"
    try:
        with open(_os.path.join(gdir, "gitdir")) as fh:
            return _os.path.dirname(fh.read().strip())
    except OSError:
        return None


def current_branch(top):
    return git(top, "symbolic-ref", "-q", "--short", "HEAD").strip()


def worktree_for(top, branch):
    """Path of the worktree (of the same repository) that has <branch> checked out."""
    path = None
    for line in git(top, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line == "branch refs/heads/" + branch and path and _os.path.isdir(path):
            return path
    return None


def worktree_at(top, sha):
    path = None
    for line in git(top, "worktree", "list", "--porcelain").splitlines():
        if line.startswith("worktree "):
            path = line[9:]
        elif line == "HEAD " + sha and path and _os.path.isdir(path):
            return path
    return None


def origin_slug(top):
    url = git(top, "remote", "get-url", "origin").strip()
    m = _re.search(r"[:/]([^/:]+/[^/]+?)(?:\.git)?/?$", url)
    return m.group(1).lower() if m else ""


def candidate_repos(start_top):
    seen = []
    for d in (start_top, _os.environ.get("MC_HOOK_REPO"), _os.environ.get("CLAUDE_PROJECT_DIR")):
        t = toplevel(d) if d else None
        if t and t not in seen:
            seen.append(t)
    return seen


def resolve_branch(top, branch, what):
    """-> (checkout, None) or (None, why-blocked)."""
    if not branch or branch in ("HEAD", "@"):
        return top, None
    branch = _re.sub(r"^refs/heads/", "", branch)
    if branch == current_branch(top):
        return top, None
    wt = worktree_for(top, branch)
    if wt:
        return wt, None
    if git(top, "show-ref", "--verify", "-q", "refs/tags/" + branch, ok=True).returncode == 0:
        return "", None  # a tag: nothing to test
    sha = git(top, "rev-parse", "-q", "--verify", branch + "^{commit}").strip()
    if sha:
        wt = worktree_at(top, sha)
        if wt:
            return wt, None
    return None, (f"{what}: '{branch}' is not checked out in any worktree of {top}, so its tests "
                  f"cannot run. Run it from the branch's own worktree (git -C <worktree> ...).")


def _not_a_checkout(what, e, events_cwd):
    """The command seems to run outside any checkout. -> (blocks, notes).
    The reader's picture of the folder can be wrong (a cd it could not follow
    exactly, a cd that fails). If the session itself sits in a checkout, the
    real command may well run there: fail closed."""
    hook_top = toplevel(events_cwd)
    if hook_top and not e.get("explicit"):
        return [f"{what}: the command seems to run in {e.get('dir')}, which is not a git checkout, but the "
                f"session is in {hook_top}. The gate cannot be sure which folder git will use. Write the path "
                f"literally: git -C <worktree> ..."], []
    return [], [f"{what}: {e.get('dir')} is not a git checkout - not gated (git refuses there too)"]


def resolve(kind, e, events_cwd):
    """-> list of (checkout) to test, list of blocking reasons, list of notes."""
    via = e.get("via", "")
    if kind == "push":
        if e.get("git_dir"):
            top = toplevel_from_gitdir(e["git_dir"], e.get("work_tree"), e.get("dir"))
        elif e.get("dir") is None:
            return [], [f"push ({via}): the checkout is behind a variable or an unknown 'cd' - the gate "
                        f"cannot tell which tests to run. Write the path literally: git -C <worktree> push"], []
        else:
            top = toplevel(e["dir"])
        if not top:
            return [], *_not_a_checkout("push", e, events_cwd)
        flags = e.get("flags") or set()
        if flags & {"--delete", "-d"}:
            return [], [], []
        if flags & {"--all", "--mirror", "--branches"}:
            return [], ["push --all/--mirror/--branches: push one branch at a time, so the gate can test "
                        "each branch in its own worktree"], []
        refs = e.get("refs") or []
        if not refs and flags & {"--tags"}:
            return [], [], []
        if not refs:
            return [top], [], []
        out, why = [], []
        for spec in refs:
            src = spec.lstrip("+").split(":", 1)[0]
            if spec.lstrip("+").startswith(":"):
                continue  # ':branch' deletes
            if src.startswith("refs/tags/"):
                continue
            wt, err = resolve_branch(top, src, "push")
            if err:
                why.append(err)
            elif wt:
                out.append(wt)
        return out, why, []
    # pr creation (gh pr create/new, gh api .../pulls, curl .../pulls)
    if e.get("dir") is None:
        return [], [f"PR creation ({via}): the directory is behind a variable or an unknown 'cd' - "
                    f"run it from the branch's worktree with a literal path"], []
    top = toplevel(e["dir"])
    repos = candidate_repos(top)
    slug = (e.get("repo") or "").lower()
    if slug:
        slug = _re.sub(r"^(https?://)?(www\.)?github\.com/", "", slug).rstrip("/")
        if "/" not in slug:
            slug = ""
    pick = None
    for r in repos:
        if not slug or origin_slug(r) == slug:
            pick = r
            break
    if pick is None:
        if slug or e.get("head"):
            return [], [f"PR creation ({via}) for {slug or 'branch ' + str(e.get('head'))}: no local checkout "
                        f"of that repository found from {e.get('dir')}. Run it inside the branch's worktree "
                        f"(cd <worktree> && gh pr create ...)."], []
        return [], *_not_a_checkout("PR creation", e, events_cwd)
    head = e.get("head")
    if head is None and via in ("curl", "gh api", "code") and pick != top:
        return [], [f"PR creation ({via}) without a head branch the gate can read - run gh pr create "
                    f"--head <branch> from the branch's worktree"], []
    wt, err = resolve_branch(pick, head, "PR creation")
    return ([wt] if wt else []), ([err] if err else []), []


# ------------------------------------------------------------ test choice
STOP_NAMES = {"__init__.py", "conftest.py", "README.md", "package.json", "package-lock.json",
              "index.ts", "index.tsx", "utils.py", "config.py", "main.py", "models.py", "setup.py",
              "Dockerfile", "types.ts", "utils.ts", "constants.ts", "requirements.txt", "AGENTS.md",
              "CLAUDE.md", "Makefile", "pyproject.toml", "tsconfig.json"}


def is_test(path):
    b = _os.path.basename(path)
    return bool(_re.match(r"test_.*\.(py|sh)$", b) or _re.search(r"\.test\.(ts|tsx|js|jsx)$", b))


def read(top, rel, cache):
    if rel not in cache:
        try:
            with open(_os.path.join(top, rel), encoding="utf-8", errors="replace") as fh:
                cache[rel] = fh.read()
        except OSError:
            cache[rel] = ""
    return cache[rel]


def module_of(rel):
    """backend/app/a/b.py -> app.a.b ; backend/app/a/__init__.py -> app.a"""
    m = rel[len("backend/"):-3].replace("/", ".")
    return m[:-len(".__init__")] if m.endswith(".__init__") else m


def refs_module(text, mod):
    if _re.search(r"(?<![\w.])" + _re.escape(mod) + r"(?![\w])", text):
        return True
    parent, _, leaf = mod.rpartition(".")
    if parent:
        for m in _re.finditer(r"from\s+" + _re.escape(parent) + r"\s+import\s+(\([^)]*\)|[^\n]*)", text):
            if _re.search(r"\b" + _re.escape(leaf) + r"\b", m.group(1)):
                return True
    return False


def select_tests(top, changed):
    """-> dict suite -> ordered list of test files, notes. Suites:
    'backend' (cwd backend/), 'py:<dir>' (cwd <dir>), 'vitest', 'sh'."""
    tracked = git(top, "ls-files").splitlines()
    tests = [t for t in tracked if is_test(t)]
    cache, notes = {}, []
    suites = {}

    def add(suite, f, rank):
        suites.setdefault(suite, {})
        if f not in suites[suite] or suites[suite][f] > rank:
            suites[suite][f] = rank

    def suite_of(t):
        if t.startswith("backend/tests/") and t.endswith(".py"):
            return "backend"
        if t.startswith("frontend-v2/"):
            return "vitest"
        if t.endswith(".sh"):
            return "sh" if _re.match(r"scripts/tests/test_[^/]*\.sh$", t) else None
        d = _os.path.dirname(t)
        a = d[:-len("/tests")] if d.endswith("/tests") else d
        a = _re.sub(r"/tests/.*$", "", a)
        return "py:" + a

    be_tests = [t for t in tests if t.startswith("backend/tests/") and t.endswith(".py")]
    fe_changed, fe_full = [], False
    for f in changed:
        b = _os.path.basename(f)
        stem = b.rsplit(".", 1)[0]
        if is_test(f):
            s = suite_of(f)
            if s:
                add(s, f, 0)
            continue
        if f.startswith("frontend-v2/"):
            if f.startswith("frontend-v2/src/") and _re.search(r"\.(ts|tsx|js|jsx|json|css)$", f):
                fe_changed.append(f)
                if b.startswith("test-setup"):
                    fe_full = True
            elif _re.match(r"frontend-v2/(vitest|vite)\.config\.|frontend-v2/package\.json$", f):
                fe_full = True
            continue
        if f.startswith("backend/app/") and f.endswith(".py"):
            mod = module_of(f)
            for t in be_tests:
                tb = _os.path.basename(t)
                if stem not in ("__init__",) and _re.match(r"test_" + _re.escape(stem) + r"(_.*)?\.py$", tb):
                    add("backend", t, 1)
                elif refs_module(read(top, t, cache), mod):
                    add("backend", t, 2)
            continue
        if f.startswith("backend/tests/"):
            if b == "conftest.py":
                for t in be_tests:
                    if t.startswith(_os.path.dirname(f) + "/"):
                        add("backend", t, 3)
                notes.append(f"{f} changed: every test below it is affected")
            elif f.endswith(".py"):
                for t in be_tests:
                    if _re.search(r"\b" + _re.escape(stem) + r"\b", read(top, t, cache)):
                        add("backend", t, 1)
            continue
        # anything else: the component's own tests that name or import it,
        # plus backend tests that name the file (scripts/x.py, config json, ...)
        d = _os.path.dirname(f)
        anc = None
        while True:
            if any(t.startswith((d + "/tests/") if d else "tests/") or
                   (_os.path.dirname(t) == d and is_test(t)) for t in tests):
                anc = d
                break
            if not d:
                break
            d = _os.path.dirname(d)
        distinctive = b not in STOP_NAMES and (len(b) >= 8 or "-" in b or "_" in b)
        if anc is not None:
            for t in tests:
                if not (t.startswith(anc + "/") if anc else "/" not in t):
                    continue
                if t.startswith("backend/") or t.startswith("frontend-v2/"):
                    continue
                s = suite_of(t)
                if not s:
                    continue
                tb = _os.path.basename(t)
                text = read(top, t, cache)
                if _re.match(r"test_" + _re.escape(stem) + r"([_.].*)?$", tb):
                    add(s, t, 1)
                elif distinctive and b in text:
                    add(s, t, 2)
                elif f.endswith(".py") and _re.search(r"(import|from)\s+[\w.]*\b" + _re.escape(stem) + r"\b", text):
                    add(s, t, 2)
        if distinctive and not f.startswith("backend/"):
            for t in be_tests:
                if b in read(top, t, cache):
                    add("backend", t, 2)
    if fe_changed or fe_full:
        suites.setdefault("vitest", {})
    out = {}
    for s, files in suites.items():
        order = sorted(files, key=lambda t: (files[t], t))
        if s != "vitest" and len(order) > MAX_FILES:
            notes.append(f"{s}: {len(order)} affected test files, ran the {MAX_FILES} closest "
                         f"(changed tests, then same name, then importers); CI runs the other {len(order) - MAX_FILES}")
            order = order[:MAX_FILES]
        out[s] = order
    return out, fe_changed, fe_full, notes


# ------------------------------------------------------------- running
def left():
    return BUDGET - (time.time() - STARTED)


def run(argv, cwd, env=None):
    secs = min(LIMIT, int(left()))
    if secs <= 0:
        return None, "time budget used up"
    e = dict(_os.environ)
    e.update(env or {})
    try:
        p = _sp.run(argv, cwd=cwd, stdout=_sp.PIPE, stderr=_sp.STDOUT, timeout=secs, env=e)
    except _sp.TimeoutExpired:
        return (124 if secs < LIMIT else 125), f"timeout after {secs}s"
    except OSError as exc:
        return 127, str(exc)
    return p.returncode, ANSI.sub("", p.stdout.decode(errors="replace"))


def pick_python(top, common):
    for c in (_os.path.join(top, "backend/.venv/bin/python"), _os.path.join(common, "backend/.venv/bin/python"),
              _os.environ.get("MC_TEST_GATE_PYTHON", ""), "python3"):
        if not c:
            continue
        try:
            if _sp.run([c, "-c", "import pytest"], capture_output=True).returncode == 0:
                return c
        except OSError:
            continue
    return None


def has_xdist(py):
    return _sp.run([py, "-c", "import xdist"], capture_output=True).returncode == 0


def failed_ids(out, kind):
    if kind == "vitest":
        return sorted(set(_re.findall(r"FAIL\s+(\S+\.test\.(?:tsx|ts|jsx|js))(?![\w])", out)))
    ids = _re.findall(r"^(?:FAILED|ERROR)\s+(\S+?)(?:\s+-\s.*)?$", out, _re.M)
    return sorted(set(ids))


def suite_cmd(kind, root, py, files, xdist):
    """-> (argv, cwd, env) for one suite rooted at checkout <root>."""
    if kind == "backend":
        # no -x: a test that is already red on main must not stop the run before
        # the branch's own failures are reached (the base recheck filters it)
        argv = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"]
        if xdist and len(files) >= 6:
            argv += ["-n", "auto"]
        return argv + [f[len("backend/"):] if f.startswith("backend/") else f for f in files], \
            _os.path.join(root, "backend"), {"PYTHONPATH": _os.path.join(root, "backend")}
    if kind.startswith("py:"):
        d = kind[3:]
        argv = [py, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"]
        if xdist and d.startswith("tools/") and len(files) >= 2:
            argv += ["-n", "auto"]  # our own tool tests are xdist-safe (CI: gate-audit)
        rel = [f[len(d) + 1:] if d and f.startswith(d + "/") else f for f in files]
        return argv + rel, _os.path.join(root, d), \
            {"PYTHONPATH": _os.pathsep.join([_os.path.join(root, d), _os.path.join(root, "backend")])}
    if kind == "vitest":
        return [_os.path.join(root, "frontend-v2/node_modules/.bin/vitest")] + files, \
            _os.path.join(root, "frontend-v2"), {"NO_COLOR": "1", "FORCE_COLOR": "0", "CI": "1"}
    raise ValueError(kind)


def recheck_on_base(top, base, kind, ids, py, xdist):
    """Run the failing tests again on the merge base. -> set of ids that fail there too."""
    if left() < 15:
        return set()

    def rel_of(i):
        p = i.split("::", 1)[0]
        if kind == "vitest":
            return "frontend-v2/" + p
        if kind == "backend":
            return "backend/" + p
        return (kind[3:] + "/" + p) if kind[3:] else p
    # tests that do not exist on the base are new: their failure is this branch's
    if not any(git(top, "cat-file", "-e", f"{base}:{rel_of(i)}", ok=True).returncode == 0 for i in ids):
        return set()
    tmp = tempfile.mkdtemp(prefix="test-gate-base.")
    shutil.rmtree(tmp)
    r = git(top, "worktree", "add", "--detach", "-q", tmp, base, ok=True)
    if r.returncode != 0:
        return set()
    try:
        if kind == "vitest":
            nm = _os.path.join(top, "frontend-v2/node_modules")
            if _os.path.isdir(nm):
                _os.symlink(_os.path.realpath(nm), _os.path.join(tmp, "frontend-v2/node_modules"))
            present = [i for i in ids if _os.path.exists(_os.path.join(tmp, "frontend-v2", i))]
            if not present:
                return set()
            argv, cwd, env = suite_cmd("vitest", tmp, py, ["run"] + present, False)
        else:
            def path_of(i):
                p = i.split("::", 1)[0]
                if kind == "backend":
                    return _os.path.join(tmp, "backend", p)
                return _os.path.join(tmp, kind[3:], p)
            present = [i for i in ids if _os.path.exists(path_of(i))]
            if not present:
                return set()
            argv, cwd, env = suite_cmd(kind, tmp, py, present, False)
        rc, out = run(argv, cwd, env)
        if rc is None or rc in (124, 125):
            return set()
        return set(failed_ids(out, kind)) if rc != 0 else set()
    finally:
        git(top, "worktree", "remove", "--force", tmp, ok=True)
        shutil.rmtree(tmp, ignore_errors=True)
        git(top, "worktree", "prune", ok=True)


def gate_checkout(top, in_line_commit, blocks, notes, ran):
    if git(top, "rev-parse", "--verify", "-q", "origin/main", ok=True).returncode != 0:
        notes.append(f"origin/main unknown in {top} - gate skipped there")
        return
    base = git(top, "merge-base", "origin/main", "HEAD").strip()
    if not base:
        return
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir").strip()
    common = _os.path.dirname(common) if common else top
    if in_line_commit:
        # the same command commits first: test what will be committed
        changed = git(top, "diff", "--name-only", "--diff-filter=ACMR", base).splitlines()
        changed += git(top, "ls-files", "--others", "--exclude-standard").splitlines()
        deleted = git(top, "diff", "--name-only", "--diff-filter=D", base).splitlines()
    else:
        dirty = [f[3:] for f in git(top, "status", "--porcelain", "--untracked-files=no").splitlines()]
        dirty = [f.split(" -> ")[-1] for f in dirty if not f.endswith(".md")]
        if dirty:
            blocks.append(f"{top}: {len(dirty)} tracked file(s) changed but not committed "
                          f"({', '.join(dirty[:5])}{' ...' if len(dirty) > 5 else ''}). The tests would see code "
                          f"you are not pushing. Commit or stash them first.")
            return
        changed = git(top, "diff", "--name-only", "--diff-filter=ACMR", base, "HEAD").splitlines()
        deleted = git(top, "diff", "--name-only", "--diff-filter=D", base, "HEAD").splitlines()
    changed = sorted(set(c for c in changed if c))

    # A deleted test hides a red test. Allowed when its subject went too, or a
    # commit on the branch says why: "Removed-Test: <path> - <reason>".
    del_tests = [d for d in deleted if is_test(d)]
    if del_tests:
        log = git(top, "log", "--format=%B", base + "..HEAD")
        del_stems = {_os.path.basename(d).rsplit(".", 1)[0] for d in deleted if not is_test(d)}
        unexplained = []
        for d in del_tests:
            subj = _re.sub(r"^test_|\.test$", "", _os.path.basename(d).rsplit(".", 1)[0])
            m = _re.search(r"^Removed-Test:\s*" + _re.escape(d) + r"\s*[-—–:]\s*\S", log, _re.M)
            if m or subj in del_stems:
                notes.append(f"deleted test {d} (explained)")
            else:
                unexplained.append(d)
        if unexplained:
            blocks.append("this branch deletes test file(s) without saying why: " + ", ".join(unexplained) +
                          ". A deleted red test looks green. Add a commit with the trailer "
                          "'Removed-Test: <path> - <reason>' (one per file) if the removal is meant.")
    if not changed:
        return
    suites, fe_changed, fe_full, sel_notes = select_tests(top, changed)
    notes.extend(sel_notes)
    py = pick_python(top, common) if any(s == "backend" or s.startswith("py:") for s in suites) else None
    xdist = bool(py) and has_xdist(py)
    for kind, files in sorted(suites.items()):
        if kind == "sh":
            for t in files:
                ran.append(f"bash {t}")
                rc, out = run(["bash", t], top)
                if rc is None or rc == 124:
                    blocks.append(f"time budget ({BUDGET}s) used up before 'bash {t}' finished")
                elif rc != 0:
                    blocks.append(f"RED: bash {t} (exit {rc})\n" + "\n".join(out.splitlines()[-25:]))
            continue
        if kind == "vitest":
            vt = _os.path.join(top, "frontend-v2/node_modules/.bin/vitest")
            if not _os.path.exists(vt):
                shared = _os.path.join(common, "frontend-v2/node_modules")
                if _os.path.isdir(shared) and shared != _os.path.join(top, "frontend-v2/node_modules"):
                    blocks.append(f"frontend files changed but {top}/frontend-v2/node_modules is missing, so vitest "
                                  f"cannot run. Link it: ln -s {shared} {top}/frontend-v2/node_modules")
                else:
                    notes.append("frontend-v2/node_modules missing and no shared copy - vitest NOT run")
                continue
            rel = [f[len("frontend-v2/"):] for f in files]
            if fe_full:
                args, label = ["run"], "vitest run (config/setup changed: whole suite)"
            else:
                args = ["related", "--run"] + sorted(set(rel + [f[len("frontend-v2/"):] for f in fe_changed]))
                label = f"vitest related ({len(args) - 2} file(s))"
            argv, cwd, env = suite_cmd("vitest", top, None, args, False)
        else:
            if not py:
                notes.append(f"{kind}: no python with pytest found - {len(files)} test file(s) NOT run")
                continue
            argv, cwd, env = suite_cmd(kind, top, py, files, xdist)
            label = f"{kind} pytest ({len(files)} file(s))"
        ran.append(label)
        rc, out = run(argv, cwd, env)
        if rc is None or rc == 124:
            blocks.append(f"time budget ({BUDGET}s) used up before '{label}' finished. Run it yourself, "
                          f"or raise MC_TEST_GATE_BUDGET in the hook entry (keep it below the hook timeout).")
            continue
        if kind == "vitest" and rc != 0 and "No test files found" in out:
            continue
        if rc == 5 and kind != "vitest":  # pytest: nothing collected
            continue
        if rc != 0:
            ids = failed_ids(out, kind)
            pre = recheck_on_base(top, base, kind, ids, py, xdist) if ids else set()
            new = [i for i in ids if i not in pre]
            if ids and not new:
                notes.append(f"{label}: {len(ids)} failing test(s) fail on origin/main too (not this branch): "
                             + ", ".join(ids[:5]))
                continue
            tail = "\n".join(out.splitlines()[-30:])
            blocks.append(f"RED: {label} (exit {rc})" + (f" - new failures: {', '.join(new[:8])}" if new else "")
                          + "\n" + tail)


_PUSHY = _re.compile(r"push|commit|pulls|send-pack|\bpr\b")


def report(blocks, notes, ran, what="push / PR"):
    for n in notes:
        say("test-gate: note - " + n)
    if blocks:
        say("test-gate: BLOCKED - " + str(len(blocks)) + f" problem(s) before this {what}:")
        for b in blocks:
            say("--- " + b)
        say("Fix the tests (or the code) first. This gate is early feedback; CI still runs everything.")
        return 2
    if ran:
        say(f"test-gate: ok - {len(ran)} suite(s) affected by this branch green: " + "; ".join(ran))
    return 0


def pre_push(lines):
    """git's own pre-push hook: stdin has '<local ref> <local sha> <remote ref>
    <remote sha>' per pushed ref, cwd is the pushing checkout. git runs this for
    every spelling of a push (bash -lc, xargs, python, aliases, scripts)."""
    top = toplevel(_os.getcwd())
    if not top:
        return 0
    blocks, notes, ran, checkouts = [], [], [], []
    for line in lines.splitlines():
        parts = line.split()
        if len(parts) < 4 or set(parts[1]) == {"0"}:
            continue  # malformed, or a deletion
        local = parts[0]
        if local.startswith("refs/tags/"):
            continue
        branch = local[len("refs/heads/"):] if local.startswith("refs/heads/") else None
        if branch is None:
            wt = worktree_at(top, parts[1]) or (top if git(top, "rev-parse", "HEAD").strip() == parts[1] else None)
            err = None if wt else (f"pre-push: {local} ({parts[1][:10]}) is not checked out in any worktree, so "
                                   f"its tests cannot run. Push a branch from its own worktree.")
        else:
            wt, err = resolve_branch(top, branch, "pre-push")
        if err:
            blocks.append(err)
        elif wt and wt not in checkouts:
            checkouts.append(wt)
    for wt in checkouts:
        gate_checkout(wt, False, blocks, notes, ran)
    return report(blocks, notes, ran, "push")


def main():
    if _os.environ.get("MC_HOOK_MODE") == "pre-push":
        return pre_push(sys.stdin.read())
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (d.get("tool_input") or {}).get("command") or ""
    cwd = d.get("cwd") or _os.getcwd()
    try:
        events = analyze(cmd, cwd)
    except Exception as exc:  # a parser bug must not stop all work - but not open the gate either
        if _PUSHY.search(cmd):
            say(f"test-gate: BLOCKED - could not read this command ({exc!r}) and it may push or open a PR. "
                f"Write it plainly (git -C <worktree> push) and report the reader bug.")
            return 2
        say(f"test-gate: could not read the command ({exc!r}) - not gated")
        return 0
    gated = [(k, e) for k, e in events if k in ("push", "pr")]
    hidden = [e["why"] for k, e in events if k == "hidden"]
    blocks, notes, ran = [], [], []
    for why in hidden:
        blocks.append(f"{why}: the gate cannot see what this runs. Write git/gh literally.")
    commit_tops = set()
    for k, e in events:
        if k in ("commit", "add"):
            t = toplevel(e.get("dir")) if e.get("dir") else None
            if t:
                commit_tops.add(t)
    checkouts = []
    for kind, e in gated:
        outs, why, nts = resolve(kind, e, cwd)
        blocks += why
        notes += nts
        for o in outs:
            if o and o not in checkouts:
                checkouts.append(o)
    for top in checkouts:
        gate_checkout(top, top in commit_tops, blocks, notes, ran)
    return report(blocks, notes, ran)


sys.exit(main())
PY
