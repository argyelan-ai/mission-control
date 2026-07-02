# Mission Control on Windows

**Status: experimental.** The maintainers develop on macOS and CI-test on
Linux. Everything below *should* work because the whole stack runs in Docker
(Linux containers via the WSL2 backend) — but it is not part of CI. Reports
and PRs welcome.

## Prerequisites

- **Docker Desktop for Windows** with the **WSL2 backend** (default on
  current versions) — https://docs.docker.com/desktop/install/windows-install/
- **git** — https://git-scm.com/download/win

## Recommended: run inside WSL2 (the Linux path)

Everything from the README works 1:1 inside a WSL2 distro (Ubuntu), including
the one-liner:

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

Docker Desktop exposes Docker inside WSL2 automatically (Settings →
Resources → WSL integration). Open http://localhost in your Windows browser —
WSL2 forwards it.

## Alternative: native PowerShell

If you prefer staying in PowerShell:

```powershell
git clone https://github.com/argyelan-ai/mission-control.git
cd mission-control
.\setup.ps1                                          # generates .env with secure secrets
docker compose up -d          # pulls prebuilt images (or builds); migrations run automatically
start http://localhost
```

`setup.ps1` mirrors `setup.sh` (same secrets, PowerShell-native crypto RNG).

## Known limitations on Windows

- **Host-side agents** (Boss/Hermes-style launchd workers) are macOS-only.
  The Docker agent fleet is unaffected.
- **Cross-image runtime switching** shells out to `docker compose` with
  host-path mounts — untested on Windows paths. Use the WSL2 path if you
  need it.
- File-permission mapping (`HOST_UID`) differs from Linux hosts; `setup.ps1`
  pins the container default (1000). If bind-mounted volumes show permission
  errors, run inside WSL2 instead.
