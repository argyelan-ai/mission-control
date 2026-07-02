# ADR-043 — Open-Source-Release: Portabilitäts- & Identitäts-Vertrag

**Status:** Accepted
**Datum:** 2026-07-02
**Scope:** Infra/Deploy | Backend/Config | Release-Prozess

## Kontext

MC soll open-sourced werden. Das Repo enthielt (a) ein echtes Secret im Code
(`DB_PASSWORD` in `tools/generate-agent-map.py`, seit April in der History),
(b) persönliche Daten in ~480 Dateien (Operator-Name, E-Mail, Arbeitgeber,
Tailscale-/LAN-Topologie, `/Users/…`-Pfade, Telegram-Chat-ID) und (c) vier
harte Deploy-Blocker für Fremde (BSD-only `sed` in setup.sh, Caddy-TLS-Block
mit gitignorten Certs, maschinenspezifische Bind-Mounts, stale README/env).
Die Git-History (2400+ Commits) ist damit nicht publizierbar.

## Entscheidung

1. **Fresh-History-Release:** Veröffentlicht wird ein sanitisierter
   Single-Commit-Export via `scripts/release-public.sh` (strippt interne
   Dateien, Zero-Grep-Gate über persönliche Identifier, Gitleaks-Gate mit
   `.gitleaks.toml`-Allowlist für verifizierte False Positives). Das
   private Repo bleibt Dev-Heimat und wird NIE public geschaltet.
2. **Identitäts-Vertrag über Env:** Alles Operator-Spezifische kommt aus
   `.env`/Settings statt aus Code: `OPERATOR_NAME` (Agent-Templates rendern
   `{{ operator_name }}`), `GITHUB_OWNER`, `TELEGRAM_CHAT_ID`,
   `NEWSLETTER_BRAND`, `NEWS_REPO_PATH`, `HOST_SSH_USER`,
   `MC_OWNED_REPO_PREFIXES`, `DGX_SSH_HOST/USER`, `LIVEKIT_NODE_IP`,
   `PUBLIC_HOST`, `MC_REPO_PATH`, `HOST_UID`.
3. **Pfad-Vertrag:** Host-Pfade leiten sich aus `HOME_HOST`
   (`settings.home_host`) ab; Repo-Pfad aus `MC_REPO_PATH` (setup.sh
   schreibt `$(pwd)`). Maschinenspezifische Mounts (SSH-Keys, free-code)
   leben in `docker-compose.override.yml` (Beispiel:
   `docker-compose.override.example.yml`).
4. **TLS-Modell:** Die shipped `Caddyfile` bedient nur `:80`. TLS ist
   Opt-in via `caddy/Caddyfile.tls.example` → `caddy/Caddyfile.local`
   (gitignored) + Override-Mount.
5. **DB-Auth:** `pg_hba.conf` verlangt `scram-sha-256` für
   Netzwerk-Verbindungen (vorher `trust`).

## Alternativen

- **History-Rewrite (filter-repo):** Verworfen — bei ~480 betroffenen
  Dateien über 2400 Commits fehleranfällig; Commit-Messages/Diffs bleiben
  Rekonstruktionsrisiko.
- **Zweites, handgepflegtes Public-Repo:** Verworfen — Drift-Garantie.
  Der Release-Script-Export ist reproduzierbar und gated.
- **Operator-Identität hardcoded lassen:** Verworfen — Public-Repo mit
  Personenbezug; ausserdem bricht es Fremd-Deployments.

## Konsequenzen

### Positiv
- Frischer Clone bootet mit `./setup.sh && docker compose up --build -d`
  auf macOS und Linux (sed-Portabilität containergetestet).
- Personenbezug zentral konfigurierbar; Agents adressieren den Operator
  weiter mit Namen (`OPERATOR_NAME`).
- Release ist ein deterministischer, doppelt gegateter Build-Schritt.

### Negativ / Upgrade-Hinweise (bestehende Installationen)
- **pg_hba scram:** Wenn `.env`-`DB_PASSWORD` je von der echten Postgres-
  Rolle abgewichen ist (unter `trust` unsichtbar), scheitert das Upgrade →
  vorher `ALTER USER mc PASSWORD` auf den `.env`-Wert setzen.
- **TLS/Voice:** Ohne `Caddyfile.local`-Override verschwindet der frühere
  HTTPS-Endpoint; ohne `LIVEKIT_NODE_IP` advertised LiveKit nur noch
  `127.0.0.1` (Voice remote unerreichbar). Beides bewusst Opt-in.
- **Leere Defaults:** `GITHUB_OWNER`/`DGX_SSH_*` sind leer bis gesetzt —
  Features, die sie brauchen, sind bis dahin aus.
- Der `USE_SUBAGENT_DISPATCH`-Kill-Switch bleibt verdrahtet (Review-Fund:
  das Flag ist entgegen der CLAUDE.md-Notiz „obsolet" in 7 Call-Sites
  aktiv). Ob es wirklich pensioniert wird, ist eine eigene Aufräum-PR.

## Follow-ups
- HOME_HOST-Resolution auf einen Accessor konsolidieren (`settings.home_host`
  statt ~15 ad-hoc `os.environ.get`-Stellen) — Teil der Modularisierung.
- Release-Excludes/Identifier-Patterns aus dem Script in Manifest-Dateien
  ziehen (`release/`-Config), CI-Gate.
- News/Shorts-Vertical als Modul strippen (ADR folgt mit der
  Modularisierung).
