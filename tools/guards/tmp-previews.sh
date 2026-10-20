#!/usr/bin/env bash
# tmp-previews.sh — previews, deploy folders and worktrees do not belong in /tmp.
#
# Lesson: preview and deploy folders were built under /tmp. macOS clears /tmp
# entries after a few days, so one preview lost its deploy link and a handoff
# pointed to nothing. It happened twice before the rule stuck.
#
# Red for a line that WRITES to /tmp/, /private/tmp/ or $TMPDIR when the target
# or its command names one of: preview, vorschau, deploy, handoff, worktree,
# render, screenshot, .vercel, release. A write is:
#   * a redirection target (`> /tmp/x`, `>> …`, `&> …`),
#   * an output option (`--out /tmp/x`, `--output=…`, `-o …`, `--dest …`, `path=…`),
#   * the destination of cp/mv/rsync/ln/install/ditto/scp (the last argument),
#   * an argument of mkdir/touch/tee/mktemp, `git worktree add <path>`, `git clone … <path>`,
#   * an assignment `OUT=/tmp/preview-x` (the folder is then used as output).
# Reading or cleaning up (ls, cat, du, rm, find …) is not a write. The harness
# session scratchpad (/tmp/claude-*/…/scratchpad) is a sanctioned place, not a leak.
# Short-lived files (health stamps, scratch output read once) stay allowed.
# Opt out on one line of a repo file with "guard-allow: tmp-previews (<reason>)"
# (ignored in live commands, see lib.sh).
#
# Usage: tmp-previews.sh <file-or-folder>...   Exit: 0 green, 1 red, 2 usage.
export GUARD_NAME=tmp-previews
. "$(cd "$(dirname "$0")" && pwd)/lib.sh"

RE_TMP='(^|[^A-Za-z0-9_.-])(/private)?/tmp/|[$][{]?TMPDIR'
RE_DURABLE='([Pp]review|[Vv]orschau|[Dd]eploy|[Hh]andoff|[Ww]orktree|[Rr]ender|[Ss]creenshot|[.]vercel|[Rr]elease)'

# writes_durable_tmp <line>: exit 0 when the line writes durable output under /tmp.
writes_durable_tmp() {
  python3 - "$1" <<'PY'
import re, shlex, sys
line = sys.argv[1]
TMP = re.compile(r"(?:^|[^A-Za-z0-9_.-])(?:/private)?/tmp/|\$\{?TMPDIR")
SCRATCH = re.compile(r"(?:/private)?/tmp/claude-[^/\s]+/(?:[^\s]*/)?scratchpad(?:/|$)|\$\{?TMPDIR\}?/claude-[^/\s]+/(?:[^\s]*/)?scratchpad(?:/|$)")
DURABLE = re.compile(r"[Pp]review|[Vv]orschau|[Dd]eploy|[Hh]andoff|[Ww]orktree|[Rr]ender|[Ss]creenshot|\.vercel|[Rr]elease")
OUT_OPT = re.compile(r"^(--?(?:o|O|out|output|outdir|out-dir|output-dir|output-file|dest|destination|target|save|save-to|screenshot|path|file|filename|dir|directory|prefix))(?:=(.*))?$")
KEY_ARG = re.compile(r"(?:^|[(,])(?:path|filename|file|out|output|dest)=['\"]?([^'\",)]+)")
COPY = {"cp", "mv", "rsync", "ln", "install", "ditto", "scp"}
MAKE = {"mkdir", "touch", "tee", "mktemp"}
READ = {"rm", "rmdir", "ls", "cat", "du", "stat", "test", "[", "[[", "head", "tail", "less", "more",
        "grep", "rg", "find", "file", "wc", "open", "diff", "readlink", "realpath", "shasum", "md5", "trash"}
PREFIX = {"sudo", "env", "nohup", "time", "command", "exec", "xargs"}
try:
    lex = shlex.shlex(line, posix=True, punctuation_chars=";&|()<>")
    lex.whitespace_split = True
    toks = list(lex)
except ValueError:
    toks = line.split()
segs, cur = [], []
for t in toks:
    if t and set(t) <= set(";&|()") :
        segs.append(cur); cur = []
    else:
        cur.append(t)
segs.append(cur)

def bad(target, seg):
    if not TMP.search(" " + target) or SCRATCH.search(target):
        return False
    return bool(DURABLE.search(target) or DURABLE.search(" ".join(seg)))

for seg in segs:
    targets, words, i = [], [], 0
    while i < len(seg):
        t = seg[i]
        if set(t) <= set("<>&|") and ">" in t:          # > >> &> >| (not >&1)
            if i + 1 < len(seg) and not (t.endswith("&") and seg[i + 1].isdigit()):
                targets.append(seg[i + 1])
            i += 2
            continue
        if set(t) <= set("<>&"):
            i += 2
            continue
        words.append(t)
        i += 1
    # leading assignments: VAR=/tmp/preview-x
    k = 0
    while k < len(words) and re.match(r"^[A-Za-z_]\w*=", words[k]):
        targets.append(words[k].split("=", 1)[1]); k += 1
    while k < len(words) and words[k] in PREFIX:
        k += 1
    verb = words[k].rsplit("/", 1)[-1] if k < len(words) else ""
    args = words[k + 1:]
    for t in args:
        m = KEY_ARG.search(t)
        if m:
            targets.append(m.group(1))
    if verb not in READ:
        for j, t in enumerate(args):
            m = OUT_OPT.match(t)
            if m:
                if m.group(2) is not None:
                    targets.append(m.group(2))
                elif j + 1 < len(args):
                    targets.append(args[j + 1])
        plain = [a for a in args if not a.startswith("-")]
        if verb in COPY and plain:
            targets.append(plain[-1])
        if verb in MAKE:
            targets.extend(plain)
        if verb == "git" and len(plain) >= 2 and plain[0] == "worktree" and plain[1] == "add" and len(plain) >= 3:
            targets.append(plain[2])
        if verb == "git" and plain[:1] == ["clone"] and len(plain) >= 3:
            targets.append(plain[-1])
    if any(bad(t, seg) for t in targets):
        sys.exit(0)
sys.exit(1)
PY
}

check_line() {
  is_comment "$3" && return 0
  # cheap filter first: most lines name neither /tmp nor a durable word
  [[ $3 =~ $RE_TMP ]] && [[ $3 =~ $RE_DURABLE ]] || return 0
  # >>> guard-logic
  if writes_durable_tmp "$3"; then
    hit "$1" "$2" "durable output under /tmp (macOS deletes it after a few days). Use a folder under the project or \$HOME"
  fi
  # <<< guard-logic
}

lint_main "$@"
