# Media Downloader Integration (Radarr + NZBGet)

Delegate movie downloads to a Mission Control agent. Films land on the QNAP NAS
**in German**, **CAM-free**, and **sized for Apple TV / Plex direct play** (no
transcoding). Request a movie in plain language — via the Boss orchestrator or
directly on a board — and the **Downloader** agent finds it, adds it to Radarr,
and starts the Usenet download through NZBGet.

```
Operator ── "lade Film X auf Deutsch" ──▶ Boss ──(mc delegate --to Downloader)──▶ Downloader
                                                                                 │
                                                     Radarr (search + quality)   │  NZBGet (Usenet)
                                                     ┌───────────────────────────┴───────────────┐
                                                     │  TMDb lookup → add (German profile) →       │
                                                     │  grab German-DL release → download → NAS    │
                                                     └──────────────────────────────────────────────┘
                                                                                 ▼
                                                                    Apple TV / Plex (direct play)
```

## Components

| Piece | Where | What |
|-------|-------|------|
| **Skill** `mc-media-downloader` | [`SKILL.md`](./SKILL.md) → synced to `~/.mc/skills/` | The full Radarr v3 + NZBGet recipe the agent follows (lookup, add, search, monitor, report). |
| **Downloader agent** | Mission Control (cli-bridge, Sonnet) | Executes movie requests. Scopes include `credentials:read`; skill assigned via `cli_skills`. |
| **Radarr config** | QNAP NAS | Custom formats + optimized German profiles + size caps. Reproduce with [`radarr-setup.py`](./radarr-setup.py). |
| **Custom formats** | [`custom-formats.json`](./custom-formats.json) | Exported CF definitions (CAM block, German DL). |
| **Boss/Jarvis knowledge** | MC board memory (mc-dev, pinned) + global knowledge | Tells the orchestrator to route movie requests to Downloader. |

## The three guarantees

1. **German first.** Profile `4K / HD - DE` requires German audio; a `German DL`
   custom format (+11000) prefers dual-audio releases. No German release yet →
   the film stays monitored and Radarr keeps searching; the agent never
   substitutes a non-German or CAM release.
2. **No CAM.** Three layers: cinema-rip qualities (CAM/TS/TC/SCR/WP) are disabled
   in the profile, a `CAM / Telesync / Screener` custom format scores -10000, and
   `minimumAvailability: released` means Radarr doesn't even search during the
   cinema window.
3. **Apple-TV-friendly sizes.** Remux and BR-DISK (40–80 GB) are excluded; global
   caps keep 1080p ≤ ~15 GB and 4K ≤ ~20 GB. WEB-DL (HEVC + EAC3/DDP) is
   preferred over full-disc Bluray, whose TrueHD/DTS-HD would force Plex to
   transcode audio on the Apple TV.

Verified live with Radarr's interactive release search: German-DL releases are
accepted and scored highest, while French/English releases ("German is wanted,
but found …"), BR-DISK, and a 75 GB release ("larger than maximum allowed") are
rejected.

## How to request a movie

**Via Boss (chat / voice):**
> "Boss, lade *Dune: Part Two (2024)* auf Deutsch aufs NAS."

Boss delegates to Downloader:
```
mc delegate "Lade 'Dune: Part Two (2024)' auf Deutsch aufs NAS" --to Downloader \
  --description "Deutsch bevorzugt (German DL), HD oder 4K, Apple-TV/Plex-tauglich."
```

**Directly in MC:** create a task on the board and assign it to the Downloader agent.

The agent reports back what it added, the download progress, or — if no German
release exists yet — that the film stays monitored for automatic retry.

## Credentials

Both logins live in the MC **credentials vault** (`radarr`, `nzbget`), fetched by
the agent at runtime with its `credentials:read` scope. Nothing is stored in this
repo. The Radarr REST API key is derived once per task from the stored web login
(see [`SKILL.md`](./SKILL.md) §0). The NAS is reached over its LAN IP, which every
agent container can route to; if it later gets a Tailscale name only the vault
`url` changes.

## Reproducing the Radarr config

```bash
export RADARR_URL="http://<nas-ip>:7878"
export RADARR_API_KEY="<Radarr → Settings → General → API Key>"
python3 radarr-setup.py
```

Idempotent and additive: it creates the custom formats if missing, retunes the
German profiles (default ids `6,7`; a CAM-only profile such as a Hungarian one via
`CAM_ONLY_PROFILE_IDS`), and applies the size caps. Existing downloaded movies are
untouched (profiles have `upgradeAllowed: false`).

## Optional companions

See [`../media-downloader-plus/`](../media-downloader-plus/README.md) for opt-in
add-ons (Recyclarr auto-sync of TRaSH scores, Plex-Watchlist import, Bazarr German
subtitles, import notifications).
