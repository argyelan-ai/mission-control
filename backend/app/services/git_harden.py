"""Hardened git invocation for directories an agent can write to.

The backend runs git inside agent workspaces, task worktrees and project
checkouts (Changes panel, chat file revert/commit, task push/PR). Those
trees are agent-writable, so their repository config (``.git/config``,
``config.worktree``, anything it ``include``s, ``.git/info/attributes``)
is untrusted. With ``safe.directory=*`` git honours that config — and a
lot of git config names a *program* git runs: fsmonitor, hooks, clean /
smudge / process filters, diff drivers, merge drivers, upload/receive-pack,
ssh/askpass/credential helpers, gpg, git:// proxies. Unhardened, a plain
``git status`` or ``git diff`` in such a tree runs whatever the agent put
there, inside the backend.

Every git call on such a tree goes through ``hardened_argv`` +
``hardened_env``. What blocks what (each proven in
``tests/test_git_harden.py``):

* Config overrides — passed as ``GIT_CONFIG_COUNT`` env pairs, which act
  like ``-c`` (highest priority) and also reach git processes started by
  ``gh``: ``core.fsmonitor``, ``core.hooksPath``, ``core.sshCommand``,
  ``core.askPass``, ``protocol.ext/git.allow``, gpg/signing, submodule
  recursion, and — read fresh from the repo's own config on every call —
  every ``filter.<name>`` / ``merge.<name>.driver`` the local scope
  defines. ``credential.helper`` is reset and only the system/global
  helpers are put back, so a repo-local helper never sees the GitHub token.
* Flags, because those keys either are first-wins (``remote.*.uploadpack``)
  or have no "off" value (an empty diff driver fails instead of being
  skipped): ``--no-ext-diff --no-textconv`` on diff/show/log,
  ``--ignore-submodules=dirty`` on diff/status (otherwise git runs status
  inside the submodule under *its* config), ``--upload-pack`` /
  ``--receive-pack`` on network commands.

Known limits: the filter names are read in a separate git call just
before the real one, so a config rewritten in between (milliseconds) is
not covered; ``core.worktree`` is not overridden (a read, not an exec).
"""

from __future__ import annotations

import os
import re
import subprocess
from collections.abc import Mapping, Sequence
from pathlib import Path

# Scopes an agent can write. ``--show-scope`` reports included files under
# the scope of the file that included them.
_UNTRUSTED_SCOPES = frozenset({"local", "worktree"})

_STATIC_OVERRIDES: tuple[tuple[str, str], ...] = (
    ("core.fsmonitor", "false"),
    ("core.hooksPath", "/dev/null"),
    ("core.pager", "cat"),
    ("core.sshCommand", "ssh"),
    ("core.askPass", ""),
    ("protocol.ext.allow", "never"),
    # git:// is unauthenticated and unused; blocking it disarms core.gitProxy,
    # which has no value that switches it off.
    ("protocol.git.allow", "never"),
    ("log.showSignature", "false"),
    ("commit.gpgSign", "false"),
    ("tag.gpgSign", "false"),
    ("push.gpgSign", "false"),
    ("gpg.program", "gpg"),
    ("gpg.ssh.program", "ssh-keygen"),
    ("gpg.x509.program", "gpgsm"),
    ("gpg.ssh.defaultKeyCommand", ""),
    ("submodule.recurse", "false"),
    ("fetch.recurseSubmodules", "false"),
    ("push.recurseSubmodules", "no"),
)

_FILTER_KEY = re.compile(r"^filter\.(.+)\.(?:clean|smudge|process|required)$", re.IGNORECASE)
_MERGE_DRIVER_KEY = re.compile(r"^merge\.(.+)\.driver$", re.IGNORECASE)
_CREDENTIAL_HELPER_KEY = re.compile(r"^credential\.(?:.+\.)?helper$", re.IGNORECASE)

_DIFF_COMMANDS = frozenset({"diff", "show", "log", "whatchanged"})
_SUBMODULE_STATUS_COMMANDS = frozenset({"diff", "status"})
_UPLOAD_PACK_COMMANDS = frozenset({"fetch", "pull", "ls-remote"})


def _subcommand_index(args: Sequence[str]) -> int | None:
    """Index of the git subcommand in ``args`` (no leading ``"git"``),
    skipping global options such as ``-C <dir>`` / ``-c k=v``."""
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("-C", "-c", "--git-dir", "--work-tree", "--namespace"):
            i += 2
        elif a.startswith("-"):
            i += 1
        else:
            return i
    return None


def _effective_dir(args: Sequence[str], cwd: str | os.PathLike | None) -> str:
    """The directory git will run in: ``cwd`` plus any ``-C`` options."""
    path = Path(cwd) if cwd is not None else Path.cwd()
    i = 0
    stop = _subcommand_index(args)
    while i < (stop if stop is not None else len(args)):
        if args[i] == "-C" and i + 1 < len(args):
            path = path / args[i + 1]
            i += 2
        else:
            i += 1
    return str(path)


def hardened_argv(args: Sequence[str]) -> list[str]:
    """``["git", *args]`` with the per-subcommand safety flags inserted
    right after the subcommand. ``args`` must not start with ``"git"``."""
    args = list(args)
    idx = _subcommand_index(args)
    if idx is None:
        return ["git", "--no-pager", *args]
    sub = args[idx]
    flags: list[str] = []
    if sub in _DIFF_COMMANDS:
        flags += ["--no-ext-diff", "--no-textconv"]
    if sub in _SUBMODULE_STATUS_COMMANDS:
        flags.append("--ignore-submodules=dirty")
    if sub in _UPLOAD_PACK_COMMANDS:
        flags.append("--upload-pack=git-upload-pack")
    if sub == "push":
        flags.append("--receive-pack=git-receive-pack")
    return ["git", "--no-pager", *args[: idx + 1], *flags, *args[idx + 1 :]]


def _read_config(cwd: str, env: Mapping[str, str]) -> list[tuple[str, str, str]]:
    """``(scope, key, value)`` for every config entry git sees in ``cwd``.
    Reading config runs no program. Empty on any failure — the real call
    then still gets every static override."""
    try:
        res = subprocess.run(
            ["git", "-c", "safe.directory=*", "config", "--list", "--show-scope", "-z"],
            cwd=cwd,
            capture_output=True,
            env=dict(env),
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if res.returncode != 0:
        return []
    fields = res.stdout.decode("utf-8", errors="replace").split("\0")
    entries: list[tuple[str, str, str]] = []
    for scope, kv in zip(fields[0::2], fields[1::2]):
        key, _, value = kv.partition("\n")
        entries.append((scope, key, value))
    return entries


def config_overrides(cwd: str, env: Mapping[str, str]) -> list[tuple[str, str]]:
    """Static overrides plus the ones derived from the repo's own config."""
    pairs = list(_STATIC_OVERRIDES)
    entries = _read_config(cwd, env)
    filters: dict[str, None] = {}
    merge_drivers: dict[str, None] = {}
    trusted_helpers: list[tuple[str, str]] = []
    for scope, key, value in entries:
        if scope == "command":
            continue
        if scope in _UNTRUSTED_SCOPES:
            if m := _FILTER_KEY.match(key):
                filters[m.group(1)] = None
            elif m := _MERGE_DRIVER_KEY.match(key):
                merge_drivers[m.group(1)] = None
        elif _CREDENTIAL_HELPER_KEY.match(key):
            trusted_helpers.append((key, value))
    for name in filters:
        pairs += [
            (f"filter.{name}.clean", ""),
            (f"filter.{name}.smudge", ""),
            (f"filter.{name}.process", ""),
            (f"filter.{name}.required", "false"),
        ]
    # An empty driver makes the merge fail instead of running anything.
    pairs += [(f"merge.{name}.driver", "") for name in merge_drivers]
    # An empty helper clears the list collected so far; command-scope config
    # is read last, so afterwards only the trusted helpers are left.
    pairs.append(("credential.helper", ""))
    pairs += trusted_helpers
    return pairs


def hardened_env(
    cwd: str | os.PathLike | None,
    args: Sequence[str] = (),
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Process env for a git (or gh) call in ``cwd``: no prompts, no
    editors, no pager, plus the config overrides as ``GIT_CONFIG_*``."""
    env = dict(os.environ if base is None else base)
    env.update(
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        SSH_ASKPASS="",
        GIT_EDITOR="true",
        GIT_SEQUENCE_EDITOR="true",
        GIT_PAGER="cat",
    )
    inherited: list[tuple[str, str]] = []
    try:
        count = int(env.pop("GIT_CONFIG_COUNT", "0") or "0")
    except ValueError:
        count = 0
    for i in range(count):
        key = env.pop(f"GIT_CONFIG_KEY_{i}", None)
        value = env.pop(f"GIT_CONFIG_VALUE_{i}", "")
        if key:
            inherited.append((key, value))
    pairs = inherited + config_overrides(_effective_dir(args, cwd), env)
    env["GIT_CONFIG_COUNT"] = str(len(pairs))
    for i, (key, value) in enumerate(pairs):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    return env


def run_git(
    *args: str,
    cwd: str | os.PathLike | None = None,
    env: Mapping[str, str] | None = None,
    **kwargs,
) -> subprocess.CompletedProcess:
    """``subprocess.run`` of a hardened git call; ``kwargs`` pass through
    (``capture_output``, ``text``, ``check``, ``input`` …)."""
    return subprocess.run(
        hardened_argv(args),
        cwd=None if cwd is None else str(cwd),
        env=hardened_env(cwd, args, env),
        **kwargs,
    )
