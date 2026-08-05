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

## Native PowerShell (currently not supported)

Running `docker compose` directly from PowerShell against a Windows checkout
does **not** work today: the compose file expects POSIX paths and `${HOME}`,
which a native Windows environment doesn't provide, so the stack fails on
its bind mounts. Use WSL2 above — it is the supported path.

A one-click `setup.ps1` bootstrapper that sets up WSL2 + Docker and runs the
standard installer for you is on the roadmap (see the README).

## Known limitations on Windows

- **Host-side agents** (Boss/Hermes-style launchd workers) are macOS-only.
  The Docker agent fleet is unaffected.
- **Cross-image runtime switching** shells out to `docker compose` with
  host-path mounts — untested on Windows paths. Use the WSL2 path if you
  need it.
- File-permission mapping (`HOST_UID`) differs from Linux hosts; `setup.ps1`
  pins the container default (1000). If bind-mounted volumes show permission
  errors, run inside WSL2 instead.

## Windows Server / company hypervisors

Mission Control is a Linux-container stack, so on server infrastructure the
clean way to run it is a **small Linux VM next to your Windows VMs** — not
inside Windows Server itself:

1. On your hypervisor (Hyper-V or VMware ESXi), create a Linux VM —
   Ubuntu Server 24.04, 2 vCPU / 8 GB RAM / 40 GB disk is a comfortable
   start.
2. Inside the VM, install Docker (`curl -fsSL https://get.docker.com | sh`)
   and git.
3. Run the standard one-liner from the README. Done — MC is reachable at the
   VM's address (bind beyond localhost via `MC_BIND_ADDRESS`, see the
   README's security notes, or put it on your
   [Tailscale](https://tailscale.com) tailnet).

This keeps your Windows Server untouched and matches how companies typically
run Docker workloads.

**If the Windows Server VM is all you have** (no rights to create a VM): WSL2
inside a Windows Server 2022+ VM works, but on ESXi the VM needs **nested
virtualization** enabled first (VM settings → CPU → *Expose hardware assisted
virtualization to the guest OS*; on Hyper-V:
`Set-VMProcessor -ExposeVirtualizationExtensions $true`). Expect a
performance overhead and check your organisation's policy — many admin teams
prefer the separate Linux VM for exactly this reason.
