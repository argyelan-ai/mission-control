# Security Audit — Mission Control

## Wichtiger Hinweis zur Abdeckung

Die Etappen A-D unten waren ein Sweep nach gefährlichen **Primitiven**
(`shell=True`, `eval`, `verify=False`, CORS-Wildcard, hardcodierte Secrets).
Der Sweep war sauber ausgeführt und die Ergebnisse halten — aber es wurde nie
die architektonische Frage gestellt: **"welche Endpunkte haben gar keine
Auth-Dependency, und ist das jeweils Absicht?"** Genau dort saß das HIGH-
Finding unten (`internal.py`). Kein Grep nach verbotenen Funktionsnamen
findet einen Endpunkt, dessen Problem das *Fehlen* von etwas ist. Die
"Keine Findings"/"sonst sauber"-Aussagen in Etappe A und C weiter unten
gelten nur für das, was tatsächlich geprüft wurde (siehe dort) — nicht als
umfassende Auth-Coverage-Aussage für das gesamte Repo. Dieser Abschnitt
wurde nach PR #404 Rex-Review (Stand `d95eaa08`) ergänzt, der genau diese
Lücke aufgedeckt hat.

| Schweregrad | Datei:Zeile | Problem | Fix |
|---|---|---|---|
| High | backend/app/routers/internal.py:173-235 | `GET /api/v1/internal/bootstrap` war unauthentifiziert und gab bei bekanntem Agent-Namen saemtliche Vault-Secrets im Klartext heraus (MC_AGENT_TOKEN, GH_TOKEN mit repo-Scope, Provider-Keys, ELEVENLABS/HIGGSFIELD/X-Token). Der Docstring behauptete eine Caddy-Netzwerkrestriktion, die es nicht gab. | Bearer-Header-Check (`INTERNAL_BOOTSTRAP_SECRET`, `secrets.compare_digest`) + Caddy `respond 403` auf `/api/v1/internal/*` VOR dem generischen `/api/*`-Handler. Produktion faellt beim Boot hart aus ohne konfiguriertes Secret (`validate_boot_secrets`). |
| Medium | backend/app/main.py:847-854 | `slowapi`-`Limiter(default_limits=["120/minute"])` war gebaut, aber ohne `SlowAPIMiddleware` oder `@limiter.limit`-Dekoratoren wirkungslos — de facto **kein** globales Rate-Limiting, obwohl der Code danach aussah. | `app.add_middleware(PathExemptSlowAPIMiddleware)` (slowapi plus Pfad-Ausnahmen, `app/rate_limit.py`); Budget auf `600/minute` angehoben, weil Marks Browser EINE IP fuer die ganze UI ist. Nicht mitgezaehlt: `/api/v1/agent/*`, `/api/v1/internal/*`, `/health` und SSE-Streams — die Flotte pollt im Sekundentakt und haette sonst Mark aus seiner eigenen UI aussperren koennen. In Tests deaktiviert (gemeinsame Fake-Client-IP haette sonst Spurious-429 quer durch die Suite ausgeloest). Tests assertieren echtes HTTP 429 und die Ausnahmen. |
| Medium/Low | backend/app/routers/auth.py:176 | Login-Rate-Limiter schluesselte auf `request.client.host`, das hinter Caddy fuer jeden Request die Caddy-Container-IP war (Uvicorn lief ohne `--proxy-headers`). Folge: alle Nutzer teilten einen Bucket — 5 falsche Passwoerter von irgendwem sperrten den Login fuer alle 15 Minuten; per-Angreifer limitierte es gar nicht. | Uvicorn mit `--proxy-headers` gestartet. Die Vertrauensliste kommt aus `backend/docker-entrypoint.sh` (`FORWARDED_ALLOW_IPS`): die eigenen Container-Subnetze OHNE die Bridge-Gateways, zur Startzeit aus `/proc/net/route` berechnet (`backend/app/proxy_trust.py`). Caddy wird geglaubt, ein Host-Prozess ueber den publizierten Port `127.0.0.1:8000` nicht. `request.client.host` liefert danach die echte Client-IP, kein Code-Fix an der Limiter-Schluesselung selbst noetig. **Korrektur zur ersten Fassung dieses PRs:** dort stand `--forwarded-allow-ips=*` mit der Begruendung, `backend` publiziere keinen Host-Port — das war falsch (`docker-compose.yml` publiziert `127.0.0.1:8000:8000`, die Host-Agenten brauchen ihn), womit jeder lokale Prozess `X-Forwarded-For` haette faelschen koennen. |
| Low | backend/app/auth.py:127 | `require_user` akzeptiert Session-Token per `?token=`-Query-Parameter — landet in Proxy-Access-Logs, Browser-History, `Referer`-Headern. | Bewusst akzeptiertes Risiko, nicht entfernt (fuer SSE-Verbindungen ohne Custom-Header nachvollziehbar). Hier dokumentiert statt stillschweigend weggelassen. |
| Low | backend/scripts/setup_discord_channels.py:118 | Erste 20 Zeichen des Discord-Bot-Tokens wurden auf stdout geloggt (Terminal-Scrollback/CI-Logs) | Nur die letzten 4 Zeichen maskiert ausgeben |

## Etappe A — Secrets & Config

Geprüft: hardcodierte Tokens/Keys/Passwörter (Regex-Heuristik über backend/frontend/docker), `.env`-Dateien im Git (nur `.env.j2`-Template getrackt, korrekt), CORS `allow_origins` in `backend/app/main.py:823` (explizite Origin-Liste inkl. konfigurierbarem `PUBLIC_HOST`/`EXTRA_CORS_ORIGINS`, kein Wildcard `*`), `verify=False`/unverifizierte SSL-Kontexte (keine Treffer), Secrets im Logging (siehe Fund unten), `debug=True`/Docker-Compose mit hardcodierten Secrets (keine Treffer). Für die geprüften Primitive keine weiteren Findings — aber siehe den Hinweis oben: ob ein Endpunkt überhaupt eine Auth-Dependency hat, war nicht Teil dieses Greps. Das HIGH-Finding (`internal.py`) lag genau in dieser Lücke.

## Etappe B — Command-Execution

Geprüft: `shell=True`-Aufrufe (tools/generate-agent-map.py, scripts/cli-bridge.py) — Bridge-Endpunkte quoten User-Input konsequent per `shlex.quote()`, das Map-Generator-Tool ist ein lokales Maintainer-Skript ohne externe Eingabe. `cli_terminal.py` und `docker_agent_sync.py` nutzen ausschließlich `create_subprocess_exec`/`subprocess.run` mit Argv-Listen (kein Shell-String), Container-Namen sind aus DB-Slugs abgeleitet und per `assert` auf das `mc-agent-*`-Präfix beschränkt. `os.system`/`eval`/`exec` — keine Treffer im Backend. Keine Findings, nichts gefixt.

## Etappe C — Auth & Input

Geprüft: f-string-SQL (`agents.py:1180`, `vault_index.py:160/179`, `mc_henry_sunset.py:133`) — Spalten-/Tabellennamen kommen entweder aus Schema-Introspektion mit `_SQL_NAME_RE`-Whitelist oder sind statisch, Werte sind konsequent parametergebunden (`:dashed`, `:hex`). Path-Traversal in `vault.py` (`_safe_path`, `_safe_trash_filename`), `skills.py` (`get_skill_content`/`update_skill_content`), `clawhub.py` (Zip-Slip-Schutz bei `install_skill`) — jeweils explizite `..`/absolute-Pfad-Ablehnung plus `resolve()` + `relative_to()`-Doppelcheck. Auth (`backend/app/auth.py`): bcrypt für Passwörter, PBKDF2-HMAC + `hmac.compare_digest` für Agent-Tokens, `secrets.compare_digest` für den lokalen Auth-Token, JWT mit Expiry (8h) und `token_version`-Invalidierung. `config.py:validate_boot_secrets` verweigert den Produktionsstart bei Placeholder-JWT-Secret/leerem Encryption-Key. Credential-Vault (`encryption.py`) nutzt Fernet, verweigert Start ohne Key. Für SQL-Injection, Path-Traversal und die geprüften Auth-Mechanismen keine Findings — aber auch hier gilt der Hinweis oben: geprüft wurde, ob vorhandene Auth-Checks korrekt implementiert sind, nicht, ob jeder Endpunkt überhaupt einen hat. Zusätzlich in einer späteren Review nachgetragen (PR #404): Rate-Limiting war für Login und global de facto wirkungslos (siehe Findings-Tabelle oben), und Session-Token per Query-Parameter ist ein akzeptiertes Low-Risk (siehe unten).

## Etappe D — Docker & Frontend

Geprüft: `docker-compose.yml` — alle Service-Ports binden an `127.0.0.1` außer Caddy (`${MC_BIND_ADDRESS:-127.0.0.1}`), kein `privileged: true` in irgendeiner Compose-Datei, `docker.sock` wird nur read-only in `docker-socket-proxy` gemountet (kein Direktzugriff des Backends), Proxy whitelistet explizit nur die benötigten API-Pfade (BUILD/SWARM/SYSTEM=0). Frontend: kein `dangerouslySetInnerHTML`, kein `localStorage`/`sessionStorage` (Auth läuft komplett über httpOnly-Cookie + Bearer-Header, kein Client-seitiges Token-Storage), keine Secrets in `NEXT_PUBLIC_*`-Vars.

Ein Finding ist noch offen und liegt im Follow-up-PR #416 (SSE-Cookie Secure-Flag): `mc_sse_token`-Cookie in `backend/app/routers/auth.py` setzt kein `Secure`-Flag. Details unten unter "Offen im Follow-up-PR #416".

## Etappe E — Auth-Coverage (nachträglich, PR #404 Rex-Review)

Die drei in der Findings-Tabelle oben aufgeführten Vektoren wurden gezielt nachgeprüft, nachdem Etappen A-D sie nicht gefunden hatten (siehe "Wichtiger Hinweis" oben). Zusätzlich wurden folgende, konkret genannte Vektoren verifiziert und bestätigt sauber:

| Vektor | Befund |
|---|---|
| Deserialisierung | Sauber — kein `pickle`/`marshal`/`yaml.load` im Backend, auch kein `yaml.safe_load`. |
| JWT & Algorithmen | Sauber — `algorithms=[HS256]` an allen 12 Decode-Stellen gepinnt, kein `alg=none`-Fenster. Scope-Confusion zwischen Session- und Bench-View-Token wird in `auth.py:147-152` explizit abgefangen. |
| SSRF | Sauber — kein user-kontrollierter Outbound-URL in `routers/`/`services/`; httpx-Ziele sind Konstanten oder Config-Werte. |
| IDOR / Ownership | Sauber, wo stichprobenartig geprüft — `agent_scoped` hängt durchgängig an `require_scope(...)`, Task-Zugriff prüft `assigned_agent_id`/`owner_agent_id`, Deliverable-Pfade mit `..`-Reject plus `realpath()` + Prefix-Check. |
| Secrets in Migrations/Seeds | Sauber — 193 Alembic-Revisionen, keine hartkodierten Credentials. |

## Offen im Follow-up-PR #416 (SSE-Cookie Secure-Flag)

**Low — `mc_sse_token`-Cookie ohne `Secure`-Flag** (`backend/app/routers/auth.py`) — **OFFEN, liegt in PR #416** (`security/sse-cookie-secure-flag`, noch nicht gemergt)

> Dieser Abschnitt stand vorher auf "GEFIXT" und nannte einen Test als Beleg, den es in
> `main` nicht gibt. Das Repo ist oeffentlich — eine offene Session-Cookie-Schwaeche als
> geschlossen zu beschreiben ist die schlechteste Kombination. Bis #416 gemergt ist gilt:
> `secure=` wird **nicht** gesetzt. Der Rest des Abschnitts beschreibt den geplanten Fix
> aus #416, nicht den Ist-Zustand.

Das Cookie ist `httponly=True` und `samesite="lax"`, trug aber kein `secure` — es würde auch über Klartext-HTTP übertragen. Wichtig für die Risikoeinschätzung: `mc_sse_token` ist **kein reines SSE-Cookie** — `require_user` fällt für **jede** Route auf dieses Cookie zurück, wenn kein Bearer-Token vorliegt. Es ist ein vollwertiges Session-Credential.

**Gewähltes Kriterium:** `secure=request.url.scheme == "https"` (ASGI `scope["scheme"]`). Hinter Caddy läuft Uvicorn mit `--proxy-headers` und einer Vertrauensliste, die Caddy enthält (backend/docker-entrypoint.sh, PR #404) — `ProxyHeadersMiddleware` leitet das Scheme aus `X-Forwarded-Proto` des vertrauenswürdigen Proxies ab, das Flag greift also korrekt hinter dem Reverse Proxy. Bei reinem HTTP (localhost, LAN per `MC_BIND_ADDRESS=0.0.0.0`, Tailscale ohne TLS) wird `Secure` bewusst NICHT gesetzt — ein pauschales `secure=True` hätte der Browser beim Klartext-Login verworfen und den Nutzer aus der eigenen UI ausgesperrt.

Beide Richtungen sind in PR #416 per Test belegt (`backend/tests/test_sse_cookie_secure_flag.py` — die Datei existiert erst mit diesem PR): HTTPS-Login setzt `Secure`, Klartext-HTTP-Login nicht — und die Session funktioniert danach über den Cookie auf einer `require_user`-Route (`/api/v1/auth/me` → 200).

**Low — Session-Token per Query-Parameter** (`backend/app/auth.py:127`)

`require_user` akzeptiert `?token=` als Fallback. Query-Strings landen in Proxy-Access-Logs, Browser-History und `Referer`-Headern. Für SSE-Verbindungen ohne Custom-Header-Support nachvollziehbar, deshalb nicht entfernt — aber bewusst als akzeptiertes Risiko hier dokumentiert statt implizit unerwähnt zu bleiben.

