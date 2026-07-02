"""
GitService — Zentraler Service fuer alle Git-Operationen der Agents.

Nutzt gh/git CLI via asyncio subprocess. Wird von Planner, Dispatch
und Review-Handoff aufgerufen.
"""

import asyncio
import hashlib
import logging
import os
import re

logger = logging.getLogger("mc.git")

# GitHub owner (user or org) under which MC creates project repos.
# Required for the agent git workflow — set GITHUB_OWNER in .env.
GITHUB_OWNER = os.environ.get("GITHUB_OWNER", "")


def require_github_owner() -> str:
    """Fail fast with a clear message instead of building '/repo' slugs."""
    if not GITHUB_OWNER:
        raise RuntimeError(
            "GITHUB_OWNER is not configured — set it in .env (the GitHub "
            "user/org under which MC creates project repos)."
        )
    return GITHUB_OWNER
ADHOC_REPO = "mc-workspace"


def slugify_project(name: str) -> str:
    """Projektname in URL-tauglichen Slug umwandeln."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def slugify_workspace_slug(title: str, max_len: int = 50) -> str:
    """Task-Titel in gecappten Workspace-Verzeichnisnamen umwandeln.

    Kurze Titel (slug <= max_len): kein Hash, direktes pass-through.
    Lange Titel (slug > max_len): slug[:max_len-7] + "-" + sha256(title)[:6].
    Gesamtlaenge: genau max_len Zeichen. Hash basiert auf vollem Original-Titel
    (deterministisch, kein Filesystem-Check noetig).
    """
    slug = slugify_project(title)
    if len(slug) <= max_len:
        return slug
    prefix_len = max_len - 7  # 43 chars + "-" + 6 hex = 50
    content_hash = hashlib.sha256(title.encode()).hexdigest()[:6]
    return slug[:prefix_len] + "-" + content_hash


class GitService:
    """Fuehrt Git/GitHub-Operationen via CLI aus."""

    def __init__(self) -> None:
        self._configured = False

    async def _ensure_git_auth(self) -> None:
        """Git HTTPS-Auth via GH_TOKEN konfigurieren (fuer Docker)."""
        if self._configured:
            return
        self._configured = True  # Frueh setzen um Rekursion via _run_cmd zu verhindern
        token = os.environ.get("GH_TOKEN", "")
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
        logger.info("Git HTTPS auth konfiguriert via GH_TOKEN")

    async def _run_cmd(self, *args: str, cwd: str | None = None) -> str:
        """Shell-Befehl ausfuehren, stdout zurueckgeben."""
        await self._ensure_git_auth()
        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            err = stderr.decode().strip()
            raise RuntimeError(f"Git command failed: {' '.join(args)} → {err}")
        return stdout.decode().strip()

    # ── Repo-Erstellung ──────────────────────────────────────────────

    async def create_repo(self, repo_name: str, description: str = "") -> str:
        """GitHub-Repo erstellen (private). Returns: clone URL."""
        full_name = f"{require_github_owner()}/{repo_name}"
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
        """Initial-Commit: .gitignore + README + .mc-scratch/.gitkeep pushen."""
        import tempfile

        full_name = f"{require_github_owner()}/{repo_name}"
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

    # ── Workspace-Setup ──────────────────────────────────────────────

    async def ensure_workspace(
        self, workspace_path: str, repo_url: str, project_slug: str,
    ) -> str:
        """Repo in Agent-Workspace klonen oder updaten. Returns: project dir."""
        project_dir = os.path.join(workspace_path, project_slug)

        if os.path.isdir(os.path.join(project_dir, ".git")):
            # Repo existiert — auf main pullen
            await self._run_cmd("git", "fetch", "origin", cwd=project_dir)
            await self._run_cmd("git", "checkout", "main", cwd=project_dir)
            await self._run_cmd("git", "pull", "origin", "main", cwd=project_dir)
            logger.info("Workspace aktualisiert: %s", project_dir)
        else:
            # Klonen
            os.makedirs(workspace_path, exist_ok=True)
            await self._run_cmd("git", "clone", repo_url, project_dir)
            await self._run_cmd("git", "checkout", "main", cwd=project_dir)
            logger.info("Repo geklont: %s → %s", repo_url, project_dir)

        return project_dir

    async def create_task_branch(
        self, project_dir: str, task_slug: str,
    ) -> str:
        """Task-Branch erstellen und auschecken. Returns: branch name."""
        branch = f"task/{task_slug}"
        await self._run_cmd("git", "checkout", "-b", branch, cwd=project_dir)
        logger.info("Branch erstellt: %s in %s", branch, project_dir)
        return branch

    async def setup_git_identity(
        self, project_dir: str, agent_name: str,
    ) -> None:
        """Git user.name und user.email im Repo setzen."""
        await self._run_cmd(
            "git", "config", "user.name", f"{agent_name} (MC Agent)", cwd=project_dir,
        )
        await self._run_cmd(
            "git", "config", "user.email", f"{agent_name.lower()}@mc.local", cwd=project_dir,
        )

    async def ensure_adhoc_repo(self) -> str:
        """mc-workspace Repo erstellen falls es nicht existiert. Returns: clone URL."""
        return await self.create_repo(
            ADHOC_REPO,
            description="Mission Control — Ad-hoc Agent Tasks",
        )

    async def create_project_repo(self, project_slug: str, description: str = "") -> str:
        """Privates GitHub-Repo für ein Projekt erstellen.

        Naming: mc-{slug}. IMMER privat — keine Ausnahme.
        Returns: clone URL
        """
        slug = slugify_project(project_slug)
        repo_name = f"mc-{slug}"
        clone_url = await self.create_repo(repo_name, description)
        await self.init_repo_files_with_briefing(repo_name, slug)
        return clone_url

    async def init_repo_files_with_briefing(
        self, repo_name: str, project_slug: str,
    ) -> None:
        """Initial-Commit: .gitignore + briefing.md ins Repo pushen."""
        import tempfile

        full_name = f"{require_github_owner()}/{repo_name}"
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
        """Phase-Branch erstellen und auschecken. Returns: branch name.

        Convention: phase/{slug}
        """
        branch = f"phase/{phase_slug}"
        # Prüfen ob Branch bereits existiert
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
        """Deliverable als Datei committen. Returns: commit hash.

        Pfad-Convention: phases/{phase_slug}/deliverables/{filename}
        """
        rel_path = os.path.join("phases", phase_slug, "deliverables", filename)
        abs_path = os.path.join(project_dir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)

        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)

        await self._run_cmd("git", "add", rel_path, cwd=project_dir)
        commit_msg = f"deliverable: {title} [task/{task_id[:8]}]"
        await self._run_cmd("git", "commit", "-m", commit_msg, cwd=project_dir)

        # Commit-Hash lesen
        commit_hash = await self._run_cmd(
            "git", "rev-parse", "--short", "HEAD", cwd=project_dir,
        )
        logger.info("Deliverable committed: %s (%s)", title, commit_hash.strip())
        return commit_hash.strip()

    async def ensure_task_repo(self, task_title: str, task_id: str) -> str:
        """Dediziertes privates Repo für einen einzelnen Task erstellen.

        Naming: mc-task-{slug}-{short_id} (max ~60 Zeichen).
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
        """Git Worktree fuer einen Task erstellen.

        Erstellt einen isolierten Worktree neben dem Hauptrepo:
        project_dir/../../worktrees/{task_slug}/

        Args:
            project_dir: Pfad zum geklonten Hauptrepo
            task_slug: Slug fuer Branch und Verzeichnisname
            base_branch: Basis-Branch (default: main)
            branch_name: Optionaler Branch-Name (default: task/{task_slug})

        Returns: Absoluter Pfad zum Worktree-Verzeichnis
        Raises: RuntimeError wenn Worktree-Erstellung scheitert
        """
        branch = branch_name or f"task/{task_slug}"
        # Worktrees neben dem Hauptrepo: .../worktrees/task-slug/
        worktrees_dir = os.path.join(os.path.dirname(project_dir), "worktrees")
        worktree_path = os.path.join(worktrees_dir, task_slug)

        if os.path.isdir(worktree_path):
            logger.info("Worktree existiert bereits: %s", worktree_path)
            return worktree_path

        os.makedirs(worktrees_dir, exist_ok=True)

        # Sicherstellen dass der base_branch aktuell ist
        try:
            await self._run_cmd("git", "fetch", "origin", base_branch, cwd=project_dir)
        except RuntimeError:
            pass  # Fetch kann scheitern wenn offline — Worktree trotzdem versuchen

        # Branch erstellen und Worktree auschecken.
        # Versuche origin/base_branch, Fallback auf HEAD.
        try:
            await self._run_cmd(
                "git", "worktree", "add", "-b", branch,
                worktree_path, f"origin/{base_branch}",
                cwd=project_dir,
            )
        except RuntimeError as e:
            err_str = str(e).lower()
            if "already exists" in err_str:
                # Branch existiert schon — Worktree mit bestehendem Branch
                await self._run_cmd(
                    "git", "worktree", "add", worktree_path, branch,
                    cwd=project_dir,
                )
            elif "invalid reference" in err_str or "not a valid" in err_str:
                # origin/main existiert nicht — Worktree von HEAD
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
        """Worktree bereinigen.

        Args:
            project_dir: Pfad zum Hauptrepo
            worktree_path: Pfad zum Worktree
            keep_on_fail: True bei failed Tasks — Worktree nur aus Git-Index
                          entfernen, Dateien bleiben fuer Debug-Zwecke
        """
        if not os.path.isdir(worktree_path):
            return

        if keep_on_fail:
            # Nur aus Git-Worktree-Index entfernen, Dateien bleiben
            try:
                await self._run_cmd(
                    "git", "worktree", "remove", "--force", worktree_path,
                    cwd=project_dir,
                )
            except RuntimeError:
                pass  # Nicht kritisch — Dateien bleiben, Git bereinigt spaeter
            logger.info("Worktree aus Index entfernt (Dateien bleiben): %s", worktree_path)
        else:
            # Vollstaendiges Cleanup: Worktree + Dateien entfernen
            try:
                await self._run_cmd(
                    "git", "worktree", "remove", "--force", worktree_path,
                    cwd=project_dir,
                )
                logger.info("Worktree vollstaendig bereinigt: %s", worktree_path)
            except RuntimeError as e:
                logger.warning("Worktree-Cleanup fehlgeschlagen: %s — %s", worktree_path, e)

        # Git Worktree prune (orphaned entries bereinigen)
        try:
            await self._run_cmd("git", "worktree", "prune", cwd=project_dir)
        except RuntimeError:
            pass

    # ── Commit Diff ──────────────────────────────────────────────────

    async def get_commit_diff(self, workspace_path: str, commit_hash: str) -> dict:
        """Strukturierter Git-Diff fuer einen Commit.

        Returns: {hash, message, author, date, stats, files: [{filename,
        additions, deletions, hunks: [{header, lines: [{type, content,
        old_no, new_no}]}]}]}
        """
        import re

        # Commit-Metadaten
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

        # Unified diff — leeres --pretty=format: unterdrückt Commit-Header
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
                pass  # ignorieren

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

        # Letzte Datei/Hunk abschliessen
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
        """GitHub PR erstellen. Returns: PR URL."""
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
        """PR von phase/{slug} → main öffnen. Returns: PR URL.

        Pusht den Branch zuerst, erstellt dann den PR via create_pr().
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
        """Annotated Git-Tag erstellen und pushen."""
        await self._run_cmd(
            "git", "tag", "-a", tag_name, "-m", tag_name, cwd=project_dir,
        )
        await self._run_cmd("git", "push", "origin", tag_name, cwd=project_dir)
        logger.info("Git-Tag erstellt und gepusht: %s", tag_name)

    async def get_resume_briefing(self, project_dir: str) -> str:
        """Letzte 20 Commits als Markdown-Summary zurückgeben.

        Nützlich als Kontext-Briefing für Agents die eine Arbeit fortsetzen.
        Returns: Markdown-String mit Commit-Liste.
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
        """Branch auf GitHub pushen."""
        await self._run_cmd("git", "push", "-u", "origin", branch, cwd=project_dir)

    async def merge_pr(self, project_dir: str, pr_number: int) -> None:
        """PR squash-mergen und Branch loeschen."""
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
        """Prüft ob es Commits im Workspace gibt (Branch hat eigene Commits)."""
        try:
            result = await self._run_cmd("git", "log", "--oneline", "-1", cwd=workspace_path)
            return bool(result.strip())
        except Exception:
            return False

    async def get_task_git_info(self, workspace_path: str, branch_name: str | None = None) -> dict:
        """Git-Status für UI-Anzeige: Branch, Commits, uncommitted, ahead, PR-URL."""
        info: dict = {"commits": [], "pr_url": None}
        try:
            info["branch"] = (
                await self._run_cmd("git", "branch", "--show-current", cwd=workspace_path)
            ).strip()

            # Letzte 10 Commits — gefiltert nach branch_name wenn angegeben
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

            # PR-URL via gh CLI (non-critical, kein Fehler wenn kein PR)
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
