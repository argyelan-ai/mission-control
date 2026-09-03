# Security Audit — Mission Control

| Schweregrad | Datei:Zeile | Problem | Fix |
|---|---|---|---|
| Low | backend/scripts/setup_discord_channels.py:118 | Erste 20 Zeichen des Discord-Bot-Tokens wurden auf stdout geloggt (Terminal-Scrollback/CI-Logs) | Nur die letzten 4 Zeichen maskiert ausgeben |

## Etappe A — Secrets & Config

Geprüft: hardcodierte Tokens/Keys/Passwörter (Regex-Heuristik über backend/frontend/docker), `.env`-Dateien im Git (nur `.env.j2`-Template getrackt, korrekt), CORS `allow_origins` in `backend/app/main.py:823` (explizite Origin-Liste inkl. konfigurierbarem `PUBLIC_HOST`/`EXTRA_CORS_ORIGINS`, kein Wildcard `*`), `verify=False`/unverifizierte SSL-Kontexte (keine Treffer), Secrets im Logging (siehe Fund oben), `debug=True`/Docker-Compose mit hardcodierten Secrets (keine Treffer). Sonst sauber.

## Etappe B — Command-Execution

Geprüft: `shell=True`-Aufrufe (tools/generate-agent-map.py, scripts/cli-bridge.py) — Bridge-Endpunkte quoten User-Input konsequent per `shlex.quote()`, das Map-Generator-Tool ist ein lokales Maintainer-Skript ohne externe Eingabe. `cli_terminal.py` und `docker_agent_sync.py` nutzen ausschließlich `create_subprocess_exec`/`subprocess.run` mit Argv-Listen (kein Shell-String), Container-Namen sind aus DB-Slugs abgeleitet und per `assert` auf das `mc-agent-*`-Präfix beschränkt. `os.system`/`eval`/`exec` — keine Treffer im Backend. Keine Findings, nichts gefixt.

## Nicht gefixt (bewusst)

