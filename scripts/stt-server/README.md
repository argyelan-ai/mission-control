# Local STT server — voice messages transcribed on your own machine

MC transcribes operator voice messages (Slack, Telegram) through one shared
chain. By default that chain uses the OpenAI cloud; with this server it runs
**locally** — your voice never leaves the machine, and no OpenAI key is needed.

- **Model**: [Parakeet TDT 0.6B v3](https://huggingface.co/mlx-community/parakeet-tdt-0.6b-v3)
  (NVIDIA) — 25 European languages incl. German, matches or beats
  Whisper large-v3 on them, ~2 GB RAM, far faster than realtime on Apple
  Silicon via MLX.
- **Protocol**: OpenAI-compatible `POST /v1/audio/transcriptions` on port
  `8585`, bound to `127.0.0.1` (not exposed on LAN/Tailscale). The backend
  container reaches it as `host.docker.internal`.
- **Why on the host, not in Docker**: MLX needs Metal, and the Docker VM's
  RAM budget is deliberately small. Runs via launchd like the other host
  services.

## Requirements

- Apple Silicon Mac, Python 3.12
- `brew install ffmpeg` (converts m4a/ogg/mp4 → wav for the model)

## Setup

```bash
./setup.sh
```

Idempotent. Copies itself to `~/.mc/stt-server`, creates the venv there,
installs deps, registers + starts the launchd service `com.mc.stt`, waits for
the model (first run downloads ~2 GB), then runs a **self-test**: synthesises a
German sentence with `say` and checks the server transcribes it.

The service deliberately runs from `~/.mc/stt-server`, never from the git
checkout. A launchd job pointed at a working tree dies silently the moment that
path changes — a pruned worktree once left this service retrying 7527 times
while local dictation was simply gone, with nothing reporting it. Before
deleting any worktree: `grep -rl mc-worktrees ~/Library/LaunchAgents/`.

Then point MC at it — in the MC `.env`:

```
STT_BASE_URL=http://host.docker.internal:8585/v1
```

and restart the backend (`docker compose -p mission-control up -d backend`).
Optionally `STT_MODEL=<name>` — the server ignores it (it serves one model),
but some other OpenAI-compatible servers require it.

To go back to the cloud: remove `STT_BASE_URL`, restart the backend.

## Operations

```bash
curl http://127.0.0.1:8585/health          # {"ok": true, ...}
tail -f ~/.mc/logs/stt-server.log          # logs
launchctl unload ~/Library/LaunchAgents/com.mc.stt.plist   # stop
launchctl load ~/Library/LaunchAgents/com.mc.stt.plist     # start
```

Swapping the model (e.g. to `mlx-community/whisper-large-v3-turbo` if
Parakeet's German disappoints): change `MODEL_ID` in `server.py`, re-run
`setup.sh`. Nothing on the MC side changes.
