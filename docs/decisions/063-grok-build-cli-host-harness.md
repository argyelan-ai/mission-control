# ADR-063 — Grok Build CLI als host-side Harness

**Status:** Accepted
**Datum:** 2026-07-10
**Scope:** Backend/Provisioning, Infra/Host-Bridge, Backend/Runtime

## Kontext

xAI liefert mit `grok build` einen offiziellen CLI-Coding-Agenten (`brew install --cask
grok-build`, Binary `/opt/homebrew/bin/grok`, v0.2.93). Er läuft auf dem Host-Mac und ist per
OAuth mit Marks X-Premium+-Abo eingeloggt (`~/.grok/auth.json`, Auto-Refresh, **kein API-Key**,
Grenzkosten 0). Verifizierter Spike (2026-07-10):

- Headless: `grok -p "<prompt>"` bzw. `--prompt-file <path>`, single-turn, stdout, exit.
- `--output-format streaming-json` → NDJSON-Events: `{"type":"thought","data":…}`,
  `{"type":"text","data":…}`, terminal `{"type":"end","stopReason":"EndTurn","sessionId":"<uuid>",
  "requestId":"…"}`.
- Session-Kontinuität: `-s <uuid>` benennt eine **neue** Session, `-r <SESSION_ID>` resumt.
- `--permission-mode default|acceptEdits|auto|dontAsk|bypassPermissions|plan`, `--cwd`, `--max-turns`.
- Der CLI spricht **ausschliesslich** mit der xAI-Cloud (`cli-chat-proxy.grok.com`) über seine
  eigene OAuth-Session — keine `OPENAI_*`/`ANTHROPIC_*`-Env-Konfiguration möglich oder nötig.

ADR-060 hat mit dem `HostHarnessAdapter`-Layer bereits das generische Muster gelegt, einen zweiten
Host-Harness ohne Hardcode-Branches anzudocken. Grok ist der erste Test dieses Musters — und
strukturell **anders als Hermes**: Hermes ist eine persistente tmux-TUI, an eine vLLM-Runtime
gebunden, in die Dispatches als Prompt gepastet werden. Grok ist ein **headless per-Dispatch
Subprocess** mit NDJSON-Stream und eigener Cloud — es gibt keine tmux-Session und kein
MC-gebundenes Modell.

## Entscheidung

Grok wird als host-side Harness `grok` integriert — nach dem Hermes-/ADR-060-Vorbild bei den
geteilten Bausteinen (launchd, `agent.env`, Workspace-Layout, Provisioning-Dispatch), aber mit
einem headless Subprocess-Delivery-Modell nach dem Vorbild der omp-Bridge.

### 1. `scripts/grok-bridge.py` — headless Poll→Dispatch→Reduce→Lifecycle

Pattern-Quelle: `scripts/hermes-bridge.py` (Poll-Loop, stetiger Heartbeat, SIGTERM-Handling,
localhost-only HTTP-Control-Server) + `docker/omp-bridge/bridge.py` (headless Subprocess,
streaming-NDJSON-Reducer, out-of-band Wall-Clock/Idle-Watchdog, mc-cli-Lifecycle).

Ablauf pro Dispatch:

```
GET /api/v1/agent/me/poll  (CLAIM: setzt dispatched_at/ack_at + in_progress)
  state=new_task →
    write /tmp/mc-context.env   (TASK_ID / BOARD_ID / X_DISPATCH_ATTEMPT_ID)
    mc ack <task>               (schützt vor 10-Min-ACK-Timeout-Redispatch)
    session_id = uuid4()        (Runde 1: neue benannte grok-Session)
    grok --prompt-file <p> --output-format streaming-json --cwd <workspace>
         --permission-mode acceptEdits --session-id <uuid>
    reduce NDJSON:
      thought → zählen; text → final_text; end → stopReason + sessionId
      (jede Zeile refresht Liveness — Watchdog killt bei Wall-Clock/Idle-Timeout)
    map_lifecycle(outcome):
      EndTurn + exit 0 + kein Fehler → mc finish --review
      sonst (watchdog / kein end / error / non-EndTurn / exit≠0) → mc blocked
    sessionId aus end-Event merken (Follow-up-Kommentare → grok -r <sessionId>)
```

**Bridge-getriebener Lifecycle (nicht LLM-getrieben):** Weil grok headless ist, kann ihm nicht
zugetraut werden, jeden Lauf verlässlich in einen Terminalzustand zu bringen. Die Bridge besitzt
`ack`/`finish`/`blocked` deterministisch (omp-Modell: **immer terminal, nie still in_progress**).
Der grok-Agent selbst registriert Deliverables/Kommentare via die kopierte `mc`-CLI (mc-context.env-
Contract) — der Dispatch-Prompt weist ihn explizit an, **nicht** selbst `mc done`/`finish` zu rufen.

**Session-Kontinuität pro Task:** `_task_sessions[task_id]` merkt die grok-`sessionId`.
Folge-Kommentare auf einem bereits dispatchten Task resumen dieselbe Konversation via `grok -r`.

**Robustheit:** out-of-band Reader-Thread + Wall-Clock- (`GROK_TASK_DEADLINE`, 1800s) und
No-Progress-Watchdog (`GROK_IDLE_TIMEOUT`, 300s); SIGTERM → sauberer Exit 0 (launchd `KeepAlive`
restartet nicht bei absichtlichem Stop); Crash → `[fatal]` + `SystemExit(1)`; `POST /stop` setzt
ein Cancel-Event, `POST /restart` leert den Session-Cache. Port **18795** (hermes 18794,
free-code 18792/18793), Bind **127.0.0.1** only.

### 2. `GrokAdapter` (`host_harness_adapter.py`)

`harness="grok"`, `protocol="grok"`. `build_agent_env` rendert **keine** Provider-Env — nur
`MC_AGENT_TOKEN`/`MC_BASE_URL`/`HOME`/`PATH` (via `build_grok_agent_env` in `agent_bootstrap.py`).
`bootstrap` delegiert an `bootstrap_grok_agent`. `reload` nutzt den generischen
`_host_agent_lifecycle(agent,"restart")`-Pfad (launchctl kickstart des grok-bridge-plist) — nicht
den Hermes-Bridge-HTTP-Sonderfall, weil grok keine persistente Session hat: Bridge-Restart
re-sourct `agent.env` für den nächsten Dispatch, das **ist** der Reload.

`sync_host_agent_model` wird protokoll-bewusst: für protokoll-fixe Harnesses (grok) ist es ein
No-Op — es gibt keine `OPENAI_*` zu syncen, und `build_runtime_env` würde für einen `grok`-Runtime
sonst fälschlich `OPENAI_BASE_URL`/`OPENAI_MODEL` aus dem Display-Anker ableiten.

### 3. Protokoll-Einordnung (`harness_compat.py`) — die minimalste konsistente Lösung

Grok ist **protokoll-fix**: der CLI kann nicht auf einen OpenAI/Anthropic-Endpoint gezeigt werden.
Statt einen Runtime-losen Sonderpfad zu bauen, bekommt grok sein eigenes Wire-Protokoll:

- `HARNESS_PROTOCOLS["grok"] = {"grok"}` (host-only, wie hermes — **nicht** in `HARNESSES`, also
  nicht in der cli-bridge-Switch-Matrix).
- `runtime_protocol()` klassifiziert `runtime_type=="grok"` als `"grok"`.
- Seed-Runtime `grok-cloud` (`runtime_type:"grok"`, Endpoint `cli-chat-proxy.grok.com`,
  `model:"grok-4.5"`, `single_instance:true`) in `backend/config/runtimes.json`.

Damit läuft grok durch **dasselbe** `is_compatible()`-Gate wie jeder andere Host-Harness: ein
grok-Agent bindet nur `grok-cloud`, jede openai/anthropic-Runtime ist ein sauberes 422. Der
Runtime-Endpoint/Modell sind reiner Display-Anker (analog zur kosmetischen ollama-cloud-Bindung
aus ADR-060) — grok liest sein Modell aus seiner eigenen Cloud-Session.

### 4. Provisioning

Der ADR-060-Dispatch (`routers/agents.py`) bleibt unangetastet: `get_adapter("grok")` +
`is_compatible()` reichen. `bootstrap_grok_agent` spiegelt `bootstrap_hermes_agent` (Token,
`agent.env` mode 600, Config-/Workspace-/Logs-Dirs, launchctl `com.mc.grok-bridge.plist`,
MC-Dev-Board-Auto-Assign, Vault-Token-Rotation, `agent.grok_provisioned`-Event) — Rückgabe-Shape
identisch, `tmux_session=None` (headless). `_HOST_AGENT_PLISTS["grok"]` verweist auf das plist.

## Alternativen

- **grok als weiteres Hermes-artiges tmux-TUI-Modell.** Verworfen — grok build ist kein
  persistenter TUI-Worker, den man mit `send-keys` füttert; der headless `-p`/`streaming-json`-Pfad
  ist der native, robustere Weg (deterministischer Lifecycle, Watchdog greift).
- **Runtime-loser Sonderpfad (grok braucht keine Runtime-Bindung).** Verworfen — würde den
  ADR-060-Dispatch (`agent.runtime_id` Pflicht, `is_compatible()`-Gate) aufweichen. Ein eigenes
  `"grok"`-Protokoll + Display-Anker-Runtime hält grok im **selben** generischen Gate, ohne
  Sonderfälle im Provisioning.
- **LLM-getriebener Lifecycle (grok ruft `mc done` selbst, wie Hermes).** Verworfen als
  Terminal-Garantie — ein headless one-shot Lauf, der abstürzt/getimeoutet wird, hinterliesse
  einen still hängenden `in_progress`-Task. Die Bridge besitzt den Endzustand (omp-Prinzip); grok
  registriert nur Deliverables/Kommentare.
- **Provider-Env injizieren (`OPENAI_*`/`ANTHROPIC_*`).** Unmöglich/sinnlos — grok konfiguriert
  seinen Provider nicht über Env, sondern über die eigene OAuth-Session.

## Konsequenzen

### Positiv

- **Zweiter Host-Harness ohne Hardcode-Branch** — ADR-060 hält, der Adapter-Registry-Eintrag +
  Bootstrap + ein Protokoll-Tag genügen; `routers/agents.py`/`runtime_propagation`/`switch` bleiben
  unangetastet.
- **Grenzkosten 0** — läuft auf Marks X-Premium+-Abo, kein API-Key, kein lokales GPU.
- **Deterministischer, hang-sicherer Lifecycle** — Wall-Clock/Idle-Watchdog + bridge-getriebenes
  finish/blocked; ein Lauf erreicht immer einen Terminalzustand.
- **Das generische Muster ist an einem strukturell anderen Harness verifiziert** (headless statt
  TUI, eigene Cloud statt vLLM) — der offene Punkt aus ADR-060 §Negativ ist damit adressiert.

### Negativ

- **Neues Protokoll-Tag `"grok"`** in der Compat-Matrix — ausschliesslich mit sich selbst
  kompatibel. Sauber, aber die Matrix wächst pro proprietärer Cloud-CLI um einen Eintrag.
- **Display-Anker-Runtime bleibt kosmetisch** — `grok-cloud` Endpoint/Modell werden nicht gelesen;
  dieselbe „zwei Wahrheiten"-Fussnote wie bei hermes/ollama-cloud (ADR-060 §Negativ).
- **Kein Live-Provisioning in dieser Runde** — Adapter/Bridge/Tests sind gebaut und grün, aber das
  tatsächliche `POST /provision` gegen den Host (launchctl, echter grok-Lauf, Abo-Rate-Limits)
  bleibt Marks Gate. Der grok-Binary wurde nur read-only smoke-gecheckt (`grok --help`).
- **Session-Cache ist prozess-lokal** — ein Bridge-Restart verliert die `-r`-Resume-Fähigkeit für
  laufende Tasks (Folge-Kommentare starten dann kalt via nächstem Full-Dispatch). Akzeptabel;
  Persistenz wäre Overkill für v1.

## Referenzen

- Betroffene Dateien:
  - `scripts/grok-bridge.py` (neu)
  - `backend/app/services/host_harness_adapter.py` (`GrokAdapter`, `sync_host_agent_model`-Guard)
  - `backend/app/services/agent_bootstrap.py` (`build_grok_agent_env`, `bootstrap_grok_agent`,
    `GROK_PLIST_PATH_REL`)
  - `backend/app/services/harness_compat.py` (`HARNESS_PROTOCOLS["grok"]`, `runtime_protocol`)
  - `backend/app/routers/cli_terminal.py` (`_HOST_AGENT_PLISTS["grok"]`)
  - `backend/config/runtimes.json` (`grok-cloud`-Seed)
  - `docker/grok/com.mc.grok-bridge.plist` (neu)
  - Tests: `backend/tests/test_grok_bridge.py`, `test_grok_provisioning.py`,
    `test_host_harness_adapter.py` (grok-Fälle)
- Verwandte ADRs: **ADR-060** (HostHarnessAdapter — das Muster, das grok anwendet),
  **ADR-056** (Harness/Provider-Decoupling — `is_compatible()`/`protocol`), **ADR-049/045**
  (omp headless/native — Vorbild für streaming-NDJSON-Reduce + mc-cli-Lifecycle),
  **ADR-029** (Hermes host-side Worker — Bootstrap-/launchd-Vorbild).
- Externe Quellen: xAI Grok Build CLI v0.2.93 (`brew install --cask grok-build`).
