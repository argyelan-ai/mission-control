# ADR-104 — Knowledge in one place: one root, three drawers, files first

**Status:** Accepted (2026-11-19) · first slice of "knowledge in one place" (ADR-102 principle 6) · behind the flag `KNOWLEDGE_ROOT_ENABLED` (default off)
**Datum:** 2026-11-19
**Scope:** Backend/DB · Backend/Services · Backend/API

## Kontext

ADR-102 replaces fixed agents with chats and the task page with projects.
Knowledge today is spread over many places: the vault (`~/.mc/vault`,
Markdown, sorted by **agent**), the legacy table `board_memory`, the skills
folder (`~/.mc/skills`), repo rules in `repos.rules_md`, the project record
(`projects.briefing_doc`, `plan_summary`), reference files, deliverables and
agent workspaces. None of them knows "project" the way a chat product does:
the vault sorts by agent (`agents/<slug>/`), and its watcher only checks path
ownership under `agents/` (`services/vault_watcher.py`).

The vault already shows the pattern that works: Markdown is the truth, the
search index can be rebuilt from the files (ADR-034).

## Entscheidung

1. **One root with three drawers, files are the truth.**
   `~/.mc/knowledge/` holds `notes/` (facts), `skills/<name>/SKILL.md` and
   `projects/<slug>/` (`PROJECT.md`, `knowledge/`, machine-owned `.mc/`).
   Anything a person would call knowledge is a file there; it can be backed
   up by copying a folder, read with any editor and grepped by any harness.
2. **The database is only an index.** New table `knowledge_entries`
   (migration `0228_knowledge_root`): source, path, drawer, project slug/id,
   kind, title, tags, size, mtime, content hash, capped search text
   (64 KB), problem. Postgres full-text with the `simple` configuration and a
   GIN index (notes mix German and English, like ADR-103). The body is always
   read from the file. The table can be emptied and rebuilt at any time.
3. **One folder per project.** A sweep creates `projects/<slug>/` for every
   `projects` row with `PROJECT.md` rendered from the row (name, description,
   status, repository, working folder, briefing, plan, repository rules) and
   `.mc/project.json` (id → slug, hash of the last render). The slug is fixed
   when the folder is made — a rename changes the title, never the folder; a
   clash gets a short id suffix. MC re-renders `PROJECT.md` only while the
   file is still exactly its last render; once the operator edits it, MC
   never writes it again. A project deleted in the DB keeps its folder.
4. **Existing sources are read in place, never written.** In this slice the
   index also covers the vault (without `_*` folders, `attachments/`,
   `jobs/`, `runs/`) and the skills folder (without `_*` folders), both only
   read. A vault note whose frontmatter `project` names a project folder is
   listed with that project. Nothing is moved or copied; the move of the vault
   is a later slice.
5. **Ownership rule for projects.** Under `projects/<slug>/` a frontmatter
   `project` that names another project flags the file
   (`problem = project_mismatch`): it is not listed in that project and is
   counted in the status. The file is not moved. Symlinks that leave their
   folder are not indexed.
6. **When the index is written.** `services/knowledge_import.sync_all`:
   background sweep every `KNOWLEDGE_ROOT_SYNC_INTERVAL` seconds (default
   300), `POST /api/v1/knowledge-root/_admin/sync`, or
   `python -m app.services.knowledge_import` (`--dry-run` only counts). An
   unchanged file (same size and mtime) costs one `stat`.
7. **Read API** (admin only, like the vault reads; 404
   `knowledge_root_disabled` while the flag is off): `GET
   /api/v1/knowledge-root/status`, `/projects`, `/entries` (filter by drawer,
   project, source, `problems`), `/entries/{id}` (body read from the file now,
   `stale` when it changed since the sweep, 410 when it is gone), `/search`.
   The prefix is not `/knowledge` because that is the legacy `board_memory`
   API, which stays.
8. **Nothing else changes in this slice.** No screen reads the new API yet,
   the vault, its watcher and its index run as before, `board_memory` keeps
   its writers, no chat or harness gets project knowledge yet.

## Alternativen

- **B — DB first, files as an export.** Repeats the `board_memory` story that
  ADR-034 reversed; harnesses cannot read a database; rejected.
- **C — DB truth for metadata, files for bodies.** Two truths per note (the
  drift ADR-050 removed for repos); rejected.
- **Copy the vault and the skills into the new root now.** Two copies of the
  same note while the vault is still written by agents; rejected for this
  slice — the move with a symlink is its own slice.
- **Extend the vault's SQLite index instead of a new table.** It is owned by
  the vault watcher and sorted by agent; a second root there needs the watcher
  rules first. A separate index keeps this slice additive.

## Konsequenzen

- **Measured** (real vault and skills folder, only read, 2026-11-19, SQLite
  in memory): 1,924 files indexed (1,894 vault notes, 30 skills) in 0.55 s;
  second sweep 0.11 s, all unchanged; vault `git status` and every file's
  size and mtime identical before and after.
- The index holds up to 64 KB of text per note, so note content is now also
  in the MC database and its backups — the same notes the vault already holds
  on disk. Readable by admins only.
- The vault and the index can disagree for one sweep interval.
- Later slices: Knowledge page column "Projects / Notes / Skills" and a
  `knowledge` root on the Files page (frontend), chats grouped by project,
  `PROJECT.md` into the harness's instruction file, chat export, task history
  export, the vault move, dropping `board_memory`.
- **Way back:** `KNOWLEDGE_ROOT_ENABLED=false` (nothing written or read), then
  `TRUNCATE knowledge_entries` or `alembic downgrade 0227_chat_record`. The
  folders under `~/.mc/knowledge` stay until deleted by hand; the vault and
  the skills folder were never touched.

## Referenzen

- `backend/app/models/knowledge_entry.py`, `backend/alembic/versions/0228_knowledge_root.py`
- `backend/app/services/knowledge.py`, `backend/app/services/knowledge_import.py`
- `backend/app/routers/knowledge_root.py`, `backend/app/background.py` (`knowledge_sync`), `backend/app/config.py`
- `backend/tests/test_knowledge_root.py`
- ADR-102 (direction), ADR-034 (vault: files first), ADR-103 (chat record, same flag and index pattern)
