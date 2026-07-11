# ADR-069 — Vault-Token-Keys auf stabiles Slug-Schema (`mc_token_{slug}`)

**Status:** Accepted
**Datum:** 2026-07-11
**Scope:** Backend/DB · Backend/Auth · Infra/Provisioning

## Kontext

Der MC_AGENT_TOKEN eines Agenten liegt im `secrets`-Vault unter einem Etikett,
das bisher aus dem **Namen** abgeleitet wurde: `mc_token_{agent.name.lower()}`
(Leerzeichen bleiben erhalten). Das ist fragil:

- **Rename verwaist den Key.** Ein einfacher PATCH-Rename rotiert den Token
  nicht und rendert Compose nicht neu — das Etikett zeigt weiter auf den alten
  Namen. `delete_agent` musste in PR #99 deshalb um zwei Key-Varianten
  (Bindestrich + Leerzeichen) herumbauen, um Orphans zu treffen.
- **Leerzeichen brechen Parsing.** Ein mehrwortiger Agent („Host Testpilot")
  erzeugte `mc_token_host testpilot`. `start-all.sh` generierte daraus eine
  Env-Zeile `MC_TOKEN_HOST TESTPILOT=…`, die `docker/.env.agents` zerbrach
  (Vorfall 2026-07-11).
- **Vier Stellen** leiteten das Etikett je eigen ab (Writer, Reader, Cleanup,
  Consumer) — Drift-Gefahr.

`Agent.slug` (gesetzt beim Insert via `_agent_fill_slug`, **nie** bei Rename,
nur `[a-z0-9-]`, Leerzeichen→Bindestrich) ist bereits die stabile Identität für
Workspace-/Deliverable-Pfade und den Compose-Envkey. Der Token-Vault war das
letzte namensbasierte Stück.

## Entscheidung

Der Token-Vault-Key wird aus dem **stabilen Slug** abgeleitet:
`mc_token_{agent.slug}`. Writer und Reader gehen beide über den kanonischen
Resolver `fs_service.agent_slug(agent)` (persistierter Slug + Fallback
`name.lower().replace(" ", "-")` für Alt-Rows). Bestehende namensbasierte Keys
in der DB werden per Alembic-Migration **0152** im Gleichschritt umbenannt.

Single-Word-Agents sind unter beiden Schemata byte-identisch
(`"rex".lower()` == Slug `"rex"`) — nur mehrwortige Agents ändern sich.

## Alternativen

- **Status quo lassen + Delete-Dual-Key behalten:** → Verworfen. Behandelt nur
  das Symptom bei Delete, nicht bei Rename/Bootstrap/Consumer; die
  Leerzeichen-Parsing-Falle bliebe.
- **Auf UUID (`mc_token_{agent.id}`) keyen:** → Verworfen. Maximal stabil, aber
  Keys werden für Menschen/Debug unlesbar, und der Compose-Envkey +
  `start-all.sh`-Konsument müssten ebenfalls auf UUID umziehen (grössere
  Blast-Radius). Slug ist stabil genug (ändert sich nie) und bleibt lesbar.
- **Nur Code ändern, keine Migration:** → Verworfen. Live-Agents hätten weiter
  Namens-Etiketten; der Reader suchte `mc_token_{slug}`, fände das echte Secret
  `mc_token_{name}` nicht → Token weg → `poll.sh` Crash-Loop.

## Konsequenzen

### Positiv
- Ein Etikett-Schema, überall gleich (Writer/Reader/Cleanup/Consumer/Compose).
- Rename-sicher: Slug ändert sich nie, Writer und Delete stimmen immer überein.
- Keine Leerzeichen mehr in Keys → `.env.agents`-Parsing kann nicht mehr brechen.
- `delete_agent_token_secret` von Dual-Key auf Single-Key vereinfacht.
- `upsert_agent_token_secret`-Signatur nimmt jetzt das `agent`-Objekt statt des
  losen Namens → Aufrufer können den Namen nicht mehr falsch übergeben.

### Negativ
- **Migration fasst Live-Auth an.** Deploy nur mit Backup (`./backup.sh`) und
  `in_progress == 0` (Hard-Rule: kein Fleet-Eingriff während Agenten arbeiten).
- **Downgrade ist verlustbehaftet:** eine auf Upgrade zusammengeführte Kollision
  lässt sich nicht wieder auftrennen; Orphan-Keys bleiben unangetastet.
- Kollisions-Tiebreak (beide Key-Formen vorhanden aus Rename+Reset-Historie)
  wählt das neuere `updated_at` — im theoretischen Gleichstand gewinnt die
  kanonische Slug-Form. Bewusst simpel gehalten.

## Referenzen

- Betroffene Dateien:
  - `backend/app/services/vault_key_migration.py` — Pure Planner + Connection-Executor
  - `backend/alembic/versions/0152_vault_token_keys_to_slug.py` — Migration
  - `backend/app/services/secrets_helper.py:113` (Writer), `:146` (Delete)
  - `backend/app/routers/internal.py:160` (Reader/Bootstrap)
  - 10 Writer-Aufrufer (agents.py, agent_scoped.py, approvals.py,
    agent_templates.py, cli_terminal.py, agent_bootstrap.py)
  - `scripts/start-all.sh` — Consumer-Kommentar aktualisiert (funktional identisch)
- Tests: `backend/tests/test_vault_key_migration.py` (Planner + Integration),
  `backend/tests/test_agent_token_vault.py` (Writer/Reader/Delete-Slug)
- Verwandte ADRs: ADR-063 (Onboarding-Wizard/Host-Provisioning), ADR-033
  (Secrets vs Credentials), PR #99 (Delete-Kaskade, baute um genau dieses Problem)
