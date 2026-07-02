# omp + Qwen Runtime — Abschlussbericht

**Stand:** 2026-07-02 · **Worktree:** `.worktrees/omp-runtime` (Branch, von `main`) · **Nicht gemerged, nichts gebaut, nichts geschaltet — alles GATED.**

---

## 1. Exec Summary — ehrliche Einschätzung

**Verdict: Code + Routing + Tests sind FERTIG und grün. Registrieren + Live-Switch sind READY, aber noch nicht am echten Container/Qwen bewiesen.**

| Bereich | Status |
|---|---|
| Routing (Image, Token, .env, Readiness) | ✅ Fertig, an echtem Code verifiziert, Tests grün |
| `bridge.py --serve` (Poll-Loop + Lifecycle) | ✅ Real implementiert (war im Design noch offen) |
| Backend-Tests (12 omp) + Bridge-Tests (7 serve + 17 golden) | ✅ 36/36 grün |
| ADR-045 + ARCHITECTURE.md §6 | ✅ Vorhanden |
| Docker-Image `mc-omp-agent` bauen | ⛔ GATED — nie gebaut |
| Runtime-Row in DB registrieren | ⛔ GATED — Seed vorbereitet, nicht angewandt |
| Sparky live umschalten | ⛔ GATED — braucht Build + Mensch |

**Was einem funktionierenden Live-Switch noch im Weg steht (harte Gates, kein Code-Problem):**
1. **Modell-ID unbestätigt.** Seed nutzt `nvidia/Qwen3.6-35B-A3B-NVFP4`. Der **hermes**-Eintrag auf **demselben** vLLM (`:8000`) nennt `Qwen/Qwen3.6-35B-A3B-FP8`. Ein vLLM serviert **eine** Modell-ID → max. eine stimmt. **Vor dem Switch `GET http://192.0.2.100:8000/v1/models` abfragen und die echte ID setzen.** Falsche ID = omp startet nicht → jede Task sofort `blocked` (terminal, aber 100 % funktionslos).
2. **omp-Paket-Pin ungetestet.** `ARG OMP_PACKAGE=omp@16.2.13` — Vendor/Name/Pin muss vor dem ersten `docker build` bestätigt werden.
3. **Live-Sentinel/Reflection unverifiziert.** Der `TASK_COMPLETE` + 4-Feld-Reflection-Vertrag ist nur gegen synthetische/Qwen-förmige Fixtures getestet. Verlässlichkeit auf echtem Qwen (False-Pos/Neg) ist ein Phase-2-Gate.

---

## 2. Was gebaut wurde

### Routing — genau 3 Verzweigungspunkte, **null** dupliziertes Token-Routing
| Punkt | Datei:Zeile | Änderung |
|---|---|---|
| (a) Image-Wahl | `compose_renderer.py:71-93` | `if rt_type=="omp": return OMP_IMAGE` (`mc-omp-agent:latest`), **vor** der openclaude-Allowlist (gab vorher `None` zurück → brach `detect_image_change`). 3-Wege-Anchor im `_build_new_agent_block`. |
| (b) .env-Token (Single-Path) | `internal.py:55-67` `build_runtime_env` | Expliziter `omp`-Branch analog `hermes` → `OPENAI_BASE_URL` + `OPENAI_MODEL` aus `runtime.endpoint`/`.model_identifier`. **Keine** anthropic-Tokens, **kein** Vault-Lookup (Test: `assert_not_called`). |
| (c) docker_agent_sync .env | `docker_agent_sync.py:212,304-320` | **Kein neuer Branch.** Slug `omp-qwen` ist non-anthropic → fällt in den bestehenden OpenAI-Zweig. **Das ist die „keine Duplizierung"-Garantie.** |

### Readiness — der subtile, korrigierte Punkt
- `wait_for_agent_healthy` + `_wait_for_window_ready` (`docker_agent_sync.py:551,783,813`): routet zum **Pane-Scrape**, wenn `respawn_mode` **ODER** `ready_signals` gesetzt ist.
- **Warum wichtig:** Der erste Sparky-Switch (openclaude → omp) ist **cross-image** → `respawn_mode=False`. Ohne den `ready_signals`-Pfad hätte `docker inspect "running"` genügt → **False-Positive** auf einer crash-loopenden bridge (tmux ist PID 1). Jetzt wird auf `OMP_BRIDGE_READY` gescraped; `ready_signals` **ersetzt** die Glyph-Tuple (kein `$ `-Fehltreffer). `agent_runtime_switch.py` gibt `ready_signals=('OMP_BRIDGE_READY',)` nur bei `runtime_type=='omp'` mit.

### Image / Entrypoint / Bridge
- `docker/omp-bridge/bridge.py`: `--serve` **real** — `serve_loop` (Poll `/api/v1/agent/me/poll` → ack-dedup auf `dispatch_attempt_id` → `drive_live_run` → immer terminal `finish|blocked`), `McCliLifecycle` (shellt zu `mc ack/finish/blocked`, Kontext via env), Prompt-Wrapping (`TASK_COMPLETE` + Reflection), `container_workspace_path` (Host→`/workspace`), `_default_model_selector` (`qwen-spark/<model>`). Druckt `OMP_BRIDGE_READY` genau einmal nach erstem Poll.
- `entrypoint.sh`: MC-Bootstrap, **droppt** das falsche `ANTHROPIC_OAUTH_TOKEN`-Re-Export, rendert omp `models.yml` (Provider `qwen-spark`, `auth: none`) am Profilpfad `$HOME/.omp/profiles/mc-agent/agent/models.yml`, 3-Fenster-tmux.
- `Dockerfile`: `omp`-Install (`ARG OMP_PACKAGE=omp@16.2.13`) + `mc-cli` COPY/Install. **Build bleibt GATED.**
- `omp-recycler.sh`: `PROCESS_NAME=bridge.py`, respawnt Window-0 via `tmux respawn-pane -k`, killt nie idle während `.task-active.lock` gehalten wird.

### Registrierungs-Skript
`docker/omp-bridge/register-omp-runtime.sh` — idempotent (`POST /api/v1/runtimes/db`, 409 = schon da).

### Tests — bestanden (frisch verifiziert)
```
tests/test_omp_runtime.py .................... 12 passed in 1.91s
docker/omp-bridge/tests/test_serve_loop.py .... 7 passed, 0 failed
docker/omp-bridge/tests/test_bridge.py ........ 17 passed, 0 failed
```
Breite Selektion `-k "runtime or build_runtime_env or image or switch or omp"`: **311 passed, 1 failed** — der einzige Fehler (`test_hermes_skill::test_frontmatter_complete`) ist **vorbestehend + umgebungsbedingt** (liest `~/.hermes/skills/.../SKILL.md`, existiert auf dieser Maschine nicht), **unabhängig** von der omp-Arbeit.

---

## 3. Wie es im UI erscheint + für Sparky wählbar wird

**Runtimes-Row** (aus `runtimes.json`-Seed, `slug=omp-qwen`):

| Feld | Wert |
|---|---|
| Anzeigename | **omp headless (Qwen)** |
| runtime_type | `omp` (neu, distinkt) |
| Endpoint | `http://192.0.2.100:8000/v1` (DGX-Spark Qwen vLLM) |
| Modell | `nvidia/Qwen3.6-35B-A3B-NVFP4` (⚠ vor Switch gegen `/v1/models` prüfen) |
| Image | `mc-omp-agent:latest` (aus `runtime_type`, nicht als Feld) |
| supports_tools / reasoning / streaming | true / true / true |
| enabled | true · ui_order 10 · role_tags `[coder, general]` |

- **Sichtbar** auf `/runtimes` nach Schritt 2 (Registrieren).
- **Wählbar für Sparky:** `validate_compatibility` gibt für omp (enabled + supports_tools) **null Warnungen** und keinen Hard-Error für einen cli-bridge-Agent → in der Agent-Runtime-Auswahl erscheint „omp headless (Qwen)".
- **Hinweis (UX, nicht blockierend):** `runtime_manager.get_runtime_state` hat keinen omp-Case → die Runtime-Karte zeigt **„unknown"** statt Erreichbarkeit zu proben (hermes verhält sich identisch). Fix optional: `'omp'` in die `('openai_compatible','cloud')`-Probe-Tuple (`runtime_manager.py:383`).

---

## 4. GATED PRODUCTION STEPS (Mensch, in dieser Reihenfolge — jeder reversibel)

```bash
# --- 0. VERIFY (Worktree-venv + Bridge-Suites) — bereits grün ---
cd <repo-root>/backend
.venv/bin/python -m pytest tests/ -k "runtime or build_runtime_env or image or switch or omp" -q
cd .. && OMP_TASK_LOCK_FILE=/tmp/omp.lock python3 docker/omp-bridge/tests/test_serve_loop.py \
  && python3 docker/omp-bridge/tests/test_bridge.py

# --- PRECHECK (Gate 1): echte Modell-ID bestätigen, Widerspruch mit hermes auflösen ---
curl -s http://192.0.2.100:8000/v1/models | jq '.data[].id'
#  -> stimmt die ID NICHT mit runtimes.json (nvidia/Qwen3.6-35B-A3B-NVFP4) überein:
#     backend/config/runtimes.json:omp-qwen.model_identifier anpassen BEVOR gebaut/registriert wird.

# --- 1. BUILD (GATED: docker build). omp-Pin ggf. via --build-arg überschreiben ---
scripts/build-agent-images.sh mc-omp-agent        # materialisiert mc-cli/ + baut mc-omp-agent:latest
#  REVERSIBLE: docker image rm mc-omp-agent:latest

# --- 2. REGISTER (GATED: schreibt echte DB). Idempotent ---
bash docker/omp-bridge/register-omp-runtime.sh     # POST /api/v1/runtimes/db (409 = schon da)
#  ODER Seed-Pfad: `docker compose up -d backend` -> lifespan seed_runtimes() (INSERT-only)
curl -s localhost:8000/api/v1/runtimes | jq '.[] | select(.slug=="omp-qwen")'   # erscheint in /runtimes
#  REVERSIBLE: erst Agent zurückschalten (Schritt 3-Umkehr), DANN
#    PATCH /db/omp-qwen {"enabled":false}  (bevorzugt vor DELETE, solange ein Agent referenziert)

# --- 3. SWITCH Sparky (GATED: rekreiert Container, CROSS-image -> respawn_mode=False) ---
curl -s -X PATCH localhost:8000/api/v1/agents/<sparky-id> \
  -H 'content-type: application/json' -d '{"runtime_id":"<omp-qwen-uuid>"}'
#  Health-Check scraped Window-0 auf OMP_BRIDGE_READY (NICHT docker-inspect).
#  Kein Sentinel in HEALTH_TIMEOUT_RECREATE -> automatischer _rollback auf openclaude.
#  REVERSIBLE: gleicher PATCH zurück auf die alte runtime_id (Switch-Service force-recreate).

# --- 4. VERIFY: eine Smoke-Task dispatchen; muss finish|blocked auflösen (nie stuck in_progress) ---
```

**Reversal-Reihenfolge ist wichtig:** *erst* Agent zurückschalten (Container geht sauber aufs richtige Image), *dann* Row `enabled=false`/löschen. Umgekehrt nullt `DELETE` (ON DELETE SET NULL) nur `agent.runtime_id` und lässt Sparky auf dem omp-Container ohne Binding zurück (halb-reverted).

---

## 5. Risiken + offene Fragen

### Risiken (v. a. ohne echten Container/Switch unverifiziert)
| Sev | Risiko |
|---|---|
| **hoch** | **Modell-ID-Widerspruch** mit hermes (siehe §1). Falsche ID → 100 % `blocked`. Precheck ist Pflicht-Gate. |
| **mittel** | **`McCliLifecycle` schluckt den mc-CLI Exit-Code** (`bridge.py:861-875`): schlägt `mc finish` transient fehl (Netz/Reflection abgelehnt), bleibt die Task `in_progress` — genau der silent-hang, den diese Runtime schließen soll, für den finish-reject-Fall wieder offen. **Empf. Fix vor Live:** bei fehlgeschlagenem `finish` → Fallback `mc blocked`; bei fehlgeschlagenem up-front `ack` → Task nicht starten. |
| **mittel** | **Terminal-Garantie ist lokal**, nicht backend-verifiziert — bridge garantiert, dass ein terminaler mc-Befehl *aufgerufen* wird, nicht dass die Backend-Transition gelang. |
| **mittel** | **omp-Paket-Pin** (16.2.13) + Dockerfile-Build nie ausgeführt. |
| **niedrig** | **Reflection-Vertrag in 3 Kopien** (`constants.py`, `mc_cli`, `bridge.py`) — Drift → bridge klassifiziert FINISH, `mc finish` lehnt ab. Drift-Test empfohlen. |
| **niedrig** | **Cancel/Stop erst nach dem bounded Run beobachtet** (serve_loop ist synchron); kein Mid-Run-Abort. Beschränkt durch Watchdog (~17 min). |
| **niedrig** | **Sessions-Terminal** zeigt bridge.py-Logs statt interaktiver Pane — UX-Bestätigung offen. |
| **niedrig** | **`get_runtime_state` = „unknown"** für omp (UX, siehe §3). |

### Offene Fragen (Phase-2-Gates)
1. Echte Qwen-Modell-ID via `/v1/models` — und hermes-Eintrag gleich mitkorrigieren (einer der beiden ist stale).
2. `TASK_COMPLETE` + 4-Feld-Reflection: Verlässlichkeit auf **echtem** Qwen (False-Pos/Neg-Zählung) — der eine real erfasste Stream hatte keinen Sentinel und blockte korrekt.
3. omp-Vendor/Name/Pin final bestätigen, bevor `docker build` läuft.
4. `mc finish`-Fehlerpfad härten (Fallback `mc blocked`), damit die Terminal-Garantie backend-verifiziert ist.
5. `cwd`-Zustellung: `dispatch._container_workspace_path` für `omp --cwd` + Null-Workspace-Fallback (ad-hoc-Task) bestätigen.
6. Erster Live-Switch ist bis Phase-2 evtl. Auto-Rollback — das ist das **Sicherheitsnetz**, kein Datenverlust.
