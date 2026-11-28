# Sharing skills and personas (`.mcext`)

Move a skill or a persona from one MC installation to another. Backend and
API only. The screen (Settings -> Capabilities) comes in a later slice.

Code: `backend/app/services/extension_bundle.py`, `backend/app/routers/extensions.py`,
the `bundle:` branch in `backend/app/services/install_executor.py`.
Tests: `backend/tests/test_extension_bundle.py`.

## What can be shared

| Kind | Comes from | Lands as |
|---|---|---|
| `skill` | a folder in the skills library (`~/.mc/skills/<name>`) | the same folder, byte for byte, after approval |
| `persona` | an agent template (`agent_templates`) | a new template plus its skills, after approval |

Only **text** skills travel: `SKILL.md` plus `.md` / `.markdown` / `.txt`
files that are UTF-8, carry no `#!` line and no executable bit. Scripts,
hooks, binaries and unknown file types are refused with reason
`contains_code`. Plugins and MCP servers are code too. A persona export
leaves them out and only counts them (`not_included`). A persona import that
lists them is refused.

## File format

A `.mcext` file is a zip:

```
manifest.json
skills/<name>/<path>        one folder per embedded skill
```

```json
{
  "format": "mcext", "version": 1, "kind": "skill",
  "name": "note-taker", "description": "...", "created_with": "<MC version>",
  "items": [{"type": "skill", "name": "note-taker", "sha256": "<tree hash>",
             "files": [{"path": "SKILL.md", "sha256": "...", "size": 123}]}],
  "persona": {"name": "...", "description": "...", "emoji": "...",
              "soul_md": "...", "cli_skills": ["note-taker"],
              "model_hint": "...", "not_included": {"plugins": 0, "mcp_servers": 0}}
}
```

`persona` appears only in a persona bundle. The tree hash is the sha256 over
the sorted lines `path NUL sha256(file)` of the skill's files.

## API (admin only)

```sh
# export
curl -H "Authorization: Bearer $TOKEN" -OJ \
  "http://localhost/api/v1/extensions/export?skill=note-taker"
curl -H "Authorization: Bearer $TOKEN" -OJ \
  "http://localhost/api/v1/extensions/export?template=<template id>"

# import: a skill needs the agent that gets it, a persona does not
curl -H "Authorization: Bearer $TOKEN" \
  -F file=@note-taker.mcext -F target_agent_id=<agent id> \
  http://localhost/api/v1/extensions/import
```

The import answers `201` with the pending approval(s). It answers `200` with
`"existing": true` when the same bundle is already waiting. A refusal is
`{"detail": {"reason": "<code>", "message": "..."}}`.

| reason | HTTP | meaning |
|---|---|---|
| `invalid_bundle` | 400 | not a zip, unreadable entry |
| `bad_manifest` | 400 | missing or wrong manifest, unlisted file |
| `too_large` / `too_many_files` | 400 | over 5 MB (upload or unpacked) / over 200 files |
| `unsafe_path` | 400 | `..`, absolute path, backslash, symlink |
| `bad_name` | 400 | name not `[a-z0-9][a-z0-9_-]{0,99}` |
| `sha_mismatch` | 400 | a file or skill does not match its sha256 |
| `contains_code` | 400 / 422 on export | script, hook, binary, plugin or MCP server |
| `contains_secret` / `contains_path` | 422 (export) | token-shaped secret or home path in the content |
| `target_required` | 400 | skill import without `target_agent_id` |
| `exists` | 409 | the skill or template name is taken, never overwritten |
| `not_found` | 404 | unknown skill, template or agent |

## What happens on import

1. The whole file is checked in memory: size, paths, names, hashes, text rule.
2. Each skill is written to `~/.mc/extensions/staging/<tree sha256>/`.
   Nothing goes into the skills library yet.
3. Approvals are created, `pending`, 7 days:
   - **skill** -> `install_skill` with source `bundle:<tree sha256>`, the
     same payload shape as an agent's install request. On approval the
     install executor reads the staged files again, checks the sha256
     again, refuses an existing name and copies the folder. It then adds
     the skill to the target agent (an agent that loads all skills stays at
     all). The skill takes effect at the agent's next chat start.
   - **persona** -> one `import_persona` approval. On approval the template
     is created with its skills. An identical existing skill is reused, a
     different one with the same name stops the import. Name taken ->
     nothing is installed. The template gets no plugins and no MCP servers,
     and its scopes are the developer set without credential access
     (scopes never travel in a bundle).

What never leaves an installation in an export: agent or template ids,
scopes, env, plugin and MCP server lists, absolute paths. An export with a
token-shaped secret or a home path is refused rather than cleaned.
