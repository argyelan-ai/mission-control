# ADR-095 — Harden the Docker socket proxy: allow only known bind mounts

**Status:** Accepted
**Datum:** 2026-10-17
**Scope:** Infra/Runtime · Docker · Backend/Services · Security

## Kontext

`backend` and `mc-worker` never talk to `/var/run/docker.sock` directly;
they reach Docker through `docker-socket-proxy` (`DOCKER_HOST` in
`docker-compose.yml`). The proxy (tecnativa/docker-socket-proxy) allows or
blocks **API paths**: containers, images, networks, volumes, exec and info
are allowed, `POST` is allowed, build, swarm and system are blocked.

The comment above that service says a compromised backend cannot take over
the host through Docker. That is not true today, and ADR-094's threat model
depends on saying so honestly:

- The backend writes `volumes:` entries into the agent fleet file
  (`backend/app/services/compose_renderer.py`).
- The fleet file's directory is mounted writable into the backend
  (`./docker:…/docker:rw` in `docker-compose.yml`).
- The backend applies it with `docker compose up --force-recreate`
  (`backend/app/services/docker_agent_sync.py`) through the proxy.
- A path filter cannot see the **body** of `POST /containers/create`. A
  container with `"Binds": ["/:/host"]`, `Privileged: true` or the host PID
  namespace passes, and that container is host root.

So anyone who controls the backend process controls the host. The
self-update (ADR-094) does not make this worse, but it cannot be called safe
while this stays open.

## Entscheidung

1. **The proxy checks what a container may get, not only which API path is
   called.** Container create and update requests are inspected; a request
   is refused (`403` with a reason) when it asks for:
   - a bind mount whose host source is not on the **allowlist** (decision 2),
   - the Docker socket itself, in any form,
   - `Privileged`, host PID/IPC/network namespaces, host devices,
     `CapAdd` beyond a short fixed list, or `SecurityOpt` that disables
     confinement (`apparmor=unconfined`, `seccomp=unconfined`).
   Named volumes stay allowed. Read-only calls are unchanged.

2. **The allowlist is derived, not hand-written per machine.** It is built
   from the paths MC itself renders for agents: below `HOME_HOST/.mc/` (vault,
   shared MCP screenshots, sessions), below `MC_REPO_PATH`, and the project
   roots the user configured for agents (read-only where the renderer mounts
   read-only). A source outside these roots, or a `..` escape after
   normalising the path, is refused. Symlinks are resolved on the host side
   before the check.

3. **Defence in depth at the source.** The compose renderer validates every
   mount it writes against the same allowlist, so a wrong entry fails at
   render time with a clear message instead of at the proxy. The proxy stays
   the boundary that holds even if the renderer is bypassed.

4. **Implementation choice is made in the implementing PR, with proof.**
   tecnativa/docker-socket-proxy filters paths only and is expected not to
   inspect bodies — the PR proves that with a test. The replacement is
   either a body-aware proxy or a small MC-owned filter in front of the
   socket. Either way it **replaces** tecnativa (PRINCIPLES §2 rule 5: no
   added component without removing one), its version is pinned, and it
   keeps today's path rules (`BUILD=0`, `SWARM=0`, `SYSTEM=0`).

5. **Proof before done.** Through the proxy, creating a container with
   `/:/host`, with `Privileged`, and with the Docker socket mounted is
   refused; a normal agent recreate (vault, workspace, sessions mounts) still
   works; a sabotage that removes the body check turns the test red.

6. **The misleading comment in `docker-compose.yml` is corrected** in the
   implementing PR to say what the proxy protects against, and what not.

## Alternativen

- **Keep the path-only proxy and rely on code review** → Rejected: the risk
  is a compromised backend, which code review does not stop.
- **Rootless Docker or user-namespace remapping** → Not chosen as the main
  fix: a good additional layer, but platform-dependent (Docker Desktop on
  macOS, Raspberry Pi OS) and a host-level change for the installer, not
  something MC can guarantee on every install. May be offered later with an
  explicit yes (ADR-093 decision 3).
- **Move all container management to a host service** → Rejected for now:
  it moves the same power to another process in the same user account and
  adds a lifecycle; the body check closes the actual hole.
- **Read-only fleet file in the backend** → Rejected as the fix: the backend
  has to write the fleet file to create and switch agents; protecting the
  file does not stop a direct API call.

## Später ändern (ADR-093)

The user does not edit the allowlist: it follows from the paths MC renders
and from the project roots the user sets for agents in MC. When a new kind of
mount is needed (a new harness, a new shared folder), it is added once in the
renderer's mount templates, which feeds both the renderer check and the
proxy — not as a per-machine exception. A refused request shows up as an
error with its reason on the agent's start/recreate, so the user sees why an
agent did not start.

## Konsequenzen

### Positiv
- A compromised backend can no longer turn a container create into host
  root through bind mounts, privileged mode or the socket.
- ADR-094's threat model can be updated from "backend ≈ host root" to a
  narrower statement once this is live, and the bridge-versus-own-service
  question gets re-evaluated on real ground.
- The renderer fails early with a clear message on a wrong mount.

### Negativ
- A body-aware filter is security-critical code; a bug can block legitimate
  agent recreates. Mitigated by tests for every mount MC renders today and by
  a pinned version.
- Hand-added mounts in a user's fleet file outside the allowed roots stop
  working; the error says which path and how to allow it (configured project
  roots).
- Until it lands, ADR-094 runs with the hole open — stated there.

## Referenzen

- Betroffene Dateien: `docker-compose.yml` (`docker-socket-proxy`, `DOCKER_HOST`,
  fleet-file mount), `backend/app/services/compose_renderer.py`,
  `backend/app/services/docker_agent_sync.py`
- Verwandte ADRs: ADR-094 (self-update, threat model), ADR-093 (host-level
  changes stay with the installer)
- Externe Quellen: Docker Engine API `POST /containers/create`
  (https://docs.docker.com/reference/api/engine/)
