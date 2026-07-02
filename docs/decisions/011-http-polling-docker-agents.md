# ADR-011 — HTTP-Polling für Docker-Agents (statt Push)

**Status:** Accepted
**Datum:** 2026-04-07
**Scope:** Infra/Dispatch

## Kontext

Docker-V2-Agents (mc-agent-{slug}) müssen Tasks bekommen. Zwei grundsätzliche Modelle:

1. **Push** (Backend sendet, Agent empfängt): WebSocket, gRPC bidirectional, Message Queue
2. **Pull/Poll** (Agent fragt, Backend antwortet): HTTP GET alle N Sekunden

Für Docker-Container-Setting gelten zusätzliche Constraints:
- Container haben keine stabile Inbound-Adressen (Container Names, kein DNS ausserhalb)
- Backend muss nicht "wissen" welche Container gerade laufen (stateless)
- Bei Agent-Restart dürfen keine Tasks verloren gehen

## Entscheidung

**HTTP-Polling** — Agent (poll.sh im Container) macht HTTP GET auf `/api/v1/agent/me/next-task` alle 5 Sekunden. Bei Task-Verfügbarkeit: JSON-Response mit Prompt, Agent verarbeitet.

Zusätzlich:
- **Heartbeat**: Alle 30s POST `/api/v1/agent/me/heartbeat` → Backend weiss Agent lebt
- **Agent-Auth**: Bearer-Token aus `MC_TOKEN` Env-Var
- **Completion-Detection**: poll.sh prüft tmux-Pane Output-Stability (6×5s ohne Änderung → Task fertig)
- **Recovery**: Nach Agent-Restart pollt poll.sh wieder → falls Task noch `dispatched` ohne ACK → neuer Dispatch-Versuch

## Alternativen

- **A: WebSocket** (wie Gateway) → verworfen weil:
  - Backend muss Connection-State pro Container halten (stateful)
  - Reconnect-Logik im Agent komplexer
  - WebSocket-Proxy durch Caddy zusätzlicher Layer
  - Für einen einfachen "hast du was für mich?" Flow overkill
- **B: Message Queue** (Redis Streams, RabbitMQ) → verworfen weil:
  - Mehr Infrastruktur (RabbitMQ ein weiterer Container)
  - Dispatcher-Logik bleibt gleich — Queue ist nur Transport-Layer
  - Agent muss trotzdem Queue-Client-Lib haben (nicht trivial in Bash)
- **C: Server-Push via SSE** → verworfen weil:
  - SSE ist einseitig (Backend → Agent)
  - Agent müsste zusätzlich POST für Heartbeat
  - Im Agent-Container nur `curl`, kein SSE-Client
- **D: Webhooks** (Backend HTTP-POST an Agent-URL) → verworfen weil:
  - Backend müsste Container-URLs kennen
  - NAT/Network-Setup würde kompliziert
- **E: File-Queue wie Host-cli-bridge** → verworfen weil:
  - Volume-Shared-State zwischen Backend + Container problematisch (Mount-Konflikte)
  - File-Watching im Container (inotify) nicht reliable

## Konsequenzen

### Positiv
- **Stateless Backend**: Backend hat keine per-Container State — skalierbar, einfach zu testen
- **Simple Implementation**: `curl` + `python3` reichen — keine zusätzlichen Libraries
- **NAT-Friendly**: Agent startet alle Connections → Inbound-Netzwerk-Config nicht nötig
- **Resilient**: Agent-Crash, Backend-Restart, Network-Glitch → next Poll kommt, alles continues
- **Einfaches Debugging**: `docker logs mc-agent-{slug}` zeigt alle Polls, klar sichtbar was passiert

### Negativ
- **Latenz**: Max 5s bis neuer Task gesehen wird (akzeptabel für AI-Tasks die eh Minuten dauern)
- **Backend-Load**: 10 Agents × Poll alle 5s = 2 Requests/s nur für next-task + 10× alle 30s Heartbeat. Bei 100 Agents wäre das deutlicher.
  - Ursprünglich 2s Interval → verursachte spürbare CPU-Last am Backend, auf 5s erhöht
- **Polling-Waste**: Meiste Polls kriegen `{"task": null}` zurück — "leere" Requests verbrauchen Bandwidth
- **ACK-Handshake wichtig**: Ohne ADR-001 könnte Race entstehen (zwei Polls von zwei Agent-Instanzen, beide bekommen denselben Task)

## Referenzen

- Agent-Endpoint: `backend/app/routers/agents.py` (`GET /agent/me/next-task`, `POST /agent/me/heartbeat`)
- Poll-Loop: `docker/mc-agent-base/poll.sh` (`poll_next_task()`, `heartbeat()`)
- Interval-Tuning Commit: `ec84e5d` (2s → 5s nach CPU-Lastanalyse)
- Verwandt: ADR-001 (ACK-Handshake — essentiell für Polling-Race-Safety), ADR-003 (Triple-Runtime)
