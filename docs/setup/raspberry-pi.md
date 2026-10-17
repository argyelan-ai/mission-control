# Raspberry Pi (and other small Linux boards)

Mission Control runs on a Raspberry Pi 4 or 5 — the published images are
built for `linux/arm64` as well as `linux/amd64`. This page covers what is
different from a normal Linux server. Everything else is the
[standard install](../handbook/operations/platforms.md#linux-headless-server-the-default).

## Hardware

| | Recommendation | Why |
|---|---|---|
| Board | Pi 4 or Pi 5, **8 GB RAM** | The core stack peaked at **758 MB** RAM on a fresh Pi 4 — every agent container comes on top of that. 4 GB works for the core stack alone, with little headroom. |
| OS | Raspberry Pi OS **64-bit** (Bookworm or newer) | The images are `arm64`; a 32-bit OS cannot run them. |
| Storage | **SSD over USB 3** (or NVMe on a Pi 5), not the SD card | The core stack adds about **3.2 GB** on disk. Building an agent image took **364 s on an SD card** — and Postgres on an SD card wears it out. |

## 1. Prerequisites

```bash
sudo apt update
sudo apt install -y git curl openssl
curl -fsSL https://get.docker.com | sudo sh          # Docker Engine + Compose v2
sudo usermod -aG docker $USER                        # use docker without sudo
```

**Log out and back in** (or reboot) after `usermod` — the group change only
applies to new logins. Check with `docker compose version` and
`docker info` (no "permission denied").

## 2. Turn on the memory cgroup (important)

Raspberry Pi OS boots with the kernel's memory controller **off**. Docker
then prints

```
WARNING: No memory limit support
```

in `docker info`, and every `mem_limit` in the compose file is silently
ignored — one runaway container can take the whole board down. The
installer warns when it sees this. Fix it once:

```bash
# /boot/firmware/cmdline.txt is ONE line — append to it, never add a new line
sudo sed -i '1 s/$/ cgroup_enable=memory cgroup_memory=1/' /boot/firmware/cmdline.txt
sudo reboot
```

(Older images without `/boot/firmware/` use `/boot/cmdline.txt`.) After the
reboot, `docker info 2>&1 | grep -i "memory limit"` prints nothing, and
`cat /sys/fs/cgroup/cgroup.controllers` lists `memory`.

## 3. Install

```bash
curl -fsSL https://raw.githubusercontent.com/argyelan-ai/mission-control/main/install.sh | bash
```

The installer clones the latest release and pulls its prebuilt `arm64`
images, so nothing big is built on the Pi. Only agent images you build
yourself (`scripts/build-agent-images.sh`) are compiled locally — that is
where an SSD pays off.

## Limits

- Host-side agents (launchd) are macOS-only; on a Pi agents run as Docker
  containers.
- The omp and kimi agent images pin `arm64` binaries and are built locally;
  they work on a Pi but are not published.
- Local LLMs are out of scope for a Pi — point agents at a runtime on another
  box (see [AI providers](ai-providers.md)).
