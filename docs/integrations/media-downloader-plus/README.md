# Media Downloader — Optional Companions

Opt-in add-ons for the [media-downloader integration](../media-downloader/README.md).
Everything here is **off by default** — run a script or install a container only
when you want that piece. Nothing touches your setup unless you ask it to.

Priority order (most bang for the buck first):

## 1. Import notifications — know when a film is ready ⭐

Radarr pings you (Telegram or Discord) the moment a movie finishes importing to
the NAS, so you're not checking the queue.

```bash
export RADARR_URL="http://<nas-ip>:7878"
export RADARR_API_KEY="<api key>"
# Telegram:
export TELEGRAM_BOT_TOKEN="..."; export TELEGRAM_CHAT_ID="..."
python3 radarr-notify.py telegram
# …or Discord:
export DISCORD_WEBHOOK_URL="https://discord.com/api/webhooks/..."
python3 radarr-notify.py discord
```

Idempotent — re-running updates the existing connection. See
[`radarr-notify.py`](./radarr-notify.py).

## 2. Recyclarr — keep the TRaSH tuning current ⭐

The one-shot `radarr-setup.py` bootstraps the German + Apple-TV custom formats;
[Recyclarr](https://recyclarr.dev) keeps them synced with upstream TRaSH-Guides
on a schedule so scores never drift. Ready-to-use config:
[`recyclarr.yml`](./recyclarr.yml).

```bash
docker run --rm -it -v /share/Container/recyclarr:/config \
  -e RECYCLARR_RADARR_API_KEY="<api key>" \
  ghcr.io/recyclarr/recyclarr sync --preview   # drop --preview to apply
```

## 3. Plex Watchlist import — add films from the couch

Radarr can watch your Plex Watchlist: star a film in the Plex app on the Apple
TV, and Radarr picks it up on the German profile automatically.

Radarr → **Settings → Import Lists → + → Plex Watchlist** → sign in →
Quality Profile `4K / HD - DE`, Root Folder your movies path,
Minimum Availability `Released`, Monitor `on`. No extra service needed.

## 4. Bazarr — German subtitles for OV-only cases

When only an original-language release exists, [Bazarr](https://www.bazarr.media/)
fetches German subtitles automatically. Point it at the same Radarr + root
folder, set languages to German (+ optionally forced subs for foreign parts).
Runs as a QNAP/Docker container next to Radarr.

## 5. Overseerr / Jellyseerr — a request UI for the family

A polished request front-end (web + mobile) that hands requests to Radarr with
the right profile — nice if others in the household should be able to ask for
films without touching Radarr. https://overseerr.dev

## 6. Maintainerr — reclaim NAS space

Auto-cleanup rules (watched + older than N days → remove from Plex/Radarr/disk),
so the NAS doesn't fill up with films nobody rewatches. https://maintainerr.info
Configure conservatively and review its preview before enabling deletion.

---

**Not included on purpose:** none of these are auto-configured. Import
notifications and Recyclarr are ready-to-run scripts/configs; the rest are
third-party services you install on the NAS when you want them. Keeps the core
integration lean and your channels quiet until you opt in.
