# ADR-103 — MC keeps its own chat record

**Status:** Accepted (2026-11-18) · first slice (S1) of "chats instead of fixed agents" from ADR-102 · behind the flag `CHAT_RECORD_ENABLED` (default off)
**Datum:** 2026-11-18
**Scope:** Backend/DB · Backend/Services · Backend/API

## Kontext

ADR-102 makes the chat the unit of work instead of a fixed agent. Today MC
stores not a single word of a chat. The history page and the live stream read
the harness' own session files (`services/transcript_chat.py`,
`routers/agent_chat.py`), and "which session" is a guess: the newest file in
the agent's folder. When the model or the harness changes, the history in the
chat is gone, there is no search across chats, and a chat list (ROADMAP
stage 1, N4) would have to parse files on every request.

A harness-neutral event format already exists (`transcript_chat.py` module
docstring: message, tool, thinking, command, todo, usage, plus the omp extras).
It is produced on every read and thrown away.

## Entscheidung

1. **MC's database holds the canonical record of a chat; the harness keeps
   its native files for resume.** (Alternative C of the architecture study.)
   Three new tables, additive only (migration `0227_chat_record`):
   - `chats` — title, owner (`owner_user_id`, nullable), project (nullable),
     pair (`harness`, `runtime_id`, `model`), `permission_level`, `pinned`,
     `legacy_agent_id` (unique: one chat per imported agent), `next_seq`,
     `last_activity_at`, `archived_at`.
   - `chat_sessions` — one row per native harness session (`native_session_id`,
     `transcript_path`, `cwd`, `place`, `imported_bytes`, start/end).
   - `chat_events` — append-only, `seq` unique per chat, `source_key` unique
     per chat (dedupe), `ts`, `kind`, `role`, `payload` (the event dict of
     `transcript_chat.py` unchanged; JSONB on Postgres), `search_text`.
2. **Two writers, one key.** The live tailer (`ChatTailerManager`) hands each
   event it publishes to a background writer (`chat_record.LiveRecorder`);
   the importer reads whole files through the same `read_history` the history
   page uses. Both derive the same `source_key` (`tool:<toolUseId>`,
   `<entry uuid>:<kind>:<n>`), and every write is an upsert on
   `(chat_id, source_key)`. A tool card that gets its result later is the same
   key with a fuller payload. When a tailer starts it first backfills its file
   (the tailer joins at the end of the file).
3. **Importer** (`python -m app.services.chat_record_import`): one chat per
   agent, one chat session per native session file, files only read. Same
   gates as the chat: the adapter's privacy gate (`transcript_allowed`), no
   omp task sessions, each omp session once (the bridge's file). A file whose
   size has not changed since the last run is skipped.
4. **Record rules:** volatile events (`tool_output`, previews, pane state)
   are never stored. Tool `result` text is capped at 32 KB per event and
   message text at 128 KB (flagged `resultTruncated` / `textTruncated`); the
   full text stays in the native file. U+0000 is dropped (Postgres refuses it).
   No retention in this slice.
5. **Read API** (signed-in users, 404 `chat_record_disabled` while the flag is
   off): `GET /api/v1/chat-record/chats`, `GET /api/v1/chat-record/chats/{id}/history`
   (paging by event id), `GET /api/v1/chat-record/search?q=` (Postgres
   full-text, `simple` configuration — no stemming, because a chat mixes
   languages; GIN index). A chat without owner (all imported ones) is visible
   to every signed-in user, like the agent chats today; an owned chat to its
   owner and admins.
6. **Nothing else changes in this slice.** Agents, their processes, the
   history page and the frontend run exactly as before. No switch-over of the
   chat UI, no chat creation, no handoff.

## Alternativen

- **A — harness files stay canonical, MC only keeps a pointer table.** No
  harness-neutral record, so no switch with history and no search; rejected.
- **B — DB only, harness files disposable.** Every restart would re-feed the
  conversation into the model and pay tokens for it; rejected.
- **Reuse `threads` / `messages`.** They are the comms path (ADR-100: inbox,
  cursors, loop brake) and carry text only; chat events (tool payloads,
  hundreds per turn) would flood every inbox reader; rejected.
- **One chat per session file** on import instead of one per agent: floods
  the list with throwaway sessions; rejected (study §6.2, recommended answer).

## Konsequenzen

- **Measured** (copy of two real agents' folders, 2026-11-18, local Postgres
  16): 135 session files, 105.5 MB read → 2 chats, 135 sessions,
  29,801 events, 43 MB table; first run 6.8–14.4 s (warm / cold file cache),
  second run 0.3 s (all files unchanged); full-text query 1.4 ms; files
  byte-identical after the run. 13 events appeared in two session files of
  the same chat (resumed sessions) and are stored once.
- `usage` events are more than half of the rows (16,128 of 29,814 in the
  measurement). Retention or folding of `usage` is a later decision.
- Claude Code thinking stays unavailable (encrypted at the source); nothing
  new is lost.
- Two writers can drift (a missed line while nobody watched and no backfill
  ran). The importer and the backfill heal it idempotently; a nightly
  file↔DB count check is the next step.
- **Way back:** `CHAT_RECORD_ENABLED=false` (nothing is written or read),
  then `TRUNCATE chats CASCADE` or `alembic downgrade 0226_comms_guest_cards`.
  Harness files are never touched.

## Referenzen

- `backend/app/models/chat_record.py`, `backend/alembic/versions/0227_chat_record.py`
- `backend/app/services/chat_record.py`, `backend/app/services/chat_record_import.py`
- `backend/app/routers/chat_record.py`; hook in `backend/app/services/transcript_chat.py` (`ChatTailerManager._run`)
- `backend/tests/test_chat_record.py`
- ADR-102 (direction), ADR-100 (comms tables stay separate), ADR-086 (pairs)
