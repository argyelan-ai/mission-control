# Platforms

Mission Control is a Docker Compose stack of Linux containers. Anything that
runs Docker Compose v2 runs MC — but "runs" and "is tested" are different
things, so here is the honest split.

| Platform | Status |
|---|---|
| **Linux (x86_64)** | CI-tested on every push — the fresh-boot E2E job runs `./install.sh` on `ubuntu-latest`, migrates an empty database, registers an admin and makes an authenticated API call. |
| **macOS** | The maintainers' daily driver. Not in CI. The only platform for host-side (launchd) agents. |
| **Windows via WSL2** | Experimental — community-tested, not in CI. CI does check that `setup.ps1` still parses. |
| **NAS / self-hosting catalogs** | Manifests are prepared and were verified locally end-to-end; store availability varies (see below). |
| **Windows Server / hypervisors** | Run a Linux VM. Not in CI. |

Prerequisites everywhere: Docker with Compose v2, `git`, `openssl`, optionally
`python3` for nicer secret generation. `install.sh` refuses to continue on any
OS other than Linux or macOS.

## Linux headless server (the default)

Nothing special — this is the tested path:

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

The installer is non-interactive when there is no TTY, so it works from a
provisioning script. On a headless box you will want remote access: MC binds
to `127.0.0.1` by default, so set `MC_BIND_ADDRESS` and `PUBLIC_HOST` — see
[Reverse proxy & remote access](reverse-proxy.md).

Host-side agents are macOS-only (launchd). The Docker agent fleet works
everywhere Docker does.

## NAS and self-hosting catalogs

Catalog installs run the **core stack** — boards, tasks, vault, sessions,
agents bound to API runtimes. Host-level fleet extras (Docker socket access,
host launchd runtimes, the CLI-Bridge helper) need the manual install.

| Catalog | How to install | Status |
|---|---|---|
| **Runtipi** | Add the custom app store `https://github.com/argyelan-ai/tipi-store` under *Settings → App Stores*, then install Mission Control. | Published as a custom store — the official runtipi-appstore stopped accepting new apps. |
| **Portainer** | Add the raw URL of [`deploy/catalogs/portainer-template.json`](../../../deploy/catalogs/portainer-template.json) as an App Template source (*Settings → App Templates*). | Template shipped in-repo; no central store exists. |
| **CasaOS** | Manifest prepared: [`deploy/catalogs/casaos-app.yml`](../../../deploy/catalogs/casaos-app.yml). | Submission to the CasaOS AppStore prepared, not merged. |
| **Umbrel** | Package prepared: [`deploy/catalogs/umbrel/`](../../../deploy/catalogs/umbrel/). | Submission to `getumbrel/umbrel-apps` prepared, not merged. |

All catalog packages bundle their own small Caddy proxy — the browser must
enter through it, because the prebuilt frontend makes same-origin `/api/*`
calls. Catalogs can't run `setup.sh`, so secrets come from the store: CasaOS
asks at install time, Runtipi generates `type: random` form fields, Umbrel
derives them in `exports.sh`. The backend derives a valid Fernet key from any
passphrase, so a random string is acceptable for `SECRETS_ENCRYPTION_KEY`.

Details and image digest pins: [`deploy/catalogs/README.md`](../../../deploy/catalogs/README.md).

## macOS

The development platform. Standard install, plus two macOS-only capabilities:

- **Host agents** — native `claude` processes managed by launchd instead of
  containers (`agent_runtime: host`).
- **Scheduled backups via launchd** — `make backup-schedule` installs
  `~/Library/LaunchAgents/com.mc.backup.plist`, see
  [Backup & restore](backup-restore.md).

Docker Desktop's default resource limits are worth a look before you run a
fleet — see [hardware requirements](hardware-requirements.md).

## Windows 10/11 (WSL2)

Use WSL2. Full guide: [docs/setup/windows.md](../../setup/windows.md).

```bash
# inside your WSL2 Ubuntu distro
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

Docker Desktop exposes Docker inside WSL2 automatically (*Settings →
Resources → WSL integration*), and `http://localhost` in the Windows browser
reaches the stack. Known limitations: host-side agents are macOS-only,
cross-image runtime switching uses host-path mounts that are untested on
Windows paths, and `HOST_UID` permission mapping differs — if bind-mounted
volumes throw permission errors, stay inside WSL2. Check
[docs/setup/windows.md](../../setup/windows.md) for the current status of the
native PowerShell path (`setup.ps1`); WSL2 is the path the maintainers
support.

## Windows Server and company hypervisors

MC is a Linux-container stack, so on server infrastructure the clean way to
run it is a **small Linux VM next to your Windows VMs** — not inside Windows
Server itself:

1. On Hyper-V or VMware ESXi, create a Linux VM. Ubuntu Server 24.04 with
   2 vCPU / 8 GB RAM / 40 GB disk is a comfortable start.
2. Install Docker and git inside the VM:
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```
3. Run the standard one-liner. MC is then reachable at the VM's address — set
   `MC_BIND_ADDRESS=0.0.0.0` (see [Reverse proxy & remote
   access](reverse-proxy.md)) or put the VM on your tailnet.

This leaves Windows Server untouched and matches how Docker workloads are
normally run in company environments.

**If a Windows Server VM is all you have** (no rights to create a VM): WSL2
inside a Windows Server 2022+ VM works, but the VM needs **nested
virtualization** enabled first:

```powershell
# Hyper-V, on the host, VM powered off
Set-VMProcessor -VMName <vm> -ExposeVirtualizationExtensions $true
```

On ESXi it is *VM settings → CPU → Expose hardware assisted virtualization to
the guest OS*. Expect a performance overhead, and check your organisation's
policy — many admin teams prefer the separate Linux VM for exactly this
reason.
