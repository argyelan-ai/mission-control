# ADR-050: Repos Registry — first-class Repo-Modell mit per-Repo-Arbeitsregeln

**Status:** Accepted (2026-07-04)

## Kontext

GitHub-Repos waren bisher kein eigenes Konzept in MC, sondern zwei String-Felder
auf `Project` (`github_repo_url`, `github_repo_name`, Migration 0019). Folgen:

- **Keine Verwaltungssicht.** Repos tauchten nur indirekt in der Task-Maske auf
  (via Projekt-Auswahl). Kein Ort zum Anschauen, Importieren, Archivieren.
- **Keine per-Repo-Arbeitsregeln.** Unterschiedliche Projekte/Codebases haben
  unterschiedliche Konventionen (Test-Kommandos, Branch-Policy, Stil) — es gab
  keinen Platz dafür; Regeln existierten nur pro Agent (`rules_md`) oder als
  Board-Bools.
- **Keine Wiederverwendung.** Zwei Projekte im selben Repo bedeuteten
  duplizierte Strings ohne Konsistenzgarantie.
- **Latenter Bug:** `init-repo` speicherte `github_repo_name = "mc-{slug}"`
  OHNE Owner-Präfix — alle `gh --repo {name}`- und
  `gh api repos/{name}/...`-Aufrufe (PR-Merge, Branch-Liste) brauchen aber
  `owner/name`.

## Entscheidung

Neues Modell **`Repo`** (`repos`-Tabelle, `models/repo.py`, Migration 0137):
`full_name` (unique, kanonisch `owner/name`), `url`, `default_branch`,
`description`, **`rules_md`**, `visibility`, `is_active`, `source`
(`mc`|`imported`), `last_synced_at`. `Project` erhält `repo_id` FK.

**Kompatibilitäts-Kontrakt:** Die Legacy-Felder `github_repo_url`/`_name`
bleiben der Read-Pfad aller Clone-/PR-/Merge-Flows und werden beim
(Ent-)Verknüpfen aus der Repo-Row gesynct
(`services/repo_registry.apply_repo_link`/`clear_repo_link`). Dadurch bleiben
`task_context_builder`, `cli_bridge_runner`, `agent_git`, `task_lifecycle`
unverändert.

**Regeln-Injektion:** `task_context_builder._load_dispatch_context` löst das
Repo des Projekts auf (`repo_id`, Fallback Legacy-`full_name`-Match) und
stasht `rules_md` auf dem `DispatchContext`; `dispatch_message_builder` hängt
sie als Abschnitt „Repository-Arbeitsregeln (owner/name) — BINDEND" an die
Git-Sektion der Worker-Directive. Best-effort — kein Regel-Lookup-Fehler
bricht je einen Dispatch.

**API** (`routers/repos.py`, `/api/v1/repos`, User-Auth): list/get/patch,
`POST /repos` (Import eines bestehenden GitHub-Repos via `gh repo view`),
`GET /repos/import-candidates` (`gh repo list` minus bereits registrierte,
minus archivierte), `POST /{id}/sync`, `POST /{id}/link-project` +
`DELETE /{id}/link-project/{pid}`, `DELETE /{id}` (409 solange Projekte
verknüpft; **löscht NIE auf GitHub**).

**Backfill (0137):** bestehende `projects.github_repo_name`-Werte werden
distinct zu Repo-Rows; Namen ohne `/` werden mit `GITHUB_OWNER` normalisiert.
`init-repo` legt seither Repo-Row + Link an und schreibt kanonisches
`owner/name` (Bugfix).

**Frontend:** neue Seite `/repos` (Liste, Detail mit Regeln-Editor,
Import-Dialog, Link/Unlink, Sync, Archivieren) + Sidebar-Eintrag.

## Alternativen

1. **`project_config`-JSON für Regeln** (kein neues Modell): minimal-invasiv,
   aber Regeln blieben pro Projekt dupliziert, keine Verwaltungssicht, keine
   Wiederverwendung — löst Marks Kernanliegen nicht. Verworfen.
2. **Repo-Regeln in `agent.rules_md`**: falsche Achse — Regeln gehören zur
   Codebase, nicht zum Agenten. Verworfen.
3. **Harte Migration auf `repo_id` (Legacy-Felder droppen):** sauberer
   Endzustand, aber grosser Blast-Radius (8+ Konsumenten) für null
   Nutzer-Mehrwert jetzt. Verschoben; Legacy-Felder können in einer späteren
   Phase fallen, wenn alle Read-Pfade auf `repo_id` umgestellt sind.

## Konsequenzen

- Repos sind verwalt- und teilbar; Regeln fliessen automatisch in jeden
  Dispatch im betreffenden Repo.
- Zwei Wahrheiten (Repo-Row + Legacy-Strings) — bewusst, mit Sync-Kontrakt an
  genau einer Stelle (`repo_registry`). Drift ist möglich, wenn jemand die
  Legacy-Felder direkt schreibt; neue Codepfade MÜSSEN über
  `apply_repo_link` gehen.
- `gh` CLI bleibt die einzige GitHub-Anbindung (kein neuer HTTP-Client).
