# Security Policy

## Reporting a vulnerability

Please **do not open a public issue** for security vulnerabilities. Use
GitHub's private vulnerability reporting ("Report a vulnerability" under the
Security tab) so the maintainers can triage and fix before disclosure.

## Scope & threat model

Mission Control is designed to run **on hardware you own, on a network you
trust** (localhost or a private VPN such as Tailscale). It is not hardened
for direct internet exposure. In particular:

- The backend never talks to `/var/run/docker.sock` directly. It goes
  through a filtering proxy (`docker-socket-proxy`, only reachable on the
  internal compose network) that whitelists the API paths the runtime-switch
  feature needs (container lifecycle, exec, images/networks/volumes) and
  blocks the rest (build, swarm, system). A compromised backend can manage
  MC's containers but cannot use the Docker API to take over the host —
  note that container lifecycle control is still a powerful capability.
- Agents execute code and shell commands by design. Their capabilities are
  limited by scopes and per-agent tokens, but an agent with `deploy:execute`
  or a broad workspace mount can affect the host.
- All service ports except the Caddy proxy bind to `127.0.0.1`.

## Hardening checklist for operators

- Keep the stack behind a VPN; never port-forward it to the internet.
- Use the JWT user login (register flow) instead of `LOCAL_AUTH_TOKEN`.
- Give agents only the scopes they need; rotate agent tokens via
  `POST /api/v1/agents/{id}/reset-token`.
- Store provider keys in the encrypted secrets store (Settings → API Keys),
  not in task briefs.
- Run `./backup.sh` regularly and keep backups off-host.
