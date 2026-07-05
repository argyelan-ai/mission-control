# ADR-055 — GitHub-Verbindung als konfigurierbarer First-Class-Anschluss (Vault > Env, live)

**Status:** Accepted
**Datum:** 2026-07-05
**Scope:** Backend/Services | Backend/API | Frontend/Settings | Install

## Kontext

Der komplette Agent-Git-Workflow (Repo pro Projekt, Branch pro Task, PRs,
Repos-Registry ADR-050/052) hing an zwei unsichtbaren `.env`-Variablen:

- `GITHUB_OWNER` wurde in `git_service.py` **beim Modul-Import eingefroren**
  — Änderungen griffen erst nach Backend-Neustart.
- `GH_TOKEN` wurde von `_ensure_git_auth()` genau **einmal** in
  `~/.git-credentials` geschrieben — Token-Rotation erforderte Neustart.
- Kein Setup-Schritt fragte die Werte ab (install.sh, Web-Wizard), keine
  Settings-Seite zeigte den Verbindungszustand, kein Endpoint konnte ihn
  prüfen. Fehlerbild ohne Config: generische `RuntimeError`s → HTTP 502.
- Widerspruch zu ADR-033, das System-Token (explizit inkl. GitHub) dem
  Secrets-Vault zuordnet: der Startup-Seed (`github_token` → Vault) und die
  Agent-Bootstrap-Auslieferung existierten schon, aber `git_service` selbst
  las weiter nur `os.environ`.

Für MC-User des Public-Repos war die Git-Integration damit faktisch
unauffindbar (Marks Punkt 5(e)).

## Entscheidung

GitHub-Owner + -Token werden über **einen zentralen Resolver**
(`services/github_config.py`) aufgelöst — **Vault (`github_owner`,
`github_token`) vor Env (`GITHUB_OWNER`, `GH_TOKEN`)** — mit kurzem
TTL-Cache (30s) und expliziter Invalidierung bei Vault-Schreibzugriffen.
Alle Konsumenten (git_service, repos/boards-Router, task_context_builder,
visibility_monitor, template_renderer) gehen durch den Resolver; die
Modul-Konstante `GITHUB_OWNER` und das Einmal-Auth-Flag entfallen.
`_ensure_git_auth()` schreibt die Credentials bei Token-Wechsel neu und
injiziert den aufgelösten Token als `GH_TOKEN`/`GITHUB_TOKEN` in jede
`gh`/`git`-Subprozess-Env (Vault-Wert schlägt Prozess-Env).

Sichtbar wird der Anschluss über:

- `GET /api/v1/repos/github-status[?probe=true]` — Config-Sicht (Quellen,
  configured) + optionaler Live-Check (`gh api user` / `users/{owner}` /
  `rate_limit`, 15s-Timeouts).
- `PUT /api/v1/repos/github-config` (admin) — Owner/Token in den Vault
  (Upsert; `""` löscht den Vault-Wert → Env-Fallback), gilt sofort.
- Settings → **GitHub**-Sektion (Statuskarte + Test connection), optionaler
  **Connect-GitHub-Step** im Setup-Wizard, Onboarding-Banner auf `/repos`,
  interaktive Owner/Token-Abfrage in `install.sh` (Token silent, no-echo).
- Startup-Seed erweitert: auch `GITHUB_OWNER` wandert idempotent in den
  Vault; danach wird der Resolver-Cache geprimt (sync-Renderkontexte).

## Alternativen

- **Nur `.env` + Doku:** kein Neustart-freies Setup, Settings könnte nichts
  schreiben (Backend kann die Host-`.env` nicht editieren) → verworfen.
- **Eigene `github_config`-Tabelle (Single-Row wie `discord_config`):**
  sauber, aber neue Migration + dritter Geheimnis-Store; der Vault erfüllt
  ADR-033 bereits und verschlüsselt den Token → verworfen.
- **Env-Priorität vor Vault:** Vault-Edits aus der UI blieben wirkungslos,
  solange `.env` gesetzt ist — genau das unsichtbare Verhalten, das weg
  soll. Env bleibt bewusst der Fallback für CLI-first-Setups → verworfen.
- **Owner in per-User-Settings (`UserSettings`):** Owner ist systemweit,
  nicht per-User → verworfen.

## Konsequenzen

### Positiv
- Onboarding geschlossen: install.sh → Wizard → Settings → /repos zeigen
  denselben Zustand; „Test connection" beweist die Verbindung live.
- Token-Rotation + Owner-Wechsel ohne Backend-Neustart; der
  Visibility-Monitor wacht auf, sobald ein Owner konfiguriert wird
  (loop statt Startup-Abbruch).
- ADR-033 endlich konsistent: ein Store (Vault), Env nur noch Fallback.

### Negativ
- Zwei Quellen ⇒ Diagnose braucht die Quellen-Anzeige (`owner_source`/
  `token_source` sind deshalb Teil der Status-API und der UI).
- 30s-TTL-Cache: direkte DB-Manipulation am Vault (ohne API) greift erst
  nach Ablauf; API-Pfade invalidieren explizit.
- Tests dürfen `git_service.GITHUB_OWNER` nicht mehr patchen — Seam ist
  jetzt Env-Patch + `invalidate_github_config_cache()` (autouse-Fixture in
  conftest verhindert Cache-Leaks zwischen Tests).

## Referenzen

- Betroffene Dateien: `backend/app/services/github_config.py` (neu),
  `backend/app/services/git_service.py`, `backend/app/routers/repos.py`,
  `backend/app/routers/secrets.py`, `backend/app/main.py`,
  `backend/app/services/{task_context_builder,template_renderer,github_visibility_monitor}.py`,
  `install.sh`, `setup.sh`, `.env.example`,
  `frontend-v2/src/app/{settings,setup,repos}/`, `docs/setup/github.md`
- Verwandte ADRs: ADR-033 (Secrets vs Credentials), ADR-050 (Repos
  Registry), ADR-052 (Task-Repo-Auswahl)
