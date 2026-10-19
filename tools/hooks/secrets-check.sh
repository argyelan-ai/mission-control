#!/usr/bin/env bash
# secrets-check — Claude Code PreToolUse hook (matcher: Bash).
#
# Blocks `git commit` and `git push` when the lines they would add contain a
# credential, or when a secret-bearing file goes along - and blocks commands
# that switch off or dodge the checks. CI's leak gate is the last line, but by
# then the secret is on GitHub. This hook stops it on the machine.
#
# Red case (exit 2 = Claude Code blocks the tool call; stderr names the file
# and the kind of secret, never the value):
#   git commit -> added lines of the STAGED diff; with -a/--all or a `git add`
#                 in the same command also unstaged (and, for add, new) files;
#                 with pathspecs (git commit x.py, -o, -i) the named files as
#                 they are in the working tree
#   git push   -> added lines of the branch diff against origin/main, for each
#                 pushed branch (git push origin other-branch scans that one)
#   matches: private key block, GitHub token (ghp_/gho_/ghs_/github_pat_),
#            Anthropic key (sk-ant-), OpenAI-style key (sk-/sk-proj-, 40+),
#            AWS access key id (AKIA...), Slack token (xox?-), Hugging Face
#            token (hf_...), or a .env / *.pem / *.p12 / id_rsa* file
#            (.env.example and other *.example are fine).
#   dodging a check: git push/commit --no-verify (commit -n), git -c
#            core.hooksPath=..., GIT_CONFIG_* setting core.hooksPath, git config
#            core.hooksPath <x> / --unset, MC_LEAK_SCAN_SKIP=..., a commit or
#            push whose checkout is behind a variable, git whose words the
#            reader cannot see (xargs input, a pipe into a shell, assembled
#            strings), a command that seems to run outside a checkout while
#            the session sits in one, a command the reader fails on.
#   Spellings read: see cmdparse below (wrappers, bash -c / -lc, eval, ( ),
#   $( ), pipes and heredocs into a shell, script files, find -exec, awk,
#   aliases). It is a blacklist reader and cannot be complete.
# Exceptions: values containing TEST/dummy/fake/example; a line with
#   `# secrets-check: allow - <reason>` in a TEST file (tests/, fixtures/,
#   test_*.py, *.test.ts). A bare marker, or one outside test files, blocks.
#   Every allowed line is printed as a note, so the reviewer sees it.
# Honest limits: pattern list, not entropy analysis (CI's gitleaks is the
# broad scan and still sees marked lines). A push from a repo without
# origin/main is not scanned (warns). Only reaches Claude Code sessions that
# register this hook.
# Sabotage probes: tools/hooks/tests/test_secrets_check.py, test_cmdparse.py.
set -u
command -v python3 >/dev/null 2>&1 || { echo "secrets-check: python3 missing - check skipped" >&2; exit 0; }
payload="$(cat)"
MC_HOOK_PAYLOAD="$payload" exec python3 - <<'PY'
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
    stdin = None
    for x in extras:
        if isinstance(x, _PS):
            _sub(ctx, analyze, x.body, ctx.cwd, depth + 1, ctx)
        elif isinstance(x, _HD):
            stdin = ("text", x.body)
        elif isinstance(x, _RIN):
            stdin = ("file", x.body)
    if stdin is None and up is not None:
        stdin = ("pipe", up)
    if words:
        _simple(words, ctx, depth, stdin)


_EXEC = {"git", "gh", "curl", "eval", "source", ".", "cd", "pushd", "popd", "find", "awk", "gawk",
         "mawk", "nawk", "export"}
_RUNNERS = {"xargs", "parallel"}


def _effective(words):
    """Index of the word that is really executed (skips VAR=x and wrappers)."""
    i = 0
    while i < len(words) and _re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", words[i]):
        i += 1
    if i >= len(words):
        return None
    if _os.path.basename(words[i]) in _WRAPPERS:
        for j in range(i + 1, len(words)):
            b = _os.path.basename(words[j])
            if b in _EXEC or b in _SHELLS or _INTERP.match(b):
                return j
            if words[j].dyn and not words[j].startswith("-"):
                return j
        return None
    return i


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


def _simple(words, ctx, depth, stdin=None):
    words = [_expand(w, ctx) for w in words]
    for w in words:
        m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", w)
        if not m:
            break
        ctx.env[m.group(1)] = m.group(2)
    for w in words:
        if _re.match(r"^MC_LEAK_SCAN_SKIP=", w):
            ctx.events.append(("bypass", {"why": "MC_LEAK_SCAN_SKIP switches off the pre-push leak scan"}))
    exe = _effective(words)
    if exe is None:
        if words and words[0] == "export":
            for w in words[1:]:
                m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", w)
                if m:
                    ctx.env[m.group(1)] = m.group(2)
        return
    head, args = words[exe], words[exe + 1:]
    name = _os.path.basename(head)
    if head.dyn:
        if any(_TELL.match(a) for a in args):
            ctx.events.append(("hidden", {"why": f"command behind a variable ({head}) with '{' '.join(args[:3])}'"}))
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
        for w in args:
            m = _re.match(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$", w)
            if m:
                ctx.env[m.group(1)] = m.group(2)
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
        analyze(" ".join(args), ctx.cwd, depth + 1, ctx)
    elif name in _SHELLS:
        code, script = _shell_args(args)
        if code is not None:
            if "$(...)" in code or (_re.search(r"\$\{?[A-Za-z_]", code) and
                                    _re.search(r"\b(push|commit|send-pack)\b|\bpr\s+(create|new)\b", code)):
                ctx.events.append(("hidden", {"why": f"{name} -c with a command behind a variable ({code[:40]})"}))
            _sub(ctx, analyze, code, ctx.cwd, depth + 1, ctx)
        elif script:
            _sub(ctx, _script, _abspath(script, ctx.cwd), ctx, depth)
        else:
            _stdin(stdin, ctx, depth, "shell")
    elif name in ("source", "."):
        if args:
            _script(_abspath(args[0], ctx.cwd), ctx, depth)
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

# ------------------------------------------------------------- secrets-check
import json
import sys

RULES = [
    ("private key block", r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----"),
    ("GitHub token", r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{60,})"),
    ("Anthropic key", r"\bsk-ant-[A-Za-z0-9_-]{20,}"),
    ("OpenAI-style key", r"\bsk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    ("AWS access key id", r"\bAKIA[0-9A-Z]{16}\b"),
    ("Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    ("Hugging Face token", r"\bhf_[A-Za-z0-9]{30,}"),
]
# A value that says it is not real (test fixtures: "xoxb-TEST-token").
PLACEHOLDER = _re.compile(r"test|dummy|fake|example|placeholder|not-a-real|xxxxxxxx", _re.I)
ALLOW = _re.compile(r"secrets-check:\s*allow\b(.*)", _re.I)
# The marker is self-certification, so it only counts where fixtures live.
TEST_PATH = _re.compile(r"(^|/)(tests?|__tests__|fixtures?|testdata)/|(^|/)test_[^/]*$|_test\.(py|go)$|"
                        r"\.(test|spec)\.[jt]sx?$")
ALLOWED = []  # (path, kind) of every line let through by a marker - always reported
REASON = _re.compile(r"^\s*[-—–:]+\s*\S")
BAD_FILE = _re.compile(r"(^|/)(\.env(\.[A-Za-z0-9_-]+)?|[^/]+\.pem|[^/]+\.p12|id_rsa[^/]*)$")


def git(top, *args):
    r = _sp.run(["git", "-C", top, *args], capture_output=True, text=True, errors="replace")
    return r.stdout if r.returncode == 0 else ""


def toplevel(d, gdir=None, wtree=None):
    if gdir:
        args = ["git", "--git-dir", gdir] + (["--work-tree", wtree] if wtree else [])
        r = _sp.run(args + ["rev-parse", "--show-toplevel"], capture_output=True, text=True, cwd=d or None)
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip()
        try:
            with open(_os.path.join(gdir, "gitdir")) as fh:
                return _os.path.dirname(fh.read().strip())
        except OSError:
            return None
    if not d or not _os.path.isdir(d):
        return None
    r = _sp.run(["git", "-C", d, "rev-parse", "--show-toplevel"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def scan(path, text, out):
    for line in text.splitlines():
        for name, rx in RULES:
            m = _re.search(rx, line)
            if not m or PLACEHOLDER.search(m.group(0)):
                continue
            a = ALLOW.search(line)
            if a and REASON.match(a.group(1)) and TEST_PATH.search(path):
                ALLOWED.append((path, name))
                continue
            if a and REASON.match(a.group(1)):
                out.add(f"{path}: {name} (allow marker counts only in test files - tests/, fixtures/, "
                        f"test_*.py, *.test.ts; move the fixture there)")
            elif a:
                out.add(f"{path}: {name} (allow marker needs a reason: '# secrets-check: allow - <reason>')")
            else:
                out.add(f"{path}: {name}")


def scan_diff(diff, out):
    added, current = {}, "?"
    for line in diff.splitlines():
        if line.startswith("+++ "):
            current = line[6:] if line.startswith("+++ b/") else line[4:]
            continue
        if line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current, []).append(line[1:])
    for path, lines in added.items():
        scan(path, "\n".join(lines), out)


def scan_files(top, names, out, read_content):
    for f in names:
        f = f.strip()
        if not f:
            continue
        if BAD_FILE.search(f) and not f.endswith(".example") and not f.endswith(".pub"):
            out.add(f"{f}: secret-bearing file")
        if read_content:
            p = _os.path.join(top, f)
            try:
                if _os.path.getsize(p) > 2_000_000:
                    continue
                with open(p, "rb") as fh:
                    data = fh.read()
            except OSError:
                continue
            if b"\0" in data[:8192]:
                continue
            scan(f, data.decode("utf-8", "replace"), out)


def main():
    try:
        d = json.load(sys.stdin)
    except Exception:
        return 0
    cmd = (d.get("tool_input") or {}).get("command") or ""
    cwd = d.get("cwd") or _os.getcwd()
    try:
        events = analyze(cmd, cwd)
    except Exception as exc:
        if _re.search(r"push|commit", cmd):
            print(f"secrets-check: BLOCKED - could not read this command ({exc!r}) and it may commit or push. "
                  f"Write it plainly (git -C <checkout> commit ...) and report the reader bug.", file=sys.stderr)
            return 2
        print(f"secrets-check: could not read the command ({exc!r}) - not checked", file=sys.stderr)
        return 0
    blocks, out, modes = [], set(), []
    hook_top = toplevel(cwd)
    for kind, e in events:
        if kind == "bypass":
            blocks.append(e["why"])
        elif kind == "hidden":
            blocks.append(f"{e['why']}: what this commits or pushes cannot be checked. Write git literally")
    adds = set()
    for kind, e in events:
        if kind == "add":
            t = toplevel(e.get("dir"), e.get("git_dir"), e.get("work_tree"))
            if t:
                adds.add(t)
    for kind, e in events:
        if kind not in ("commit", "push"):
            continue
        if kind not in modes:
            modes.append(kind)
        if e.get("dir") is None and not e.get("git_dir"):
            blocks.append(f"git {kind}: the checkout is behind a variable or an unknown 'cd' - write the path "
                          f"literally (git -C <checkout> {kind}) so it can be checked")
            continue
        top = toplevel(e.get("dir"), e.get("git_dir"), e.get("work_tree"))
        if not top:
            if hook_top and not e.get("explicit"):
                # the reader's folder may be wrong (a cd it could not follow, a
                # failed cd); the session's checkout is where git may really run
                blocks.append(f"git {kind}: the command seems to run in {e.get('dir')}, not a git checkout, "
                              f"while the session is in {hook_top} - write git -C <checkout> {kind}")
            continue
        if kind == "commit":
            if e.get("paths"):
                # git commit <path> / -o / -i: records the files as they are in
                # the working tree, staged or not
                p = [x for x in e["paths"] if x]
                scan_diff(git(top, "diff", "HEAD", "-U0", "--no-color", "--", *p), out)
                scan_diff(git(top, "diff", "--cached", "-U0", "--no-color"), out)
                scan_files(top, git(top, "diff", "HEAD", "--name-only", "--diff-filter=ACMR", "--", *p).splitlines(),
                           out, False)
            if e.get("all") or top in adds:
                # -a / a `git add` in the same line: the hook runs BEFORE the
                # command, so also look at what is not staged yet.
                if git(top, "rev-parse", "--verify", "-q", "HEAD"):
                    scan_diff(git(top, "diff", "HEAD", "-U0", "--no-color"), out)
                    scan_files(top, git(top, "diff", "HEAD", "--name-only", "--diff-filter=ACMR").splitlines(),
                               out, False)
                else:
                    scan_diff(git(top, "diff", "--cached", "-U0", "--no-color"), out)
                if top in adds:
                    scan_files(top, git(top, "ls-files", "--others", "--exclude-standard").splitlines(), out, True)
            else:
                scan_diff(git(top, "diff", "--cached", "-U0", "--no-color"), out)
                scan_files(top, git(top, "diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines(),
                           out, False)
            continue
        # push: what leaves the machine = branch diff against origin/main, for
        # every branch named in the refspecs (not only the checked-out one)
        if not git(top, "rev-parse", "--verify", "-q", "origin/main"):
            print(f"secrets-check: origin/main unknown in {top} - push diff not scanned", file=sys.stderr)
            continue
        flags = e.get("flags") or set()
        if flags & {"--delete", "-d"}:
            continue
        refs = []
        for spec in e.get("refs") or []:
            s = spec.lstrip("+")
            if s.startswith(":"):
                continue
            refs.append(s.split(":", 1)[0] or "HEAD")
        if flags & {"--all", "--mirror", "--branches"}:
            refs = [b for b in git(top, "for-each-ref", "--format=%(refname)", "refs/heads").splitlines() if b]
        for ref in refs or ["HEAD"]:
            if ref in ("@",):
                ref = "HEAD"
            if not git(top, "rev-parse", "--verify", "-q", ref + "^{commit}"):
                continue
            base = git(top, "merge-base", "origin/main", ref).strip()
            if not base:
                continue
            scan_diff(git(top, "diff", "-U0", "--no-color", base, ref), out)
            scan_files(top, git(top, "diff", "--name-only", "--diff-filter=ACMR", base, ref).splitlines(), out, False)
    if blocks:
        print("secrets-check: BLOCKED - this would switch off or dodge a check:", file=sys.stderr)
        for b in blocks:
            print("  " + b, file=sys.stderr)
        print("Hooks are not optional for agents. If a hook is wrong, report it; the operator decides.",
              file=sys.stderr)
    for path, name in sorted(set(ALLOWED)):
        print(f"secrets-check: note - allow marker used: {path} ({name}); the PR reviewer should look at it",
              file=sys.stderr)
    if out:
        print(f"secrets-check: BLOCKED ({'+'.join(sorted(modes))}) - possible secret in what this would publish:",
              file=sys.stderr)
        for f in sorted(out):
            print("  " + f, file=sys.stderr)
        print("Remove it (and rotate it if it was real). Values are not printed here.", file=sys.stderr)
    return 2 if (blocks or out) else 0


sys.exit(main())
PY
