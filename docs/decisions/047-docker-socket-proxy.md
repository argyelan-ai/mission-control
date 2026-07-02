# ADR-047: Docker-Socket-Zugriff nur über filternden Proxy

**Status:** Accepted (2026-07-02)

## Kontext

Der Runtime-Switch (ADR-027/028) braucht Docker-API-Zugriff aus dem
Backend-Container: `docker compose up --force-recreate` für
Cross-Image-Switches, `docker exec tmux respawn-window` für
Same-Image-Switches, `docker inspect/restart` für Health/Env-Refresh.

Bisher war dafür `/var/run/docker.sock` rw in den Backend-Container
gemountet (+ `group_add: "0"`). Das ist die bekannteste Docker-
Sicherheitsfalle: Der Socket ist ungefiltert root-äquivalent auf dem
Host — jede RCE im Backend (FastAPI, ~50 Router) wird zur
Host-Übernahme (privilegierter Container, Host-FS-Mount, beliebige
Images). Für ein Open-Source-Projekt, das Fremde auf ihren Maschinen
betreiben, ist das der erste berechtigte Kritikpunkt.

## Entscheidung

Neuer Compose-Service **`docker-socket-proxy`**
(`tecnativa/docker-socket-proxy`, HAProxy-basierter API-Filter):

- Nur der Proxy mountet den Socket (read-only auf File-Ebene).
- Whitelist exakt der von MC genutzten API-Familien: `CONTAINERS`,
  `IMAGES`, `NETWORKS`, `VOLUMES`, `EXEC`, `INFO`, `POST` (+
  `ALLOW_START/STOP/RESTARTS`). `BUILD`, `SWARM`, `SYSTEM` etc.
  bleiben geblockt — Image-Builds laufen per Design auf dem Host
  (`scripts/build-agent-images.sh`).
- Kein `ports:`-Eintrag — der Proxy ist nur im internen Compose-Netz
  erreichbar.
- Backend: Socket-Mount + `group_add: "0"` entfernt,
  `DOCKER_HOST=tcp://docker-socket-proxy:2375` gesetzt. docker-CLI und
  compose-CLI respektieren `DOCKER_HOST`; alle Subprozesse erben
  `os.environ` (der einzige custom-env-Callsite,
  `docker_agent_sync.py`, kopiert `os.environ`).

## Konsequenzen

- Backend-RCE ≠ Host-Root mehr. Angreifer kann weiterhin MC-Container
  steuern (Container-Lifecycle bleibt mächtig — siehe SECURITY.md),
  aber nicht mehr via `/containers/create` mit Host-Mounts + privileged
  ausbrechen? Doch — **Einschränkung ehrlich benannt:** `POST` +
  `CONTAINERS` erlaubt Container-Create mit beliebigen Binds. Der
  Proxy reduziert die Angriffsfläche (kein Build/Swarm/System, kein
  roher Socket), ist aber keine vollständige Privilege-Boundary.
  Vollständige Isolation bräuchte einen eigenen Broker mit
  Request-Body-Validierung — bewusst vertagt (Aufwand vs. Nutzen,
  Threat-Model ist "trusted network").
- Ein zusätzlicher (winziger, ~10 MB) Always-on-Service.
- Fresh-Boot-E2E im CI bootet den Proxy mit (depends_on backend).

## Verifikation

Live-Proof 2026-07-02: Cross-Image-Runtime-Switch (Sparky omp ↔
qwen-general, `compose up --force-recreate` durch den Proxy) plus
Same-Image-Respawn und Sessions-Terminal (`docker exec`) — alle Pfade
funktionieren mit `DOCKER_HOST` über den Proxy.
