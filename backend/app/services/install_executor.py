"""Execute install/uninstall after approval.

This service calls Service-Layer-functions directly (docker_agent_sync,
skills filesystem ops). It does NOT call HTTP-endpoints — those require
user-auth, and bypassing them would create an auth-hole. The privileged
install path is its own boundary.

Dispatch table: see ``_HANDLERS`` (skill / plugin / mcp, install + uninstall).

Rules this module keeps (PR 0 of the extensions concept):

* **"All" stays "all".** ``cli_skills`` / ``cli_plugins`` / ``mcp_servers``
  ``None`` means "this agent loads everything". An install never turns that
  into ``[name]`` (that silently took every other item away). An uninstall
  from "all" is refused with a clear error instead of collapsing to ``[]``.
* **No automatic restart.** The install writes files; it never restarts an
  agent container (that killed running chat turns). The per-runtime result
  says ``needs_restart`` = "takes effect at next chat start".
* **One skills root.** Installed skills live in exactly one place: the
  central library ``plugin_manager._custom_skills_dir()`` (``~/.mc/skills``,
  resolved against ``HOME_HOST``) — the same folder
  ``sync_agent_skills_to_disk`` copies from. ADR-104 indexes that folder in
  place and explicitly rejects a second copy under the knowledge root; when
  the skills move there, ``_custom_skills_dir()`` is the one line to change.
* **Never overwrite a skill.** A name that already exists in the library is
  refused; the library is shared by every agent.
* **Pinned version.** A GitHub skill is fetched at an exact commit SHA (from
  the request payload, resolved at request time) and that SHA is logged.
* **Honest per-runtime result.** ``RUNTIME_APPLIERS`` maps the agent's
  runtime to an applier; the harness capability matrix
  (``harness_compat.HARNESS_CAPABILITIES``) decides whether the harness reads
  the item at all. Result: ``applied`` / ``needs_restart`` / ``not_applied``,
  stored in the install log's JSON detail (``previous_state["apply"]`` — the
  only JSON column on ``install_log``; no migration).
"""
import asyncio
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.agent import Agent
from app.models.approval import Approval
from app.models.install_log import InstallLog

logger = logging.getLogger(__name__)


@dataclass
class InstallResult:
    result: str  # "success" | "failed" | "rolled_back"
    error: str | None = None
    installed_version: str | None = None
    install_log_id: uuid.UUID | None = None
    #: Per-runtime outcome: {"runtime", "harness", "status", "note"} where
    #: status is "applied" | "needs_restart" | "not_applied". None when the
    #: install did not get that far.
    apply: dict[str, Any] | None = None


class SkillExistsError(RuntimeError):
    """A skill with this name already exists in the shared library."""


class AllItemsUninstallError(ValueError):
    """Uninstall asked on an agent that loads every item (allowlist None)."""


def _with_name(current: list[str] | None, name: str) -> list[str] | None:
    """Add ``name`` to an allowlist. ``None`` (= all) stays ``None``."""
    if current is None:
        return None
    items = list(current)
    if name not in items:
        items.append(name)
    return items


def _without_name(current: list[str] | None, name: str, field: str) -> list[str]:
    """Remove ``name`` from an allowlist. ``None`` (= all) cannot shrink by
    one name without listing everything else, so it is refused."""
    if current is None:
        raise AllItemsUninstallError(
            f"Agent loads all {field} (no allowlist); removing {name!r} alone "
            "would remove everything. Set an explicit list in the agent "
            "settings first."
        )
    return [x for x in current if x != name]


def _apply_detail(apply: Any) -> dict[str, Any] | None:
    return apply if isinstance(apply, dict) else None


class InstallExecutor:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def execute(self, approval: Approval) -> InstallResult:
        """Dispatch based on action_type. Writes InstallLog, returns result."""
        action = approval.action_type
        if action not in _HANDLERS:
            raise ValueError(f"Unsupported action_type: {action!r}")
        handler = _HANDLERS[action]
        try:
            return await handler(self, approval)
        except Exception as e:
            logger.exception("Install-Executor failed for approval %s", approval.id)
            log = await self._write_log(approval, result="failed", error=str(e))
            return InstallResult(result="failed", error=str(e), install_log_id=log.id)

    # ───────────────────── Skill handlers ─────────────────────

    async def _install_skill(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]
        source = payload.get("source", "")
        commit = payload.get("commit")

        agent = await self._get_agent(target_id)
        previous_state = {
            "cli_skills": list(agent.cli_skills) if agent.cli_skills is not None else None
        }

        # Files first: a refused or broken source never touches the agent row.
        info = await _call_skill_install(source, name, commit)
        info = info or {}
        installed_version = info.get("installed_version")
        created_dir = bool(info.get("created_dir"))

        try:
            agent.cli_skills = _with_name(agent.cli_skills, name)
            await self.session.commit()
        except BaseException:
            if created_dir:
                _remove_skill_dir(name)
            raise

        try:
            apply = await _trigger_sync_config(agent.id, "skill")
        except Exception as e:
            if created_dir:
                # We created this library folder in this run — take it back.
                _remove_skill_dir(name)
            agent.cli_skills = previous_state["cli_skills"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
                commit=info.get("commit"),
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        apply = _apply_detail(apply)
        log = await self._write_log(
            approval, result="success",
            installed_version=installed_version,
            previous_state=previous_state,
            apply=apply, commit=info.get("commit"),
        )
        return InstallResult(result="success", installed_version=installed_version,
                             install_log_id=log.id, apply=apply)

    async def _uninstall_skill(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]

        agent = await self._get_agent(target_id)
        previous_state = {
            "cli_skills": list(agent.cli_skills) if agent.cli_skills is not None else None
        }

        agent.cli_skills = _without_name(agent.cli_skills, name, "skills")
        await self.session.commit()

        try:
            apply = await _trigger_sync_config(agent.id, "skill")
        except Exception as e:
            agent.cli_skills = previous_state["cli_skills"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        apply = _apply_detail(apply)
        log = await self._write_log(
            approval, result="success", previous_state=previous_state, apply=apply,
        )
        return InstallResult(result="success", install_log_id=log.id, apply=apply)

    # ───────────────────── Plugin handlers ─────────────────────

    async def _install_plugin(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]
        source = payload.get("source", "")

        agent = await self._get_agent(target_id)
        previous_state = {
            "cli_plugins": list(agent.cli_plugins) if agent.cli_plugins is not None else None
        }

        agent.cli_plugins = _with_name(agent.cli_plugins, name)
        await self.session.commit()

        try:
            result = await _call_plugin_install(source, name)
            installed_version = result.get("installed_version") if result else None
            apply = await _trigger_sync_config(agent.id, "plugin")
        except Exception as e:
            agent.cli_plugins = previous_state["cli_plugins"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        apply = _apply_detail(apply)
        log = await self._write_log(
            approval, result="success",
            installed_version=installed_version,
            previous_state=previous_state, apply=apply,
        )
        return InstallResult(result="success", installed_version=installed_version,
                             install_log_id=log.id, apply=apply)

    async def _uninstall_plugin(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]

        agent = await self._get_agent(target_id)
        previous_state = {
            "cli_plugins": list(agent.cli_plugins) if agent.cli_plugins is not None else None
        }

        agent.cli_plugins = _without_name(agent.cli_plugins, name, "plugins")
        await self.session.commit()

        try:
            apply = await _trigger_sync_config(agent.id, "plugin")
        except Exception as e:
            agent.cli_plugins = previous_state["cli_plugins"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        apply = _apply_detail(apply)
        log = await self._write_log(
            approval, result="success", previous_state=previous_state, apply=apply,
        )
        return InstallResult(result="success", install_log_id=log.id, apply=apply)

    # ───────────────────── MCP handlers ─────────────────────

    async def _install_mcp(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]
        source = payload.get("source", "")

        agent = await self._get_agent(target_id)
        previous_state = {
            "mcp_servers": list(agent.mcp_servers) if agent.mcp_servers is not None else None
        }

        agent.mcp_servers = _with_name(agent.mcp_servers, name)
        await self.session.commit()

        proposed_config = payload.get("proposed_config")
        try:
            install_info = await _call_mcp_install(source, name, proposed_config)
            installed_version = install_info.get("installed_version") if install_info else None
            ok = await _call_mcp_smoke_test(name)
            if not ok:
                raise RuntimeError(
                    f"MCP smoke test failed for {name!r} — server did not respond "
                    "to JSON-RPC initialize/tools-list. For Python MCPs this "
                    "usually means runtime dependencies aren't installed (check "
                    "requirements.txt / pyproject.toml) or required env vars "
                    "(e.g. API keys) are missing."
                )
            apply = await _trigger_sync_config(agent.id, "mcp")
        except Exception as e:
            agent.mcp_servers = previous_state["mcp_servers"]
            await self.session.commit()
            try:
                await _call_mcp_uninstall(name)
            except Exception:
                pass
            # Always include exception type so empty-string errors (e.g. raw
            # ProcessLookupError) stay debuggable in install_log.
            err_text = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            log = await self._write_log(
                approval, result="rolled_back", error=err_text,
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=err_text,
                                 install_log_id=log.id)

        apply = _apply_detail(apply)
        log = await self._write_log(
            approval, result="success",
            installed_version=installed_version,
            previous_state=previous_state, apply=apply,
        )
        return InstallResult(result="success", installed_version=installed_version,
                             install_log_id=log.id, apply=apply)

    async def _uninstall_mcp(self, approval: Approval) -> InstallResult:
        payload = approval.payload or {}
        target_id = uuid.UUID(payload["target_agent_id"])
        name = payload["name"]

        agent = await self._get_agent(target_id)
        previous_state = {
            "mcp_servers": list(agent.mcp_servers) if agent.mcp_servers is not None else None
        }

        agent.mcp_servers = _without_name(agent.mcp_servers, name, "MCP servers")
        await self.session.commit()

        try:
            apply = await _trigger_sync_config(agent.id, "mcp")
        except Exception as e:
            agent.mcp_servers = previous_state["mcp_servers"]
            await self.session.commit()
            log = await self._write_log(
                approval, result="rolled_back", error=str(e),
                previous_state=previous_state,
            )
            return InstallResult(result="rolled_back", error=str(e),
                                 install_log_id=log.id)

        # Registry cleanup: if the MCP is orphaned after the unassign (no agent
        # references it anymore, and no agent has mcp_servers=None = "all
        # installed MCPs active"), we delete the registry directory. No rollback
        # if this fails — the agent allowlist cleanup already succeeded, but
        # leaving an orphaned dir is explicitly fine: the user only un-assigned
        # for this one agent.
        registry_cleanup_error: str | None = None
        try:
            if await self._mcp_is_orphaned(name):
                await _call_mcp_uninstall(name)
        except Exception as e:
            # Non-fatal: allowlist update + sync already went through.
            # Registry directory stays put, noted in the log.
            registry_cleanup_error = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
            logger.warning(
                "MCP %r registry cleanup failed after un-assign from agent %s: %s",
                name, agent.id, registry_cleanup_error,
            )

        apply = _apply_detail(apply)
        log = await self._write_log(
            approval, result="success", previous_state=previous_state,
            error=registry_cleanup_error, apply=apply,
        )
        return InstallResult(result="success", install_log_id=log.id, apply=apply)

    async def _mcp_is_orphaned(self, name: str) -> bool:
        """True if no agent references the MCP anymore.

        `mcp_servers = None` means "all installed MCPs active" — in this
        case the MCP is NOT orphaned, some agent still uses it implicitly.
        Only when all agents have explicit allowlists and `name` appears in
        none of them is the MCP truly free.
        """
        from app.models.agent import Agent
        result = await self.session.exec(select(Agent))
        for a in result.all():
            if a.mcp_servers is None:
                return False
            if name in a.mcp_servers:
                return False
        return True

    # ───────────────────── Helpers ─────────────────────

    async def _get_agent(self, agent_id: uuid.UUID) -> Agent:
        result = await self.session.exec(select(Agent).where(Agent.id == agent_id))
        agent = result.first()
        if agent is None:
            raise ValueError(f"Agent {agent_id} not found")
        return agent

    async def _write_log(
        self,
        approval: Approval,
        *,
        result: str,
        error: str | None = None,
        installed_version: str | None = None,
        previous_state: dict[str, Any] | None = None,
        apply: dict[str, Any] | None = None,
        commit: str | None = None,
    ) -> InstallLog:
        payload = approval.payload or {}
        # install_log has one JSON column. The rollback state stays under its
        # own keys; the per-runtime result and the pinned commit ride along
        # under reserved keys (no new column, no migration).
        detail: dict[str, Any] | None = dict(previous_state) if previous_state else None
        if apply is not None or commit:
            detail = detail or {}
            if apply is not None:
                detail["apply"] = apply
            if commit:
                detail["commit"] = commit
        log = InstallLog(
            approval_id=approval.id,
            requester_agent_id=uuid.UUID(payload["requester_agent_id"])
                if payload.get("requester_agent_id") else None,
            target_agent_id=uuid.UUID(payload["target_agent_id"]),
            action_type=approval.action_type,
            resource_name=payload.get("name", ""),
            source=payload.get("source"),
            result=result,
            error=error,
            installed_version=installed_version,
            previous_state=detail,
        )
        self.session.add(log)
        await self.session.commit()
        await self.session.refresh(log)
        return log


# Dispatch table
_HANDLERS: dict[str, Any] = {
    "install_skill": InstallExecutor._install_skill,
    "uninstall_skill": InstallExecutor._uninstall_skill,
    "install_plugin": InstallExecutor._install_plugin,
    "uninstall_plugin": InstallExecutor._uninstall_plugin,
    "install_mcp": InstallExecutor._install_mcp,
    "uninstall_mcp": InstallExecutor._uninstall_mcp,
}


# ───────────────────── Service-layer wrappers (patchable in tests) ─────────────────────

_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_BUNDLE_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_GIT_TIMEOUT_S = 120
#: ls-remote runs inside the agent's install request; keep it short.
_LS_REMOTE_TIMEOUT_S = 15


def skills_root() -> Path:
    """THE one root for installed skills (see module docstring)."""
    from app.services.plugin_manager import _custom_skills_dir
    return _custom_skills_dir()


def _remove_skill_dir(name: str) -> None:
    """Remove a library folder this install created (rollback only)."""
    import shutil
    target = skills_root() / name
    try:
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
    except OSError as e:  # rollback must not mask the original error
        logger.error("Rollback: could not remove skill dir %s: %s", target, e)


async def _git(*args: str, cwd: Path | None = None,
               timeout: float | None = None) -> str:
    """Run git non-interactively; return stdout or raise RuntimeError."""
    limit = timeout or _GIT_TIMEOUT_S
    import subprocess
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=str(cwd) if cwd else None, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=limit)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"git {args[0]} timed out after {limit:g}s")
    if proc.returncode != 0:
        raise RuntimeError(f"git {args[0]} failed: {err.decode(errors='replace').strip()}")
    return out.decode(errors="replace")


def github_url(source: str) -> str:
    return f"https://github.com/{source.removeprefix('github:')}.git"


def _parse_ls_remote(out: str) -> dict[str, str]:
    refs: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and _SHA_RE.match(parts[0]):
            refs[parts[1]] = parts[0]
    return refs


def _commit_of(refs: dict[str, str], name: str) -> str | None:
    """Commit a ref points to; an annotated tag is peeled to its commit."""
    return refs.get(f"{name}^{{}}") or refs.get(name)


def _is_own_ref(name: str) -> bool:
    # Only the repo's own branches and tags. refs/pull/* (and similar) carry
    # commits from anyone's fork.
    return name.startswith(("refs/heads/", "refs/tags/"))


async def resolve_skill_commit(source: str, ref: str | None) -> str:
    """Turn ``ref`` (branch, tag, full SHA or None = default branch) into the
    exact 40-hex commit SHA for a ``github:`` skill source.

    Called at request time so the operator approves one fixed version; the
    executor then installs exactly that commit.

    * A name matches **exactly** ``refs/heads/<ref>`` or ``refs/tags/<ref>``
      (``git ls-remote <url> <ref>`` alone tail-matches, so ``main`` would
      also hit ``aaa/main``). A branch and a tag of the same name that point
      to different commits are refused as ambiguous.
    * A full SHA is accepted only when a branch or tag of the allowlisted repo
      points to it. GitHub serves objects of the whole fork network through
      the parent URL, so an arbitrary SHA could come from anyone's fork.
    """
    url = github_url(source)
    refs = _parse_ls_remote(
        await _git("ls-remote", url, timeout=_LS_REMOTE_TIMEOUT_S)
    )

    if ref is None:
        sha = refs.get("HEAD")
        if not sha:
            raise RuntimeError(f"{source} has no default branch")
        return sha

    if _SHA_RE.match(ref.lower()):
        sha = ref.lower()
        own = {_commit_of(refs, n.removesuffix("^{}"))
               for n in refs if _is_own_ref(n)}
        if sha not in own:
            raise RuntimeError(
                f"commit {sha[:12]} is not the tip of a branch or tag of "
                f"{source}; pin by branch or tag name"
            )
        return sha

    if ref.startswith("refs/"):
        candidates = [ref] if _is_own_ref(ref) else []
    else:
        candidates = [f"refs/heads/{ref}", f"refs/tags/{ref}"]
    found = {c: _commit_of(refs, c) for c in candidates}
    shas = {v for v in found.values() if v}
    if not shas:
        raise RuntimeError(f"branch or tag {ref!r} not found in {source}")
    if len(shas) > 1:
        raise RuntimeError(
            f"ref {ref!r} is ambiguous in {source}: a branch and a tag of "
            "that name point to different commits; use refs/heads/... or "
            "refs/tags/..."
        )
    return shas.pop()


async def _fetch_commit(url: str, commit: str | None, dest: Path) -> str:
    """Fetch exactly ``commit`` (or the default branch when None, legacy
    requests) into ``dest`` and return the checked-out SHA."""
    dest.mkdir(parents=True)
    await _git("init", "-q", cwd=dest)
    await _git("remote", "add", "origin", url, cwd=dest)
    await _git("fetch", "-q", "--depth", "1", "origin", commit or "HEAD", cwd=dest)
    await _git("-c", "advice.detachedHead=false", "checkout", "-q", "FETCH_HEAD", cwd=dest)
    head = (await _git("rev-parse", "HEAD", cwd=dest)).strip()
    if commit and head != commit:
        raise RuntimeError(f"fetched {head} but {commit} was approved")
    return head


# ── Plain-text skills only (extensions concept, section 6) ────────────────
#
# A skill is plain text the agent reads. Anything that can run (scripts,
# binaries, executable bits, symlinks) is refused this round. The request
# check (routers/install_requests.py) and the install below use the SAME two
# helpers, and the install checks the folder it actually copies — so the
# check and the install can never drift apart.

#: File types that count as plain text in a skill folder.
TEXT_SUFFIXES = frozenset({".md", ".markdown", ".txt"})
#: Plain-text files that usually carry no suffix.
TEXT_NAMES = frozenset({"LICENSE", "LICENCE", "COPYING", "NOTICE"})
#: Size caps: one file / the whole skill. Larger is refused, never read in full.
MAX_FILE_BYTES = 1_000_000
MAX_SKILL_BYTES = 10_000_000


class SkillContentError(ValueError):
    """The skill holds something other than plain text, or no clean layout."""


def skill_source_dir(clone: Path, name: str) -> Path:
    """The requested skill inside a fetched repo: ``/SKILL.md``,
    ``/skills/<name>/SKILL.md`` or ``/<name>/SKILL.md`` — in that order.

    A layout counts only when its ``SKILL.md`` is a regular file and no part
    of the path is a symlink. A ``SKILL.md`` entry that exists but is not a
    regular file (a directory, a symlink) refuses the repo instead of being
    skipped, so no later layout can be picked by accident."""
    for parts in ((), ("skills", name), (name,)):
        candidate = clone.joinpath(*parts)
        md = candidate / "SKILL.md"
        walk = clone
        linked = False
        for part in (*parts, "SKILL.md"):
            walk = walk / part
            if walk.is_symlink():
                linked = True
                break
        if linked:
            raise SkillContentError(
                f"{'/'.join((*parts, 'SKILL.md'))}: symlinks are not allowed in a skill"
            )
        if not md.exists():
            continue
        if not md.is_file():
            raise SkillContentError(
                f"{'/'.join((*parts, 'SKILL.md'))} is not a plain file"
            )
        return candidate
    raise SkillContentError(
        f"No skill layout: none of /SKILL.md, /skills/{name}/SKILL.md, /{name}/SKILL.md"
    )


def text_only_files(folder: Path) -> list[str]:
    """Relative paths of every file in ``folder`` (``.git`` left out), or
    ``SkillContentError`` naming the files that are not plain text."""
    files: list[str] = []
    bad: list[str] = []
    total = 0
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if rel.parts and rel.parts[0] == ".git":
            continue
        name = rel.as_posix()
        if path.is_symlink():
            bad.append(f"{name} (symlink)")
            continue
        if path.is_dir():
            continue
        if path.suffix.lower() not in TEXT_SUFFIXES and path.name not in TEXT_NAMES:
            bad.append(name)
            continue
        st = path.stat()
        if st.st_mode & 0o111:
            bad.append(f"{name} (executable)")
            continue
        if st.st_size > MAX_FILE_BYTES:
            bad.append(f"{name} (too large: {st.st_size} bytes)")
            continue
        total += st.st_size
        if total > MAX_SKILL_BYTES:
            raise SkillContentError(
                f"Skill is too large: more than {MAX_SKILL_BYTES} bytes of text"
            )
        data = path.read_bytes()
        try:
            data.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(f"{name} (not UTF-8 text)")
            continue
        if b"\x00" in data:
            bad.append(f"{name} (binary)")
            continue
        files.append(name)
    if bad:
        shown = ", ".join(bad[:5]) + (f" and {len(bad) - 5} more" if len(bad) > 5 else "")
        raise SkillContentError(
            "Only plain-text skills (SKILL.md plus .md/.txt files) can be "
            f"requested; this one also contains: {shown}"
        )
    return files


async def _call_skill_install(
    source: str, name: str, commit: str | None = None,
) -> dict[str, Any]:
    """Install a skill into the one skills root.

    Returns ``{"installed_version", "commit", "created_dir"}``.

    Repo layouts (multi-skill monorepos, live bug 2026-04-24): the requested
    skill is taken from ``/SKILL.md``, ``/skills/<name>/SKILL.md`` or
    ``/<name>/SKILL.md``; the rest of the repo is discarded.

    A name that already exists in the library is refused
    (``SkillExistsError``) — the library is shared by every agent, so
    overwriting would silently replace another agent's skill.

    The root resolves against ``HOME_HOST`` (the backend runs as a different
    user inside its container).
    """
    import shutil
    import tempfile

    from app.services.install_allowlist import validate_skill_name
    validate_skill_name(name)
    skills_dir = skills_root() / name

    if source.startswith("github:"):
        if skills_dir.exists() or skills_dir.is_symlink():
            raise SkillExistsError(
                f"Skill {name!r} already exists in the skills library; "
                "refusing to overwrite it. Remove or rename the existing "
                "skill first."
            )
        with tempfile.TemporaryDirectory() as tmp:
            tmp_clone = Path(tmp) / "clone"
            head = await _fetch_commit(github_url(source), commit, tmp_clone)

            try:
                source_dir = skill_source_dir(tmp_clone, name)
            except SkillContentError as e:
                if not str(e).startswith("No skill layout"):
                    raise
                top = ", ".join(sorted(p.name for p in tmp_clone.iterdir()
                                       if p.name != ".git"))[:200]
                raise RuntimeError(
                    f"Repo {source} has no skill layout: none of "
                    f"/SKILL.md, /skills/{name}/SKILL.md, /{name}/SKILL.md. "
                    f"Top level: {top}"
                )
            # Same check as at request time, on exactly the folder we copy.
            text_only_files(source_dir)

            skills_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                skills_dir.mkdir()  # atomic: a concurrent install loses here
            except FileExistsError:
                raise SkillExistsError(
                    f"Skill {name!r} already exists in the skills library; "
                    "refusing to overwrite it."
                )
            try:
                shutil.copytree(source_dir, skills_dir, dirs_exist_ok=True,
                                ignore=shutil.ignore_patterns(".git"))
                version = None
                skill_md = skills_dir / "SKILL.md"
                if skill_md.exists():
                    for line in skill_md.read_text().splitlines()[:20]:
                        if line.startswith("version:"):
                            version = line.split(":", 1)[1].strip().strip('"\'')
                            break
            except BaseException:
                # This run created the folder: take it back, or every retry
                # is refused as "already exists".
                _remove_skill_dir(name)
                raise
        return {"installed_version": version or head[:12], "commit": head,
                "created_dir": True}
    elif source.startswith("bundle:"):
        # Imported .mcext skill (services/extension_bundle.py): copy the
        # staged files after re-checking their sha256; an existing name is
        # refused, never overwritten.
        from app.services.extension_bundle import install_staged_skill
        sha = source.removeprefix("bundle:")
        if not _BUNDLE_SHA_RE.match(sha):
            raise ValueError(f"Unsupported bundle source: {source!r}")
        return install_staged_skill(sha, name)
    elif source.startswith("~/.mc/skills/"):
        if source != f"~/.mc/skills/{name}":
            raise ValueError(f"Source {source!r} does not name the skill {name!r}")
        if not skills_dir.is_dir():
            raise FileNotFoundError(f"Local skill {name!r} not found at {skills_dir}")
        # The library folder may have changed since the request: check again.
        text_only_files(skills_dir)
        return {"installed_version": None, "commit": None, "created_dir": False}
    else:
        raise ValueError(f"Unsupported skill source: {source!r}")


async def _call_plugin_install(source: str, name: str) -> dict[str, Any]:
    """Delegate plugin install to CLI-Bridge via HTTP.

    MC has no local install logic for plugins — the shared cache
    (~/.mc/plugins/) is managed exclusively through the CLI Bridge
    (cli-bridge.py POST /plugins/install).

    source: marketplace name (e.g. "claude-plugins-official") — passed
            along as context, but the plugin_key (name) determines
            which plugin gets installed.

    Returns {"installed_version": str | None} — version from the
    shared cache after installation, or None if it can't be determined.
    """
    import asyncio

    def _sync_install() -> dict[str, Any]:
        from app.routers.cli_terminal import _bridge_post
        result = _bridge_post("/plugins/install", {"plugin_key": name}, timeout=130)
        if not result.get("ok"):
            raise RuntimeError(
                f"CLI-Bridge Plugin-Install fehlgeschlagen: {result.get('error', 'unknown')}"
            )
        return result

    result = await asyncio.get_event_loop().run_in_executor(None, _sync_install)

    # Try to read version from shared cache
    from app.services.plugin_manager import list_available_plugins
    installed_version: str | None = None
    for plugin in list_available_plugins():
        if plugin.key == name:
            installed_version = plugin.version if plugin.version != "unknown" else None
            break

    return {"installed_version": installed_version}


async def _call_mcp_install(
    source: str, name: str, proposed_config: dict | None = None,
) -> dict[str, Any]:
    """Install MCP via registry. Runs in thread executor because subprocess is sync."""
    import asyncio
    from app.services.mcp_registry import MCPRegistry

    def _sync_install():
        mgr = MCPRegistry()
        manifest = mgr.install(source, name, proposed_config=proposed_config)
        return {"installed_version": manifest.installed_version}

    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, _sync_install)


async def _call_mcp_smoke_test(name: str) -> bool:
    from app.services.mcp_registry import MCPRegistry
    return await MCPRegistry().smoke_test(name)


async def _call_mcp_uninstall(name: str) -> None:
    import asyncio
    from app.services.mcp_registry import MCPRegistry

    def _sync_uninstall():
        MCPRegistry().uninstall(name)

    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, _sync_uninstall)


# ───────────────────── Per-runtime apply registry ─────────────────────
#
# Which agent field a kind of item lives in, and which harness capability
# (harness_compat.HARNESS_CAPABILITIES) says the harness actually reads it.
# MCP rides on the same Claude-Code config consumer as plugins (.mcp.json next
# to settings.json); the matrix has no separate MCP column yet.
_KIND_CAPABILITY: dict[str, str] = {
    "skill": "cli_skills",
    "plugin": "cli_plugins",
    "mcp": "cli_plugins",
}

NEEDS_RESTART_NOTE = "takes effect at next chat start"

ApplyFn = Callable[[Any, Agent, str], Awaitable[dict[str, Any]]]


def _harness_of(agent: Agent) -> str:
    # Rows from before ADR-056 have no harness; docker agents back then were
    # all Claude-Code images.
    return getattr(agent, "harness", None) or "claude"


#: The sync_docker_agent_files result key that proves the item's file was
#: written. MCP is written by mcp_sync (checked separately).
_KIND_SYNC_KEY: dict[str, str] = {"skill": "skills", "plugin": "settings.json"}


def _sync_failure(results: Any, kind: str) -> str | None:
    """``sync_docker_agent_files`` never raises: it returns ``"error: ..."``
    / ``{"_error": ...}`` markers. Raise on an error (caller rolls back);
    return a note when the item's file was honestly skipped."""
    if not isinstance(results, dict):
        raise RuntimeError(f"docker file sync returned no result: {results!r}")
    if "_error" in results:
        raise RuntimeError(f"docker file sync error: {results['_error']}")
    key = _KIND_SYNC_KEY.get(kind)
    if key is None:
        return None
    value = results.get(key)
    if value is None:
        raise RuntimeError(
            f"docker file sync did not run the {key} step: {results}")
    if str(value).startswith("error"):
        raise RuntimeError(f"docker file sync {key} error: {value}")
    if key == "settings.json" and not str(value).startswith("written"):
        return f"settings.json not written: {value}"
    return None


async def _apply_docker(session: Any, agent: Agent, kind: str) -> dict[str, Any]:
    """Docker agents: render files into the claude-config bind mount. No
    container restart — a restart kills every running chat turn of the agent;
    the files are read when the next chat starts."""
    from app.services.docker_agent_sync import sync_docker_agent_files
    from app.services.harness_compat import capabilities_for

    harness = _harness_of(agent)
    results = await sync_docker_agent_files(session, agent)
    not_written = _sync_failure(results, kind)
    if not_written:
        return {"runtime": "cli-bridge", "harness": harness,
                "status": "not_applied", "note": not_written}
    from app.services.mcp_sync import sync_agent_mcp_to_disk
    try:
        sync_agent_mcp_to_disk(agent)
    except Exception as e:
        logger.warning("MCP sync failed for agent %s: %s", agent.id, e)
        if kind == "mcp":
            return {"runtime": "cli-bridge", "harness": harness,
                    "status": "not_applied", "note": f"MCP config sync failed: {e}"}

    caps = capabilities_for(harness)
    if not getattr(caps, _KIND_CAPABILITY[kind]):
        return {"runtime": "cli-bridge", "harness": harness, "status": "not_applied",
                "note": f"harness {harness} does not read MC-managed {kind} files"}
    return {"runtime": "cli-bridge", "harness": harness, "status": "needs_restart",
            "note": NEEDS_RESTART_NOTE}


async def _apply_host(session: Any, agent: Agent, kind: str) -> dict[str, Any]:
    """Host agents: the install path does not write their config files (their
    config dir does not follow the derived slug). Say so instead of logging a
    warning and reporting success."""
    return {"runtime": "host", "harness": getattr(agent, "harness", None),
            "status": "not_applied",
            "note": "host agent config files are not written by the install "
                    "path; regenerate them in the agent settings"}


#: agent_runtime -> applier. A runtime without an entry gets "not_applied".
#: "applied" (live without a restart) is reserved for a harness that reloads
#: its config in a running session; none does today.
RUNTIME_APPLIERS: dict[str, ApplyFn] = {
    "cli-bridge": _apply_docker,
    "host": _apply_host,
}


async def _trigger_sync_config(agent_id: uuid.UUID, kind: str = "skill") -> dict[str, Any]:
    """Write the agent's files for its runtime and report honestly.

    Returns ``{"runtime", "harness", "status", "note"}`` with status
    ``applied`` | ``needs_restart`` | ``not_applied``. Raises when the file
    sync itself fails (the caller rolls back). A Redis lock keeps at most one
    install per agent at a time.
    """
    from app.redis_client import get_redis
    from app.database import engine
    from sqlmodel.ext.asyncio.session import AsyncSession as _AsyncSession

    redis = await get_redis()
    lock_key = f"mc:agent:{agent_id}:install_lock"
    acquired = await redis.set(lock_key, "1", nx=True, ex=60)
    if not acquired:
        raise RuntimeError(f"Install lock busy for agent {agent_id}")
    try:
        async with _AsyncSession(engine, expire_on_commit=False) as session:
            agent = await session.get(Agent, agent_id)
            if agent is None:
                raise ValueError(f"Agent {agent_id} not found for sync-config")
            runtime = agent.agent_runtime
            applier = RUNTIME_APPLIERS.get(runtime)
            if applier is None:
                report = {"runtime": runtime, "harness": getattr(agent, "harness", None),
                          "status": "not_applied",
                          "note": f"runtime {runtime!r} has no install path"}
            else:
                report = await applier(session, agent, kind)
            logger.info("install apply for agent %s: %s", agent_id, report)
            return report
    finally:
        await redis.delete(lock_key)
