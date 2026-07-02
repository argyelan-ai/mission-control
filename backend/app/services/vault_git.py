"""Git subprocess wrapper for the vault.

Phase 2 mode: real git add/commit with 30s batching.
M.1 stub mode preserved for backward compatibility.

stage() adds files to the git index. commit_batched() creates a
single commit from all staged files with a descriptive message.
ensure_git() initializes the vault as a git repo if needed.
"""

import logging
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger("mc.vault_git")

_VAULT_GITIGNORE = """\
_inbox/
_rejected/
_conflicts/
_trash/
_lint/
.mc_index.db
.obsidian/
"""


class VaultGit:
    def __init__(self, vault_path: Path, stub_mode: bool = True):
        self.vault_path = vault_path
        self.stub_mode = stub_mode
        self._staged: list[Path] = []
        self._lock = threading.Lock()

    def ensure_git(self) -> None:
        """Initialize vault as a git repo if .git/ doesn't exist."""
        git_dir = self.vault_path / ".git"
        if git_dir.exists():
            return

        logger.info("Initializing git repo in %s", self.vault_path)
        subprocess.run(
            ["git", "init", str(self.vault_path)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.vault_path), "config", "user.email", "vault@mc.local"],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "-C", str(self.vault_path), "config", "user.name", "MC Vault"],
            check=True,
            capture_output=True,
        )

        # Write .gitignore if it doesn't exist
        gitignore = self.vault_path / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_VAULT_GITIGNORE)
            subprocess.run(
                ["git", "-C", str(self.vault_path), "add", ".gitignore"],
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "-C", str(self.vault_path), "commit", "-m", "vault: init"],
                check=True,
                capture_output=True,
            )

    def stage(self, file_path: Path) -> None:
        """Stage a file for the next batched commit.

        Validates that ALL paths (absolute and relative) resolve within the vault.
        Relative paths are resolved against vault_path before the check.
        """
        # Validate path is within vault — resolve relative paths against vault_path
        if file_path.is_absolute():
            resolved = file_path.resolve()
        else:
            resolved = (self.vault_path / file_path).resolve()
        try:
            resolved.relative_to(self.vault_path.resolve())
        except ValueError as e:
            raise ValueError(f"file {file_path} outside vault {self.vault_path}") from e

        if self.stub_mode:
            rel = file_path.relative_to(self.vault_path) if file_path.is_absolute() else file_path
            logger.info("STUB stage: %s", rel)
            with self._lock:
                self._staged.append(file_path)
            return

        rel = file_path.relative_to(self.vault_path) if file_path.is_absolute() else file_path
        try:
            subprocess.run(
                ["git", "-C", str(self.vault_path), "add", str(rel)],
                check=True,
                capture_output=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error("git add failed for %s: %s", rel, e.stderr.decode() if e.stderr else e)
            raise

        with self._lock:
            self._staged.append(file_path)
        logger.debug("Staged: %s", rel)

    def commit_batched(self, author_slug: str, message_hint: str) -> bool:
        """Commit all staged files with a descriptive message.

        Returns True if a commit was created, False if nothing to commit.

        Message format: vault: {message_hint} by {author_slug}
        Author: MC Vault <vault@mc.local>
        """
        with self._lock:
            if self.stub_mode:
                logger.info(
                    "STUB commit_batched: author=%s hint=%s (%d staged)",
                    author_slug,
                    message_hint,
                    len(self._staged),
                )
                self._staged.clear()
                return False

            if not self._staged:
                logger.debug("commit_batched: nothing staged, skipping")
                return False

            count = len(self._staged)
            message = f"vault: {message_hint} by {author_slug} ({count} file{'s' if count != 1 else ''})"

            try:
                result = subprocess.run(
                    [
                        "git", "-C", str(self.vault_path),
                        "commit",
                        "--author", "MC Vault <vault@mc.local>",
                        "-m", message,
                    ],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    if "nothing to commit" in (result.stdout + result.stderr):
                        logger.debug("commit_batched: nothing to commit (already committed)")
                        self._staged.clear()
                        return False
                    logger.error("git commit failed: %s", result.stderr)
                    return False
            except subprocess.CalledProcessError as e:
                logger.error("git commit failed: %s", e.stderr if e.stderr else e)
                return False

            self._staged.clear()
            logger.info("Committed: %s", message)
            return True

    async def flush_if_pending(self, author_slug: str = "system") -> bool:
        """Async-safe flush: commit any pending staged files.

        Called by the 30s batch timer and on shutdown.
        """
        with self._lock:
            if not self._staged:
                return False
            count = len(self._staged)
        return self.commit_batched(author_slug, f"batched {count} changes")
