# Security Audit — Mission Control

| Schweregrad | Datei:Zeile | Problem | Fix |
|---|---|---|---|
| Low | backend/scripts/setup_discord_channels.py:118 | Erste 20 Zeichen des Discord-Bot-Tokens wurden auf stdout geloggt (Terminal-Scrollback/CI-Logs) | Nur die letzten 4 Zeichen maskiert ausgeben |

## Etappe A — Secrets & Config

Geprüft: hardcodierte Tokens/Keys/Passwörter (Regex-Heuristik über backend/frontend/docker), `.env`-Dateien im Git (nur `.env.j2`-Template getrackt, korrekt), CORS `allow_origins` in `backend/app/main.py:823` (explizite Origin-Liste inkl. konfigurierbarem `PUBLIC_HOST`/`EXTRA_CORS_ORIGINS`, kein Wildcard `*`), `verify=False`/unverifizierte SSL-Kontexte (keine Treffer), Secrets im Logging (siehe Fund oben), `debug=True`/Docker-Compose mit hardcodierten Secrets (keine Treffer). Sonst sauber.

## Etappe B — Command-Execution

Geprüft: `shell=True`-Aufrufe (tools/generate-agent-map.py, scripts/cli-bridge.py) — Bridge-Endpunkte quoten User-Input konsequent per `shlex.quote()`, das Map-Generator-Tool ist ein lokales Maintainer-Skript ohne externe Eingabe. `cli_terminal.py` und `docker_agent_sync.py` nutzen ausschließlich `create_subprocess_exec`/`subprocess.run` mit Argv-Listen (kein Shell-String), Container-Namen sind aus DB-Slugs abgeleitet und per `assert` auf das `mc-agent-*`-Präfix beschränkt. `os.system`/`eval`/`exec` — keine Treffer im Backend. Keine Findings, nichts gefixt.

## Etappe C — Auth & Input

Geprüft: f-string-SQL (`agents.py:1180`, `vault_index.py:160/179`, `mc_henry_sunset.py:133`) — Spalten-/Tabellennamen kommen entweder aus Schema-Introspektion mit `_SQL_NAME_RE`-Whitelist oder sind statisch, Werte sind konsequent parametergebunden (`:dashed`, `:hex`). Path-Traversal in `vault.py` (`_safe_path`, `_safe_trash_filename`), `skills.py` (`get_skill_content`/`update_skill_content`), `clawhub.py` (Zip-Slip-Schutz bei `install_skill`) — jeweils explizite `..`/absolute-Pfad-Ablehnung plus `resolve()` + `relative_to()`-Doppelcheck. Auth (`backend/app/auth.py`): bcrypt für Passwörter, PBKDF2-HMAC + `hmac.compare_digest` für Agent-Tokens, `secrets.compare_digest` für den lokalen Auth-Token, JWT mit Expiry (8h) und `token_version`-Invalidierung. `config.py:validate_boot_secrets` verweigert den Produktionsstart bei Placeholder-JWT-Secret/leerem Encryption-Key. Credential-Vault (`encryption.py`) nutzt Fernet, verweigert Start ohne Key. Keine Findings, nichts gefixt.

## Nicht gefixt (bewusst)

