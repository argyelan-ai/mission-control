"""SQLite FTS5 index for the Markdown Vault.

Pattern adapted from llmwiki (Apache 2.0) — see NOTICE.

This module owns the .mc_index.db file living next to the vault. It is
authoritative for FTS5 search; the canonical truth remains the .md files
on disk. The index is fully rebuildable via rebuild_from_vault().
"""

import logging
import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterator

import frontmatter

from app.helpers.vault_frontmatter import parse_frontmatter, validate_frontmatter, FrontmatterError

logger = logging.getLogger("mc.vault_index")


class VaultIndex:
    def __init__(self, db_path: Path, vault_path: Path):
        self.db_path = db_path
        self.vault_path = vault_path
        self._con = sqlite3.connect(str(db_path), check_same_thread=False)
        self._con.row_factory = sqlite3.Row
        self._lock = threading.Lock()  # Serialize writes across watcher + compactor + admin threads
        migrated = self._ensure_schema()
        # After a destructive schema migration (DROP+CREATE), the index is empty
        # but the .md files on disk still exist. Auto-rebuild so the next process
        # restart doesn't require a manual POST /vault/_admin/rebuild.
        if migrated and self.vault_path.exists():
            stats = self.rebuild_from_vault()
            logger.info("vault_index: auto-rebuild after schema migration: %s", stats)

    # Columns the index expects. Any missing column triggers a DROP+CREATE
    # rebuild (FTS5 virtual tables don't support ALTER TABLE ADD COLUMN).
    _EXPECTED_COLS = frozenset({"path", "id", "agent", "type", "tags", "project", "title", "date", "content", "task"})

    def _ensure_schema(self) -> bool:
        """Returns True if a destructive schema migration was applied."""
        self._con.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS notes USING fts5(
                path UNINDEXED,
                id UNINDEXED,
                agent,
                type,
                tags,
                project,
                title UNINDEXED,
                date UNINDEXED,
                content,
                task UNINDEXED,
                tokenize = 'porter unicode61'
            );
        """)
        self._con.commit()
        # Schema migration: if the live table is missing ANY expected column,
        # nuke and recreate. The caller auto-rebuilds from disk afterwards.
        try:
            cols = {row[1] for row in self._con.execute("PRAGMA table_info(notes_data)")}
            if not self._EXPECTED_COLS.issubset(cols):
                self._con.executescript("""
                    DROP TABLE IF EXISTS notes;
                    CREATE VIRTUAL TABLE notes USING fts5(
                        path UNINDEXED,
                        id UNINDEXED,
                        agent,
                        type,
                        tags,
                        project,
                        title UNINDEXED,
                        date UNINDEXED,
                        content,
                        task UNINDEXED,
                        tokenize = 'porter unicode61'
                    );
                """)
                self._con.commit()
                return True
        except Exception:
            # notes_data doesn't exist yet (fresh DB) — schema is already correct.
            pass
        return False

    def upsert(self, file_path: Path, post: frontmatter.Post) -> None:
        with self._lock:
            self._upsert_locked(file_path, post)

    def _upsert_locked(self, file_path: Path, post: frontmatter.Post) -> None:
        """Inner upsert — caller must hold self._lock."""
        rel_path = str(file_path.relative_to(self.vault_path))
        meta = post.metadata
        tags = " ".join(meta.get("tags", []) or [])
        # Store date as the raw string the author wrote (ISO timestamp, plain
        # date, etc). Frontend formats it for display — we keep storage dumb.
        date_raw = meta.get("date") or meta.get("created_at") or ""
        date_str = str(date_raw) if date_raw else ""
        self._con.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
        # Phase E: persist `task` as an UNINDEXED FTS column so we can WHERE-
        # filter on it without polluting the full-text vocabulary. Empty
        # string ("") on notes without a task — the filter `WHERE task = ?`
        # just doesn't match. Stringified because FTS5 columns are text-only.
        task_raw = meta.get("task")
        task_str = str(task_raw) if task_raw else ""
        self._con.execute(
            "INSERT INTO notes (path, id, agent, type, tags, project, title, date, content, task) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                rel_path,
                str(meta.get("id", "")),
                meta.get("agent", "") or "",
                meta.get("type", "") or "",
                tags,
                meta.get("project", "") or "",
                meta.get("title", "") or "",
                date_str,
                post.content,
                task_str,
            ),
        )
        self._con.commit()

    _SELECT_COLS = "path, id, agent, type, tags, project, title, date, content, task"

    @staticmethod
    def _sanitize_fts_query(q: str) -> str:
        """Wrap each whitespace-separated token in double quotes so FTS5 treats
        them as literal phrases.

        Reason: FTS5 query syntax overloads several characters that show up in
        real-world agent queries:
          - ``-`` is the NOT operator
          - bare digits like ``16`` look like column references → OperationalError
          - ``:`` introduces column filters
          - ``"..."`` already denotes a phrase
        Quoting each token sidesteps the entire mini-DSL and gives us plain
        substring-style matching across all indexed columns, which is what
        agents and Voice actually want. Internal double-quotes get doubled
        (FTS5's escape convention).
        """
        tokens = (q or "").strip().split()
        if not tokens:
            return ""
        return " ".join('"' + t.replace('"', '""') + '"' for t in tokens)

    def search(
        self,
        query: str,
        agent: str | None = None,
        type: str | None = None,
        task: str | None = None,
        limit: int = 50,
    ) -> Iterator[dict[str, Any]]:
        safe_query = self._sanitize_fts_query(query)
        if not safe_query:
            return
        sql = f"SELECT {self._SELECT_COLS} FROM notes WHERE notes MATCH ?"
        params: list[Any] = [safe_query]
        if agent:
            sql += " AND agent = ?"
            params.append(agent)
        if type:
            sql += " AND type = ?"
            params.append(type)
        if task:
            sql += " AND task = ?"
            params.append(task)
        sql += " LIMIT ?"
        params.append(limit)
        for row in self._con.execute(sql, params):
            yield dict(row)

    def list_all(self, task: str | None = None) -> Iterator[dict[str, Any]]:
        # Newest first; rows with NULL/empty date sink to the bottom so the
        # list view starts with the most recent activity.
        sql = f"SELECT {self._SELECT_COLS} FROM notes"
        params: list[Any] = []
        if task:
            sql += " WHERE task = ?"
            params.append(task)
        sql += (
            " ORDER BY CASE WHEN date IS NULL OR date = '' THEN 1 ELSE 0 END, "
            "date DESC, path ASC"
        )
        for row in self._con.execute(sql, params):
            yield dict(row)

    def delete(self, rel_path: str) -> bool:
        """Remove a note row by vault-relative path. Returns True if a row
        was actually deleted, False if no match existed (already gone)."""
        with self._lock:
            cur = self._con.execute("DELETE FROM notes WHERE path = ?", (rel_path,))
            self._con.commit()
            return cur.rowcount > 0

    def find_backrefs(self, note_id: str, note_stem: str) -> list[dict[str, str]]:
        """Find notes whose body contains a [[…]] wikilink to this note.

        We check two reference forms because the writer permits both:
          - `[[<uuid>]]`   — id-based, canonical for cross-vault notes
          - `[[<stem>]]`   — stem-based, used inside a single agent's tree

        Returns minimal info (path/title/agent) so the caller can present a
        warning UI without a second round-trip per ref.

        LIKE-based scan over ~300 rows is fine; if the vault grows past 10k
        we'd switch to a wikilink edge table maintained on upsert.
        """
        refs: list[dict[str, str]] = []
        seen: set[str] = set()
        patterns: list[str] = []
        if note_id:
            patterns.append(f"%[[{note_id}]]%")
        if note_stem and note_stem != note_id:
            patterns.append(f"%[[{note_stem}]]%")
        for pat in patterns:
            for row in self._con.execute(
                "SELECT path, title, agent FROM notes WHERE content LIKE ?",
                (pat,),
            ):
                d = dict(row)
                if d["path"] in seen:
                    continue
                seen.add(d["path"])
                refs.append(d)
        return refs

    EXCLUDED_PREFIXES = ("_inbox/", "_conflicts/", "_rejected/", "_lint/", "_trash/", ".git/", ".obsidian/")

    def rebuild_from_vault(self) -> dict[str, int]:
        """Walk vault, re-index all .md files. Idempotent.

        Returns stats: {scanned, indexed, skipped, errors}.
        Files in _inbox/, _conflicts/, _rejected/, _lint/, .git/, .obsidian/
        are excluded.
        """
        with self._lock:
            stats = {"scanned": 0, "indexed": 0, "skipped": 0, "errors": 0}

            # Truncate existing rows first
            self._con.execute("DELETE FROM notes")
            self._con.commit()

            for md_file in self.vault_path.rglob("*.md"):
                rel = str(md_file.relative_to(self.vault_path))
                if any(rel.startswith(p) for p in self.EXCLUDED_PREFIXES):
                    stats["skipped"] += 1
                    continue
                stats["scanned"] += 1
                try:
                    post = parse_frontmatter(md_file)
                    validate_frontmatter(post.metadata)
                    self._upsert_locked(md_file, post)  # lock already held — no deadlock
                    stats["indexed"] += 1
                except FrontmatterError:
                    stats["errors"] += 1
                    continue

            return stats

    def close(self) -> None:
        self._con.close()
