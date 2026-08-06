# Knowledge

An agent forgets everything between sessions. Anything it should still know next week has to be written down somewhere durable, and that somewhere is a folder of Markdown files on your disk. Mission Control's knowledge layer is a **vault** — plain `.md` notes under `~/.mc/vault/`, owned by the agents that write them, indexed for keyword search and embedded for semantic search, versioned in git. The database and the vector store are both derived: lose either one and you rebuild it from the files. On top of that sits the loop that makes the whole thing worth having — every agent reflects before closing a task, the lesson is extracted and indexed, and the next similar dispatch carries it back in the briefing.

## The vault is the source of truth

Before [ADR-034](../../decisions/034-vault-as-source-of-truth.md), all knowledge lived in a PostgreSQL table and agents could only write to it through a REST call. That made every insight dependent on the agent remembering to make an API call at the right moment — and context compaction or a container restart destroyed anything not yet saved.

Now the filesystem is primary:

```
~/.mc/vault/
  agents/<slug>/      each agent's own writing space — it owns this directory
  _inbox/<target>/    cross-agent notes: you write into someone's inbox, never into their folder
  .mc_index.db        SQLite FTS5 index — derived, rebuildable
```

The backend is a watcher and a compactor, not the primary store. A file watcher validates and indexes new or changed notes and quarantines invalid ones; a git service commits and pushes after writes; an embeddings service pushes vectors to Qdrant. Agents write either directly to the filesystem (with `vault:write`) or through `POST /agent/vault/note`.

The design rule behind the layout: **an agent owns its own directory and never writes into another agent's folder.** Cross-agent knowledge goes through the inbox and is compacted from there.

Everything downstream is fail-soft. If the embedding host or Qdrant is unreachable, the note is still written and indexed, search falls back to keywords, and the frontend shows a badge saying so. Nothing is lost; only ranking degrades.

## Hybrid search: FTS5 plus vectors

Two retrieval paths run over the same notes:

| Path | Store | Good at |
|---|---|---|
| Full-text | SQLite FTS5 (`.mc_index.db`) | Exact terms, names, error strings, filters by agent / type / task |
| Semantic | Qdrant (`memory_vault` collection) | "Something about deployment rollbacks", phrasing you don't remember |

FTS5 was chosen over scanning Markdown directly because unindexed search is O(n) per query, and the index rebuilds from the vault in about a second for typical sizes. That is the whole trade: the index is disposable, the files are not.

Alongside `memory_vault`, the older **3-layer memory** collections still exist for `board_memory` rows — `memory_semantic` (knowledge, decisions, concepts, references, research), `memory_agent` (per-agent lessons), and `memory_episodic` (journal, weekly reviews, insights, with a recency boost: 30-day linear decay, up to 25 % score lift). Dispatch context retrieval draws the top semantic and top agent-layer matches above a score threshold.

## Board memory: one table, three scopes

The `board_memory` table predates the vault and is now largely superseded by it, but it is still the model behind the **Memory** page's board and knowledge views, so its scoping rule is worth knowing ([ADR-004](../../decisions/004-board-memory-unified.md)). One table, three scopes, expressed as nullable columns:

| `board_id` | `agent_id` | Scope | Who sees it |
|---|---|---|---|
| set | null | Board memory | Every agent on that board |
| set | set | Agent knowledge | Only that agent |
| null | null | Global knowledge | Everyone, plus the UI |

Memory types: `knowledge`, `decision`, `lesson`, `reference`, `journal`, `concept`, `weekly_review`, `insight`, `research`.

The reason it is one table rather than three: promoting a board insight to global knowledge is `UPDATE SET board_id = NULL` instead of a copy-and-move, and a dispatch can load board memory, agent lessons and global knowledge in a single pass. The cost is that scope filters live in queries rather than in the schema — get one wrong and you leak another agent's private notes.

## Agent lessons and the reflection loop

This is the part that compounds, and it is the reason the mandatory reflection exists ([ADR-023](../../decisions/023-review-policy-trust-by-default.md), [ADR-021](../../decisions/021-agent-personas.md)):

1. Before closing a task, the agent must post a reflection comment with four required fields — what was done, what worked, what was unclear, and the lesson for its memory — with a minimum length. The enforcement checks that such a comment *exists* on the task, not that it is the last one, so later progress updates don't break it.
2. The lesson is extracted as an agent-scoped memory entry.
3. It is indexed for retrieval.
4. The next dispatch to that agent on a similar task carries it back in the briefing.

Board leads are exempt — they coordinate rather than implement. If **you** close a task manually from the UI, no reflection is required; the backend treats that as a deliberate opt-out of the learning loop.

The field names, the minimum length and the charter live in `backend/app/constants.py`, shared by the SOUL template, the enforcement code and the error messages, so changing the rules is a single edit.

## Insights

The intelligence service is a singleton loop (same pattern as the watchdog: asyncio task, Redis lock, configurable interval) that runs rule-based analyses in parallel — task durations, agent performance, failure patterns via keyword matching, anomalies — and takes hourly agent-metric snapshots. Results are cached in Redis and surfaced on the **Insights** page as KPIs, agent performance, task duration distribution, error patterns and anomalies.

Optionally, a daily LLM distillation writes a report back into memory as an `insight` entry with `auto_generated=True` — the system summarising its own week.

## Cost and token tracking

Token accounting does not come from an API you have to configure. A **token harvester** reads the transcript files the harnesses already write — Claude Code JSONL, `omp` session logs, the Grok CLI's structured log, Hermes's session ledger — and turns them into `model_usage_events`, including cache-token splits. It is idempotent and resumable: it dedupes on stable per-line identifiers and stores a read offset, so re-running over the same files changes nothing.

The cost collector then evaluates daily and monthly budget thresholds on that data and emits warnings. Read the number for what it is: `cost_usd` is a **list-price equivalent** computed from a price table. On a Claude Pro/Max subscription nothing is billed per token — the warning exists to catch runaway consumption, such as an agent stuck in a loop, not to reconstruct an invoice. Token counts for thresholds sum input, output and cache *writes*; cache reads are excluded, because they dominate raw volume at a fraction of the price and would make the threshold meaningless.

## Where to look in the UI

**Memory** is the vault: a list view and a force-directed graph of notes, wikilinks and similarity edges, with scope filters and a search bar spanning both retrieval paths. **Files** browses the workspace roots — deliverables agents produced, reference files you uploaded, and other file roots. **Insights** is the analytics view described above.

## Related

- [Boards and tasks](boards-and-tasks.md) — reference files vs. deliverables
- [Agents and souls](agents-and-souls.md) — how knowledge reaches an agent's prompt
- [Scopes and security](scopes-and-security.md) — `vault:*`, `memory:*` and `knowledge:*` scopes
