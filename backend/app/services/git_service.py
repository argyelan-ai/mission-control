"""
GitService — central service for all Git operations of the agents.

Uses gh/git CLI via asyncio subprocess. Called by Planner, Dispatch
and Review-Handoff.
"""

import asyncio
import hashlib
import logging
import os
import re

from app.services.github_config import require_github_owner, resolve_github_config

logger = logging.getLogger("mc.git")

ADHOC_REPO = "mc-workspace"


def slugify_project(name: str) -> str:
    """Convert a project name into a URL-safe slug."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def slugify_workspace_slug(title: str, max_len: int = 50) -> str:
    """Convert a task title into a capped workspace directory name.

    Short titles (slug <= max_len): no hash, direct pass-through.
    Long titles (slug > max_len): slug[:max_len-7] + "-" + sha256(title)[:6].
    Total length: exactly max_len characters. Hash is based on the full
    original title (deterministic, no filesystem check needed).
    """
    slug = slugify_project(title)
    if len(slug) <= max_len:
        return slug
    prefix_len = max_len - 7  # 43 chars + "-" + 6 hex = 50
    content_hash = hashlib.sha256(title.encode()).hexdigest()[:6]
    return slug[:prefix_len] + "-" + content_hash


class GitService:
    """Executes Git/GitHub operations via CLI."""

    def __init__(self) -> None:
        self._auth_token_hash: str | None = None
        self._token = ""

    async def _ensure_git_auth(self) -> None:
        """Configure Git HTTPS auth from the resolved GitHub token (vault > env).

        Re-runs whenever the token changes (rotation via Settings → GitHub):
        the credentials file is rewritten in place, no restart needed.
        """
        token = (await resolve_github_config()).token
        token_hash = hashlib.sha256(token.encode()).hexdigest() if token else ""
        if token_hash == self._auth_token_hash:
            return
        # Set early to prevent recursion via _run_cmd
        self._auth_token_hash = token_hash
        self._token = token
        if not token:
            return
        cred_path = os.path.join(os.path.expanduser("~"), ".git-credentials")
        with open(cred_path, "w") as f:
            f.write(f"https://x-access-token:{token}@github.com\n")
        os.chmod(cred_path, 0o600)
        await self._run_cmd("git", "config", "--global", "credential.helper", "store")
        await self._run_cmd("git", "config", "--global", "--add", "safe.directory", "*")
        await self._run_cmd("git", "config", "--global", "user.name", "Mission Control")
        await self._run_cmd("git", "config", "--global", "user.email", "mc@mc.local")
        await self._run_cmd("git", "config", "--global", "init.defaultBranch", "main")
        logger.info("Git HTTPS auth konfiguriert (Token-Quelle: vault/env)")

    async def _run_cmd(self, *args: str, cwd: str | None = None) -> str:
        """Execute a shell command, return stdout."""
        await self._ensure_git_auth()
        env = None
        if self._token:
            # A vault-set token must beat any stale GH_TOKEN in the process
            # env — gh reads GH_TOKEN/GITHUB_TOKEN before the credential store.
            env = {**os.environ, "GH_TOKEN": self._token, "GITHUB_TOKEN": self._token}
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            raise RuntimeError(f"Git command failed: {' '.join(args)} → {err}")
        return stdout.decode().strip()

    # ── Repo Creation ──────────────────────────────────────────────

    async def create_repo(self, repo_name: str, description: str = "") -> str:
        """Create GitHub repo (private). Returns: clone URL."""
        full_name = f"{await require_github_owner()}/{repo_name}"
        try:
            url = await self._run_cmd(
                "gh", "repo", "create", full_name,
                "--private",
                "--description", description or "",
                "--clone=false",
            )
            logger.info("GitHub-Repo erstellt: %s", full_name)
        except RuntimeError as e:
            if "already exists" in str(e).lower():
                logger.info("GitHub-Repo existiert bereits: %s", full_name)
            else:
                raise
        return f"https://github.com/{full_name}.git"

    async def init_repo_files(
        self, repo_name: str, project_type: str = "feature", readme_title: str = "",
    ) -> None:
        """Initial commit: push .gitignore + README + .mc-scratch/.gitkeep."""
        import tempfile

        full_name = f"{await require_github_owner()}/{repo_name}"
        gitignore = self._gitignore_for(project_type)

        with tempfile.TemporaryDirectory() as tmpdir:
            await self._run_cmd("git", "init", "-b", "main", tmpdir)
            await self._run_cmd(
                "git", "remote", "add", "origin",
                f"https://github.com/{full_name}.git", cwd=tmpdir,
            )
            # .gitignore
            with open(os.path.join(tmpdir, ".gitignore"), "w") as f:
                f.write(gitignore)
            # README
            with open(os.path.join(tmpdir, "README.md"), "w") as f:
                f.write(f"# {readme_title or repo_name}\n")
            # .mc-scratch/.gitkeep — keeps the scratch dir alive in clones
            scratch_dir = os.path.join(tmpdir, ".mc-scratch")
            os.makedirs(scratch_dir, exist_ok=True)
            with open(os.path.join(scratch_dir, ".gitkeep"), "w") as f:
                f.write(
                    "# Agent scratch space — `research/`, `experiments/`, "
                    "`reviews/`, `logs/`. Content here is gitignored.\n"
                )
            await self._run_cmd("git", "add", ".", cwd=tmpdir)
            await self._run_cmd(
                "git", "commit", "-m", "Initial commit", cwd=tmpdir,
            )
            await self._run_cmd("git", "push", "-u", "origin", "main", cwd=tmpdir)
        logger.info("Initial-Commit gepusht: %s", full_name)

    # ── Workspace Setup ──────────────────────────────────────────────

    async def ensure_workspace(
        self, workspace_path: str, repo_url: str, project_slug: str,
    ) -> str:
        """Clone or update repo in agent workspace. Returns: project dir."""
        project_dir = os.path.join(workspace_path, project_slug)

        if os.path.isdir(os.path.join(project_dir, ".git")):
            # Repo exists — pull main
            await self._run_cmd("git", "fetch", "origin", cwd=project_dir)
            await self._run_cmd("git", "checkout", "main", cwd=project_dir)
            await self._run_cmd("git", "pull", "origin", "main", cwd=project_dir)
            logger.info("Workspace aktualisiert: %s", project_dir)
        else:
            # Clone
            os.makedirs(workspace_path, exist_ok=True)
            await self._run_cmd("git", "clone", repo_url, project_dir)
            await self._run_cmd("git", "checkout", "main", cwd=project_dir)
            logger.info("Repo geklont: %s → %s", repo_url, project_dir)

        return project_dir

    async def create_task_branch(
        self, project_dir: str, task_slug: str,
    ) -> str:
        """Create and check out task branch. Returns: branch name."""
        branch = f"task/{task_slug}"
        await self._run_cmd("git", "checkout", "-b", branch, cwd=project_dir)
        logger.info("Branch erstellt: %s in %s", branch, project_dir)
        return branch

    async def setup_git_identity(
        self, project_dir: str, agent_name: str,
    ) -> None:
        """Set Git user.name and user.email in the repo."""
        await self._run_cmd(
            "git", "config", "user.name", f"{agent_name} (MC Agent)", cwd=project_dir,
        )
        await self._run_cmd(
            "git", "config", "user.email", f"{agent_name.lower()}@mc.local", cwd=project_dir,
        )

    async def ensure_adhoc_repo(self) -> str:
        """Create mc-workspace repo if it doesn't exist. Returns: clone URL."""
        return await self.create_repo(
            ADHOC_REPO,
            description="Mission Control — Ad-hoc Agent Tasks",
        )

    async def create_project_repo(self, project_slug: str, description: str = "") -> str:
        """Create a private GitHub repo for a project.

        Naming: mc-{slug}. ALWAYS private — no exceptions.
        Returns: clone URL
        """
        slug = slugify_project(project_slug)
        repo_name = f"mc-{slug}"
        clone_url = await self.create_repo(repo_name, description)
        await self.init_repo_files_with_briefing(repo_name, slug)
        return clone_url

    # ── Repo Registry (ADR-050) ─────────────────────────────────────

    async def list_github_repos(self, limit: int = 100) -> list[dict]:
        """List the GITHUB_OWNER account's repos via gh CLI.

        Returns normalized dicts: full_name, url, description, visibility,
        default_branch, is_archived, pushed_at.
        """
        import json

        owner = await require_github_owner()
        out = await self._run_cmd(
            "gh", "repo", "list", owner,
            "--limit", str(limit),
            "--json", "nameWithOwner,url,visibility,defaultBranchRef,description,isArchived,pushedAt",
        )
        repos = []
        for r in json.loads(out or "[]"):
            repos.append({
                "full_name": r.get("nameWithOwner", ""),
                "url": r.get("url", ""),
                "description": r.get("description") or None,
                "visibility": (r.get("visibility") or "private").lower(),
                "default_branch": (r.get("defaultBranchRef") or {}).get("name") or "main",
                "is_archived": bool(r.get("isArchived")),
                "pushed_at": r.get("pushedAt"),
            })
        return repos

    async def fetch_repo_meta(self, full_name: str) -> dict:
        """Fetch a single repo's metadata via gh CLI (for import/sync)."""
        import json

        out = await self._run_cmd(
            "gh", "repo", "view", full_name,
            "--json", "nameWithOwner,url,visibility,defaultBranchRef,description",
        )
        r = json.loads(out)
        return {
            "full_name": r.get("nameWithOwner", full_name),
            "url": r.get("url", f"https://github.com/{full_name}"),
            "description": r.get("description") or None,
            "visibility": (r.get("visibility") or "private").lower(),
            "default_branch": (r.get("defaultBranchRef") or {}).get("name") or "main",
        }

    async def init_repo_files_with_briefing(
        self, repo_name: str, project_slug: str,
    ) -> None:
        """Initial commit: push .gitignore + briefing.md to the repo."""
        import tempfile

        full_name = f"{await require_github_owner()}/{repo_name}"
        briefing_content = f"""# {project_slug} — Project Briefing

**Status:** active
**Created:** {__import__('datetime').datetime.utcnow().strftime('%Y-%m-%d')}

## Overview

_Wird automatisch aktualisiert wenn Deliverables registriert oder Phasen abgeschlossen werden._

## Phases

_Noch keine Phasen erstellt._

## Deliverables

_Noch keine Deliverables registriert._

## Revision History

_Keine Revisionen._
"""
        with tempfile.TemporaryDirectory() as tmpdir:
            await self._run_cmd("git", "init", "-b", "main", tmpdir)
            await self._run_cmd(
                "git", "remote", "add", "origin",
                f"https://github.com/{full_name}.git", cwd=tmpdir,
            )
            # Use the shared project-type-aware gitignore — default gives
            # us .mc-scratch/ protection and the standard secret excludes.
            with open(os.path.join(tmpdir, ".gitignore"), "w") as f:
                f.write(self._gitignore_for("feature"))
            with open(os.path.join(tmpdir, "briefing.md"), "w") as f:
                f.write(briefing_content)
            # .mc-scratch/.gitkeep — keeps scratch dir alive in clones.
            scratch_dir = os.path.join(tmpdir, ".mc-scratch")
            os.makedirs(scratch_dir, exist_ok=True)
            with open(os.path.join(scratch_dir, ".gitkeep"), "w") as f:
                f.write(
                    "# Agent scratch space — `research/`, `experiments/`, "
                    "`reviews/`, `logs/`. Content here is gitignored.\n"
                )
            await self._run_cmd("git", "add", ".", cwd=tmpdir)
            await self._run_cmd(
                "git", "commit", "-m", "init: project briefing and structure", cwd=tmpdir,
            )
            await self._run_cmd("git", "push", "-u", "origin", "main", cwd=tmpdir)
        logger.info("Project-Repo initialisiert: %s", full_name)

    async def create_phase_branch(
        self, project_dir: str, phase_slug: str,
    ) -> str:
        """Create and check out phase branch. Returns: branch name.

        Convention: phase/{slug}
        """
        branch = f"phase/{phase_slug}"
        # Check if branch already exists
        try:
            await self._run_cmd("git", "checkout", "-b", branch, cwd=project_dir)
        except RuntimeError as e:
            if "already exists" in str(e).lower():
                await self._run_cmd("git", "checkout", branch, cwd=project_dir)
            else:
                raise
        logger.info("Phase-Branch erstellt: %s in %s", branch, project_dir)
        return branch

    async def commit_deliverable(
        self,
        project_dir: str,
        phase_slug: str,
        filename: str,
        content: str,
        task_id: str,
        title: str,
    ) -> str:
        """Commit deliverable as a file. Returns: commit hash.

        Path convention: phases/{phase_slug}/deliverables/{filename}
        """
        rel_path = os.path.join("phases", phase_slug, "deliverables", filename)
        abs_path = os.path.join(project_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        await self._run_cmd("git", "add", rel_path, cwd=project_dir)
        commit_msg = f"deliverable: {title} [task/{task_id[:8]}]"
        await self._run_cmd("git", "commit", "-m", commit_msg, cwd=project_dir)

        # Read commit hash
        commit_hash = await self._run_cmd(
            "git", "rev-parse", "--short", "HEAD", cwd=project_dir,
        )
        logger.info("Deliverable committed: %s (%s)", title, commit_hash.strip())
        return commit_hash.strip()

    async def ensure_task_repo(self, task_title: str, task_id: str) -> str:
        """Create a dedicated private repo for a single task.

        Naming: mc-task-{slug}-{short_id} (max ~60 characters).
        Returns: clone URL.
        """
        slug = slugify_project(task_title)[:40]
        short_id = str(task_id).replace("-", "")[:8]
        repo_name = f"mc-task-{slug}-{short_id}"
        clone_url = await self.create_repo(
            repo_name,
            description=f"Mission Control — Task: {task_title}",
        )
        await self.init_repo_files(repo_name, project_type="feature", readme_title=task_title)
        return clone_url

    # ── Worktree Isolation (Bundle 4) ────────────────────────────────

    async def create_task_worktree(
        self,
        project_dir: str,
        task_slug: str,
        base_branch: str = "main",
        branch_name: str | None = None,
    ) -> str:
        """Create a Git worktree for a task.

        Creates an isolated worktree next to the main repo:
        project_dir/../../worktrees/{task_slug}/

        Args:
            project_dir: Path to the cloned main repo
            task_slug: Slug for branch and directory name
            base_branch: Base branch (default: main)
            branch_name: Optional branch name (default: task/{task_slug})

        Returns: Absolute path to the worktree directory
        Raises: RuntimeError if worktree creation fails
        """
        branch = branch_name or f"task/{task_slug}"
        # Worktrees next to the main repo: .../worktrees/task-slug/
        worktrees_dir = os.path.join(os.path.dirname(project_dir), "worktrees")
        worktree_path = os.path.join(worktrees_dir, task_slug)

        if os.path.isdir(worktree_path):
            logger.info("Worktree existiert bereits: %s", worktree_path)
            return worktree_path

        os.makedirs(worktrees_dir, exist_ok=True)

        # Make sure base_branch is up to date
        try:
            await self._run_cmd("git", "fetch", "origin", base_branch, cwd=project_dir)
        except RuntimeError:
            pass  # Fetch can fail when offline — try the worktree anyway

        # Create branch and check out worktree.
        # Try origin/base_branch, fall back to HEAD.
        try:
            await self._run_cmd(
                "git", "worktree", "add", "-b", branch,
                worktree_path, f"origin/{base_branch}",
                cwd=project_dir,
            )
        except RuntimeError as e:
            err_str = str(e).lower()
            if "already exists" in err_str:
                # Branch already exists — worktree with existing branch
                await self._run_cmd(
                    "git", "worktree", "add", worktree_path, branch,
                    cwd=project_dir,
                )
            elif "invalid reference" in err_str or "not a valid" in err_str:
                # origin/main doesn't exist — worktree from HEAD
                await self._run_cmd(
                    "git", "worktree", "add", "-b", branch,
                    worktree_path, "HEAD",
                    cwd=project_dir,
                )
            else:
                raise

        logger.info("Worktree erstellt: %s → branch %s", worktree_path, branch)
        return worktree_path

    async def cleanup_worktree(
        self, project_dir: str, worktree_path: str, *, keep_on_fail: bool = False,
    ) -> None:
        """Clean up worktree.

        Args:
            project_dir: Path to the main repo
            worktree_path: Path to the worktree
            keep_on_fail: True for failed tasks — remove worktree only from
                          the Git index, files remain for debugging purposes
        """
        if not os.path.isdir(worktree_path):
            return

        if keep_on_fail:
            # Remove only from the Git worktree index, files remain
            try:
                await self._run_cmd(
                    "git", "worktree", "remove", "--force", worktree_path,
                    cwd=project_dir,
                )
            except RuntimeError:
                pass  # Not critical — files remain, Git cleans up later
            logger.info("Worktree aus Index entfernt (Dateien bleiben): %s", worktree_path)
        else:
            # Full cleanup: remove worktree + files
            try:
                await self._run_cmd(
                    "git", "worktree", "remove", "--force", worktree_path,
                    cwd=project_dir,
                )
                logger.info("Worktree vollstaendig bereinigt: %s", worktree_path)
            except RuntimeError as e:
                logger.warning("Worktree-Cleanup fehlgeschlagen: %s — %s", worktree_path, e)

        # Git worktree prune (clean up orphaned entries)
        try:
            await self._run_cmd("git", "worktree", "prune", cwd=project_dir)
        except RuntimeError:
            pass

    # ── Commit Diff ──────────────────────────────────────────────────

    async def get_commit_diff(self, workspace_path: str, commit_hash: str) -> dict:
        """Structured Git diff for a commit.

        Returns: {hash, message, author, date, stats, files: [{filename,
        additions, deletions, hunks: [{header, lines: [{type, content,
        old_no, new_no}]}]}]}
        """
        import re

        # Commit metadata
        meta_raw = await self._run_cmd(
            "git", "log", "-1",
            "--pretty=format:%h\x1f%s\x1f%an\x1f%ar",
            commit_hash,
            cwd=workspace_path,
        )
        parts = meta_raw.split("\x1f", 3)
        h = parts[0] if len(parts) > 0 else commit_hash[:7]
        message = parts[1] if len(parts) > 1 else ""
        author = parts[2] if len(parts) > 2 else ""
        date = parts[3] if len(parts) > 3 else ""

        # Unified diff — empty --pretty=format: suppresses the commit header
        diff_raw = await self._run_cmd(
            "git", "show", commit_hash,
            "--unified=3",
            "--no-color",
            "--pretty=format:",
            cwd=workspace_path,
        )

        files: list = []
        current_file: dict | None = None
        current_hunk: dict | None = None
        old_line = 0
        new_line = 0

        for raw_line in diff_raw.splitlines():
            if raw_line.startswith("diff --git "):
                if current_hunk is not None and current_file is not None:
                    current_file["hunks"].append(current_hunk)
                    current_hunk = None
                if current_file is not None:
                    files.append(current_file)
                current_file = {"filename": "", "additions": 0, "deletions": 0, "hunks": []}

            elif raw_line.startswith("+++ b/") and current_file is not None:
                current_file["filename"] = raw_line[6:]

            elif raw_line.startswith("+++ /dev/null") and current_file is not None:
                current_file["filename"] = current_file.get("filename") or "(deleted)"

            elif raw_line.startswith(("--- ", "index ", "new file", "deleted file", "Binary files")):
                pass  # ignore

            elif raw_line.startswith("@@ ") and current_file is not None:
                if current_hunk is not None:
                    current_file["hunks"].append(current_hunk)
                m = re.match(r"@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", raw_line)
                if m:
                    old_line = int(m.group(1))
                    new_line = int(m.group(2))
                current_hunk = {"header": raw_line, "lines": []}

            elif current_hunk is not None and current_file is not None:
                if raw_line.startswith("+"):
                    current_hunk["lines"].append(
                        {"type": "add", "content": raw_line[1:], "old_no": None, "new_no": new_line}
                    )
                    current_file["additions"] += 1
                    new_line += 1
                elif raw_line.startswith("-"):
                    current_hunk["lines"].append(
                        {"type": "del", "content": raw_line[1:], "old_no": old_line, "new_no": None}
                    )
                    current_file["deletions"] += 1
                    old_line += 1
                elif raw_line.startswith(" "):
                    current_hunk["lines"].append(
                        {"type": "ctx", "content": raw_line[1:], "old_no": old_line, "new_no": new_line}
                    )
                    old_line += 1
                    new_line += 1

        # Finalize the last file/hunk
        if current_hunk is not None and current_file is not None:
            current_file["hunks"].append(current_hunk)
        if current_file is not None:
            files.append(current_file)

        return {
            "hash": h,
            "message": message,
            "author": author,
            "date": date,
            "stats": {
                "files": len(files),
                "additions": sum(f["additions"] for f in files),
                "deletions": sum(f["deletions"] for f in files),
            },
            "files": files,
        }

    # ── Review (PR) ──────────────────────────────────────────────────

    async def create_pr(
        self, project_dir: str, title: str, body: str, base: str = "main",
    ) -> str:
        """Create GitHub PR. Returns: PR URL."""
        url = await self._run_cmd(
            "gh", "pr", "create",
            "--title", title,
            "--body", body,
            "--base", base,
            cwd=project_dir,
        )
        logger.info("PR erstellt: %s", url)
        return url

    async def create_phase_pr(
        self,
        project_dir: str,
        phase_slug: str,
        title: str,
        body: str | None = None,
    ) -> str:
        """Open PR from phase/{slug} → main. Returns: PR URL.

        Pushes the branch first, then creates the PR via create_pr().
        """
        branch = f"phase/{phase_slug}"
        await self._run_cmd("git", "push", "-u", "origin", branch, cwd=project_dir)
        pr_body = body or f"Phase **{phase_slug}** abgeschlossen — Deliverables im Branch `{branch}`."
        url = await self.create_pr(
            project_dir=project_dir,
            title=title,
            body=pr_body,
            base="main",
        )
        logger.info("Phase-PR erstellt: %s (%s)", branch, url)
        return url

    async def create_git_tag(self, project_dir: str, tag_name: str) -> None:
        """Create and push an annotated Git tag."""
        await self._run_cmd(
            "git", "tag", "-a", tag_name, "-m", tag_name, cwd=project_dir,
        )
        await self._run_cmd("git", "push", "origin", tag_name, cwd=project_dir)
        logger.info("Git-Tag erstellt und gepusht: %s", tag_name)

    async def get_resume_briefing(self, project_dir: str) -> str:
        """Return the last 20 commits as a Markdown summary.

        Useful as context briefing for agents resuming work.
        Returns: Markdown string with commit list.
        """
        log = await self._run_cmd(
            "git", "log", "--oneline", "-20", cwd=project_dir,
        )
        if not log.strip():
            return "Keine Commits vorhanden."
        lines = log.strip().splitlines()
        items = "\n".join(f"- `{line.strip()}`" for line in lines if line.strip())
        return f"## Git-Verlauf (letzte {len(lines)} Commits)\n\n{items}\n"

    async def push_branch(self, project_dir: str, branch: str) -> None:
        """Push branch to GitHub."""
        await self._run_cmd("git", "push", "-u", "origin", branch, cwd=project_dir)

    async def merge_pr(self, project_dir: str, pr_number: int) -> None:
        """Squash-merge PR and delete branch."""
        await self._run_cmd(
            "gh", "pr", "merge", str(pr_number),
            "--squash", "--delete-branch",
            cwd=project_dir,
        )
        logger.info("PR #%d gemerged (squash)", pr_number)

    async def cleanup_task_worktree(
        self, project_dir: str, task_slug: str,
    ) -> bool:
        """Remove the git worktree of a completed task (Workstream B3).

        Call only after the task's PR has been merged — this function does
        not verify, it just removes. Layout matches `create_task_worktree`:
        worktree lives at `<parent-of-project>/worktrees/{task_slug}/`.
        Failures are non-fatal (orphan worktrees waste disk, block nothing).
        """
        import os
        worktrees_dir = os.path.join(os.path.dirname(project_dir), "worktrees")
        worktree_path = os.path.join(worktrees_dir, task_slug)
        if not os.path.isdir(worktree_path):
            return False
        try:
            await self._run_cmd(
                "git", "worktree", "remove", "--force", worktree_path,
                cwd=project_dir,
            )
            logger.info("Task worktree aufgeraeumt: %s", worktree_path)
            return True
        except Exception as e:
            logger.warning(
                "cleanup_task_worktree failed for %s: %s (non-fatal)",
                worktree_path, e,
            )
            return False

    # ── Branch Listing ────────────────────────────────────────────

    async def list_repo_branches(self, repo_name: str) -> list[str]:
        """List remote branches for a GitHub repo via gh CLI."""
        try:
            proc = await asyncio.create_subprocess_exec(
                "gh", "api", f"repos/{repo_name}/branches",
                "--jq", ".[].name",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await proc.communicate()
            if proc.returncode != 0:
                logger.warning("list_repo_branches failed for %s: %s", repo_name, stderr.decode())
                return []
            return [b.strip() for b in stdout.decode().strip().split("\n") if b.strip()]
        except Exception as e:
            logger.warning("list_repo_branches exception for %s: %s", repo_name, e)
            return []

    # ── Helpers ──────────────────────────────────────────────────────

    async def has_task_commits(self, workspace_path: str) -> bool:
        """Check whether there are commits in the workspace (branch has its own commits)."""
        try:
            result = await self._run_cmd("git", "log", "--oneline", "-1", cwd=workspace_path)
            return bool(result.strip())
        except Exception:
            return False

    async def get_task_git_info(self, workspace_path: str, branch_name: str | None = None) -> dict:
        """Git status for UI display: branch, commits, uncommitted, ahead, PR URL."""
        info: dict = {"commits": [], "pr_url": None}
        try:
            info["branch"] = (
                await self._run_cmd("git", "branch", "--show-current", cwd=workspace_path)
            ).strip()

            # Last 10 commits — filtered by branch_name if given
            log_args = ["git", "log"]
            if branch_name:
                log_args.append(branch_name)
            log_args += ["--pretty=format:%h\x1f%s\x1f%an\x1f%ar", "-10"]
            log_raw = (
                await self._run_cmd(*log_args, cwd=workspace_path)
            ).strip()
            commits = []
            if log_raw:
                for line in log_raw.splitlines():
                    parts = line.split("\x1f", 3)
                    if len(parts) == 4:
                        commits.append({
                            "hash": parts[0],
                            "message": parts[1],
                            "author": parts[2],
                            "date": parts[3],
                        })
            info["commits"] = commits
            info["last_commit"] = (
                f"{commits[0]['hash']} {commits[0]['message']}" if commits else ""
            )

            dirty = (
                await self._run_cmd("git", "status", "--porcelain", cwd=workspace_path)
            ).strip()
            info["uncommitted"] = bool(dirty)

            ahead_str = (
                await self._run_cmd("git", "rev-list", "--count", "main..HEAD", cwd=workspace_path)
            ).strip()
            info["ahead"] = int(ahead_str) if ahead_str.isdigit() else 0

            # PR URL via gh CLI (non-critical, no error if there's no PR)
            try:
                pr_url = (
                    await self._run_cmd(
                        "gh", "pr", "view", "--json", "url", "--jq", ".url",
                        cwd=workspace_path,
                    )
                ).strip()
                info["pr_url"] = pr_url if pr_url.startswith("http") else None
            except Exception:
                info["pr_url"] = None

        except Exception:
            pass
        return info

    def _gitignore_for(self, project_type: str) -> str:
        """Project-type-specific .gitignore template (Workstream B4).

        `.mc-scratch/` is always gitignored — agents dump research HTML,
        debug output, screenshots etc. there. `.mc-deliverables/` is NOT
        gitignored: those are the committable outputs.
        """
        base = (
            "# MC agent scratch — ephemeral working dir. `.gitkeep` is tracked\n"
            "# so the structure exists in fresh clones; everything else stays\n"
            "# local (research/, experiments/, reviews/, logs/).\n"
            ".mc-scratch/*\n"
            "!.mc-scratch/.gitkeep\n"
            "\n"
            "# Secrets — never commit\n"
            ".env\n"
            ".env.*\n"
            "*.pem\n"
            "*.key\n"
            "\n"
            "# Standard Python/Node/macOS ignores\n"
            "node_modules/\n"
            "dist/\n"
            "build/\n"
            ".DS_Store\n"
            "__pycache__/\n"
            "*.pyc\n"
            ".openclaw/\n"
            ".vercel/\n"
        )

        if project_type in ("website", "feature"):
            base += (
                "\n# Web framework outputs\n"
                ".next/\n"
                "out/\n"
                ".turbo/\n"
            )
        elif project_type == "research":
            base += (
                "\n# Research scratch — raw scrapes live in .mc-scratch/\n"
                "downloads/\n"
                "scraped/\n"
                "*.html\n"
                "*.csv\n"
            )
        elif project_type == "content":
            base += (
                "\n# Content drafts + design-tool backups\n"
                "drafts/\n"
                "*.psd\n"
                "*.ai\n"
                "*.fig.backup\n"
            )
        elif project_type == "visual":
            base += (
                "\n# Visual sources — finals are committed, sources stay local\n"
                "renders/\n"
                "sources/\n"
                "*.mp4\n"
                "!final/*.mp4\n"
            )

        return base


# Singleton
git_service = GitService()
