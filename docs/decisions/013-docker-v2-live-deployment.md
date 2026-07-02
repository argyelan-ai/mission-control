# ADR-013 — MC V2 Docker-Agents Live-Deployment + 8 Deployment-Lessons

**Status:** Accepted
**Datum:** 2026-04-08
**Scope:** Infra/Runtime

## Kontext

Das MC V2 Design (2026-04-07, siehe [Spec](../../MC-CLI-TMUX-PATCH/docs/superpowers/specs/2026-04-07-mc-v2-full-design.md)) war vollständig spezifiziert und implementiert: Docker-Agents mit HTTP-Poll und PTY-Browser-Terminal. Beim eigentlichen Live-Deployment am 2026-04-08 tauchten acht Deployment-Bugs auf, die der Plan nicht antizipiert hatte. Diese ADR dokumentiert die Lessons.

## Entscheidung

Alle 8 Bugs wurden gefixt, Docker V2 ist seit 2026-04-08 produktiv live. Dieser ADR dokumentiert die Ursachen und Fixes — **nicht um die Entscheidung zu begründen** (die war klar), sondern **damit die Lessons nicht verloren gehen** und bei zukünftigen Docker-Änderungen nicht wiederholt werden.

## Die 8 Deployment-Bugs

### Bug 1: Docker socket permission denied im Backend

**Symptom**: Backend konnte keine `docker exec` oder `docker restart` Befehle absetzen → 500-Errors beim Sessions-Page-Terminal.

**Root Cause**: Docker-Socket `/var/run/docker.sock` ist `srw-rw---- root:root`. Backend-Container läuft als non-root User.

**Fix**:
- `group_add: ["0"]` in `docker-compose.yml` backend service (fügt root-group hinzu für docker.sock-Zugriff)
- `docker` CLI im `backend/Dockerfile` installiert

### Bug 2: Network-Isolation (`mc-net` vs `mission-control_default`)

**Symptom**: Agent-Container konnten Backend nicht erreichen → `curl: (6) Could not resolve host: backend`

**Root Cause**: V2-Plan hatte `mc-net` als Agent-Netzwerk spezifiziert. Main-Stack hatte aber `mission-control_default`. Zwei isolierte Docker-Netzwerke → keine Kommunikation.

**Fix**:
- `docker/docker-compose.agents.yml`: Netzwerk von `mc-net` → `mission-control_default` umgestellt
- `MC_API_URL` default von `http://mc-backend:8000` → `http://backend:8000` (matching container name)
- Dokumentiert dass Main-Stack **zuerst** gestartet sein muss (erstellt das Netzwerk)

### Bug 3: tmux Zombie wegen `/sbin/nologin` Shell

**Symptom**: `docker exec mc-agent-boss tmux attach` → Zombie-Prozess, Session unbrauchbar.

**Root Cause**: `adduser -S agent` auf Alpine legt User mit `/sbin/nologin` als Login-Shell an. tmux nutzt die Login-Shell um Window-Commands auszuführen → `nologin` exits sofort → tmux-Window schliesst → Server verliert seine letzte Session → Zombies.

**Fix**: `adduser -S -s /bin/sh agent -G agent` im Dockerfile — explizit `/bin/sh` als Shell setzen.

**Lesson**: Alpine's default-shell für System-User ist nicht brauchbar. Immer `-s /bin/sh` bei `adduser -S` auf Alpine.

### Bug 4: `docker exec` fand falschen tmux-Socket

**Symptom**: `docker exec -it mc-agent-boss tmux attach-session -t boss` → "no server running on /tmp/tmux-0/default"

**Root Cause**: `docker exec` ohne `-u agent` läuft als `root` im Container. tmux speichert Sockets pro User-ID unter `/tmp/tmux-{uid}/`. Agent-tmux liegt unter `/tmp/tmux-100/`, root sucht aber `/tmp/tmux-0/`.

**Fix**: `docker exec -itu agent {container} tmux attach-session -dt {session}` — explizit `-u agent` + `-d` (detach andere Clients).

### Bug 5: PID 1 CPU-Spike auf 40-60% pro Agent

**Symptom**: `docker stats` zeigte für jeden Agent ~50% CPU. 10 Agents = 5 CPUs nur fürs Warten.

**Root Cause**: `while true; do wait; done` in `entrypoint.sh`. Die POSIX `wait`-Semantik: Ohne Argumente wartet auf alle Background-Jobs (`&`). `entrypoint.sh` hat aber keine Background-Jobs gestartet — alle `tmux`-Calls sind Foreground-Commands. `wait` kehrt sofort zurück → tight loop → 100% CPU.

**Fix**: `exec sleep infinity` als PID 1 Keep-Alive. Kein CPU-Spin.

**Zusätzlich**: POLL_INTERVAL von 2s → 5s. 10 Agents × 2s = 5 Backend-Requests/s, 64% Backend-CPU. Auf 5s → 2 Req/s, 1% CPU.

**Lesson**: `while true; do wait; done` ist nur als Zombie-Reaper sinnvoll wenn es explizit Background-Jobs gibt. Für reines PID-1 Keep-Alive → `sleep infinity`.

### Bug 6: Tastatur-Input ging verloren

**Symptom**: Im Browser-Terminal konnte der Operator nicht tippen. Text erschien nicht im openclaude.

**Root Cause**: Frontend `term.onData((data) => ws.send(data))` sendet plain text (z.B. `"a"`, `"\x1b[A"` für Pfeiltaste). Backend `write_to_pty()` hat **jede** Text-Message als JSON geparst → `JSONDecodeError` bei einfachen Keystrokes → `except` fing den Fehler → Input gedroppt.

**Fix**: Wenn JSON-Parse fehlschlägt ODER wenn parsed value kein dict mit bekanntem `type` → als rohe Eingabe an PTY weitergeben:

```python
try:
    data = json.loads(text)
    if isinstance(data, dict) and data.get("type") in ("resize", "input"):
        # handle structured
        handled = True
except (json.JSONDecodeError, ValueError):
    pass
if not handled:
    os.write(master_fd, text.encode())
```

**Lesson**: Bei dual-format WebSocket-Messages (JSON control + raw text) **nie** JSON-parse in catch-all Try. Immer Fallback zu raw-Text.

### Bug 7: Endpoint-Konflikt `/agents/docker-sessions` → 422

**Symptom**: Sessions-Page zeigte "Verbindung zum Backend fehlgeschlagen". Backend-Logs: `GET /api/v1/agents/docker-sessions 422 Unprocessable Entity`

**Root Cause**: Route `/agents/docker-sessions` (aus `cli_terminal.py`) und Route `/agents/{agent_id}` (aus `agents.py`). FastAPI matched routes in order der Router-Inklusion. `agents.router` war vor `cli_terminal.router` inkludiert → `{agent_id}` matched zuerst → "docker-sessions" wird als UUID geparsed → 422.

**Fix**: Route umbenannt von `/agents/docker-sessions` → `/docker-sessions/agents` (kein Präfix-Konflikt mehr).

**Lesson**: Bei FastAPI-Routen mit Präfix-Konflikten (z.B. `/agents/{id}` vs `/agents/xyz`) **nie** auf Router-Inklusions-Reihenfolge verlassen. Stattdessen: Route-Namen so wählen dass kein Konflikt entsteht, oder Router-Order explizit dokumentieren.

### Bug 8: Bypass-Permissions-Dialog bei jedem Start (zwei verschachtelte Bugs)

**Symptom**: Jeder Agent zeigte beim Start den "Bypass Permissions Mode" Warn-Dialog, obwohl `skipDangerousModePermissionPrompt: true` in der settings.json stand.

**Root Cause A — Symlink im Docker-Mount kaputt**:
`cli-bridge.py` legte `claude-config/settings.json` als **Symlink** auf `../settings.json` an. Auf dem Host funktioniert das — aber der Docker-Container mountet nur `claude-config/` nach `/home/agent/.claude`. Das Parent-Verzeichnis (`~/.openclaw/agents/{slug}/`) ist nicht gemountet. Der Symlink löst im Container zu `/home/agent/settings.json` auf → existiert nicht → settings.json unlesbar → Default-Verhalten (Dialog zeigen).

**Root Cause B — `enabledPlugins` Schema-Fehler**:
Nach Fix von Symlink → settings.json war jetzt lesbar, aber Bypass-Dialog erschien **trotzdem**. Grund: `enabledPlugins` wurde in `cli-bridge.py` als **Array** gerendert (für nicht-claude-Binary Agents). openclaude erwartet aber ein **Record/Object** `{key: boolean}` laut Schema (`exports_external.record(exports_external.string())`). Schema-Validation schlug fehl → openclaude **verwarf die komplette settings.json** → `skipDangerousModePermissionPrompt` wurde ignoriert → Dialog erschien.

**Fix**:
- **A**: cli-bridge.py schreibt `claude-config/settings.json` jetzt als **echte Kopie** statt Symlink. Beide Pfade (`~/.openclaw/agents/{slug}/settings.json` und `~/.openclaw/agents/{slug}/claude-config/settings.json`) rendern aus demselben Jinja2-Template → bleiben automatisch synchron.
- **B**: `enabledPlugins` wird **immer** als dict `{k: True for k in plugins}` gerendert, der `is_claude_bin`-Check entfällt. openclaude IST Claude Code (Fork) und nutzt dasselbe Schema.

**Lesson**: Docker-Mounts und Symlinks sind gefährlich. Symlinks auf Pfade **ausserhalb** des Mount-Volumes brechen lautlos. Bei Docker-kompatiblem Design: echte Dateien kopieren statt Symlinks. Zusätzlich: Schema-Validation-Fehler in Tools wie openclaude sind **oft stumm** (Datei wird verworfen, kein Error-Log) — immer Schema-Definitionen verifizieren bevor man exotic Formate rendert.

## Konsequenzen

### Positiv
- **Docker V2 läuft stabil**: 10 Agents seit 2026-04-08 in Produktion
- **Live-Debugging per Browser-Terminal**: der Operator kann jeden Agent live beobachten und eingreifen
- **Dokumentierte Lessons**: Diese 8 Bugs werden nicht wiederkehren (ADR + Memory-Files)
- **Bessere Tests denkbar**: Für jeden dieser Bugs könnte man einen Integration-Test schreiben

### Negativ
- **Kein Bug war "im Plan"**: Zeigt dass Design-Specs die Deployment-Realität nicht vollständig abdecken können
- **Viel Manual Debugging**: 8 Bugs × manuelle Root-Cause-Analyse = 3-4 Stunden Session-Zeit
- **Template/Mount-Kopplung**: Der cli-bridge.py ist jetzt Docker-aware (keine Symlinks) — das war nicht der ursprüngliche Scope
- **Kommentar-Drift**: Der `cli-bridge.py` Kommentar "claude-Binary erwartet dict, openclaude erwartet Array" war falsch — Kommentare veralten, bei Debug-Sessions immer Code UND Kommentare verifizieren

## Referenzen

Commits (chronologisch):
- `727adf8` — feat: MC V2 Docker agents — live PTY terminal in browser (Bugs 1-4)
- `ec84e5d` — fix: Docker agent CPU-Spike — 3 Ursachen behoben (Bug 5)
- `f3a929f`, `a6ebc21`, `91052a5` — Terminal keystrokes + scroll + skip-permissions flags (Bug 6)
- `4f95fe2` — feat: Sessions Lifecycle-Buttons + Text-Auswahl + Endpoint-Konflikt-Fix (Bug 7)
- `7a840cd` — fix: cli-bridge settings.json — Kopie statt Symlink für Docker-Kompatibilität (Bug 8a)
- `892c137` — fix: enabledPlugins immer als dict — verhindert Schema-Validation-Fehler (Bug 8b)

Memory-Files mit Details:
- `feedback_docker_pid1_wait.md` — Bug 5
- `feedback_terminal_keystroke_forward.md` — Bug 6
- `feedback_docker_symlink_settings.md` — Bug 8a
- `feedback_enabledplugins_dict.md` — Bug 8b

Verwandt: ADR-003 (Triple-Runtime), ADR-011 (HTTP-Polling), ADR-006 (Templates), V2 Design Spec.
