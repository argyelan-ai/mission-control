---
name: mc-media-downloader
description: Search, add and download movies (in German, Apple-TV/Plex-optimized) to the QNAP NAS via Radarr + NZBGet. Use whenever a task asks to find, request, download, or check the status of a movie — "lade Film X", "such mir Film Y auf Deutsch", "ist Film Z schon da", "download queue". Handles the full flow: TMDb lookup → add to Radarr with the German quality profile → trigger Usenet search → monitor NZBGet download → report status back.
---

# mc-media-downloader

Delegate movie downloads to Radarr (automation brain) + NZBGet (Usenet downloader),
running on the QNAP NAS. Everything lands on the NAS, ready for Apple TV / Plex.

**Golden rules**
1. **German first.** Every movie must be in German. The Radarr profile `4K / HD - DE`
   (id 7) enforces German audio and prefers `German DL` (dual-audio) releases.
2. **No CAM.** A `CAM / Telesync / Screener` custom format (score -10000) plus disabled
   CAM/TS qualities plus `minimumAvailability: released` block cinema rips three ways.
   Never override this.
3. **Apple-TV-friendly sizes.** Profiles exclude Remux/BR-DISK; global size caps keep
   1080p ≤ ~15 GB and 4K ≤ ~20 GB so the Apple TV Plex app direct-plays without transcoding.
4. **Never print or commit credentials.** Fetch them from the vault at runtime (below).
5. **Confirm, don't guess.** If a movie title is ambiguous (remakes, franchises), pick by
   year/TMDb id and say which one you added.

---

## 0. Get credentials from the MC vault (once per task)

Both logins live in the MC credentials vault. Fetch them with your agent token:

```bash
BOARD_ID="<your board_id — in your dispatch context>"
# List to find the ids
curl -s "$MC_API_URL/api/v1/agent/boards/$BOARD_ID/credentials" \
  -H "Authorization: Bearer $MC_AGENT_TOKEN" | python3 -c "import sys,json;[print(c['name'],c['id'],c['url']) for c in json.load(sys.stdin)]"

# Fetch the 'radarr' credential (fully decrypted): {username, password} + url
RADARR_JSON=$(curl -s "$MC_API_URL/api/v1/agent/boards/$BOARD_ID/credentials/<radarr_credential_id>" -H "Authorization: Bearer $MC_AGENT_TOKEN")
RADARR_URL=$(echo "$RADARR_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['url'])")   # http://<nas-ip>:7878

# Fetch the 'nzbget' credential
NZBGET_JSON=$(curl -s "$MC_API_URL/api/v1/agent/boards/$BOARD_ID/credentials/<nzbget_credential_id>" -H "Authorization: Bearer $MC_AGENT_TOKEN")
```

### Radarr API key (derive once from the web login)

Radarr's REST API needs an `X-Api-Key`, not the web login. Derive it once from the
stored admin login, then reuse it for every call:

```bash
RUSER=$(echo "$RADARR_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['username'])")
RPW=$(echo "$RADARR_JSON"   | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['password'])")
JAR=$(mktemp)
curl -s -c "$JAR" "$RADARR_URL/login" -o /dev/null
curl -s -b "$JAR" -c "$JAR" -X POST "$RADARR_URL/login?returnUrl=%2F" \
  --data-urlencode "username=$RUSER" --data-urlencode "password=$RPW" --data-urlencode "rememberMe=" -o /dev/null
APIKEY=$(curl -s -b "$JAR" "$RADARR_URL/initialize.json" | python3 -c "import sys,json;print(json.load(sys.stdin)['apiKey'])")
R="$RADARR_URL/api/v3"
AH=(-H "X-Api-Key: $APIKEY" -H "Content-Type: application/json")
# Sanity check:
curl -s "${AH[@]}" "$R/system/status" | python3 -c "import sys,json;d=json.load(sys.stdin);print('Radarr',d['version'],'OK')"
```

> **Network note:** The NAS is reached by its LAN IP (`<nas-ip>`), which works from
> every MC agent container (same LAN as the Mac Mini). If the NAS later gets a Tailscale
> name, only the `url` in the vault credential changes — this skill needs no edit.

---

## 1. Find the movie (TMDb lookup)

```bash
# By title (pick the right result by year / tmdbId):
curl -s "${AH[@]}" "$R/movie/lookup?term=$(python3 -c 'import urllib.parse,sys;print(urllib.parse.quote(sys.argv[1]))' 'Dune Part Two')" \
  | python3 -c "import sys,json;[print(m.get('year'),'| tmdb',m['tmdbId'],'|',m['title']) for m in json.load(sys.stdin)[:8]]"

# By TMDb id directly:
curl -s "${AH[@]}" "$R/movie/lookup/tmdb?tmdbId=693134"
```

Take the **full lookup object** for the chosen movie — you POST it back with a few fields added.

---

## 2. Add the movie + start the search

Defaults: German profile `id 7`, root folder from `GET /rootfolder`, `released` only.

```bash
QPROFILE=7                      # '4K / HD - DE' — German + CAM-blocked + Apple-TV sizes
ROOT=$(curl -s "${AH[@]}" "$R/rootfolder" | python3 -c "import sys,json;print(json.load(sys.stdin)[0]['path'])")
TMDB=693134

# Build the payload from the lookup object, then POST:
curl -s "${AH[@]}" "$R/movie/lookup/tmdb?tmdbId=$TMDB" | python3 -c "
import sys,json
m=json.load(sys.stdin)
m.update({
  'qualityProfileId': $QPROFILE,
  'rootFolderPath': '$ROOT',
  'monitored': True,
  'minimumAvailability': 'released',
  'addOptions': {'searchForMovie': True, 'monitor': 'movieOnly'},
})
print(json.dumps(m))
" > /tmp/movie.json
curl -s -X POST "${AH[@]}" "$R/movie" -d @/tmp/movie.json \
  | python3 -c "import sys,json;d=json.load(sys.stdin);print('Added:',d['title'],'(id',d['id'],') — search triggered')"
```

If the movie already exists you get HTTP 400 `MovieExistsValidator` — then just trigger a
fresh search on the existing movie id (below).

### Quality profile choices
| id | name | when |
|----|------|------|
| 7  | **4K / HD - DE** | **default** — German, HD+4K, CAM-blocked, Apple-TV sizes |
| 6  | HD - 720p/1080p | German, HD only (no 4K), smaller |
| 9  | Magyar Filmek | Hungarian audio — only if explicitly requested |

Only use another profile if the task explicitly asks for it.

---

## 3. Trigger / re-trigger a search

`addOptions.searchForMovie` already searches on add. To (re)search an existing movie:

```bash
MOVIE_ID=42
curl -s -X POST "${AH[@]}" "$R/command" -d "{\"name\":\"MoviesSearch\",\"movieIds\":[$MOVIE_ID]}" \
  | python3 -c "import sys,json;print('search command',json.load(sys.stdin)['id'],'queued')"
```

---

## 4. Check status — Radarr queue + NZBGet download

```bash
# Radarr queue (what's grabbed / importing):
curl -s "${AH[@]}" "$R/queue?includeMovie=true&pageSize=50" | python3 -c "
import sys,json
for r in json.load(sys.stdin).get('records',[]):
    print(r.get('title'),'|',r.get('status'),'|',round(r.get('sizeleft',0)/1e9,2),'GB left |',r.get('trackedDownloadState'))
"
```

```bash
# NZBGet download progress (JSON-RPC, basic auth):
NUSER=$(echo "$NZBGET_JSON" | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['username'])")
NPW=$(echo "$NZBGET_JSON"   | python3 -c "import sys,json;print(json.load(sys.stdin)['data']['password'])")
NURL=$(echo "$NZBGET_JSON"  | python3 -c "import sys,json;print(json.load(sys.stdin)['url'])")   # http://<nas-ip>:6789

curl -s -u "$NUSER:$NPW" "$NURL/jsonrpc" -d '{"method":"listgroups"}' | python3 -c "
import sys,json
for g in json.load(sys.stdin)['result']:
    done=g['FileSizeMB']-g['RemainingSizeMB']
    pct=100*done/g['FileSizeMB'] if g['FileSizeMB'] else 0
    print(g['NZBName'],'|',g['Status'],'| %.0f%%'%pct,'|',g['RemainingSizeMB'],'MB left')
"
# Overall rate / paused state:
curl -s -u "$NUSER:$NPW" "$NURL/jsonrpc" -d '{"method":"status"}' | python3 -c "import sys,json;d=json.load(sys.stdin)['result'];print('rate',round(d['DownloadRate']/1e6,1),'MB/s | standby',d['ServerStandBy'],'| paused',d['DownloadPaused'])"
```

Empty queue + movie shows `hasFile: true` (`GET $R/movie/$MOVIE_ID`) → done, on the NAS,
Plex will pick it up.

---

## 5. Useful lookups

```bash
curl -s "${AH[@]}" "$R/qualityprofile"  # list profiles
curl -s "${AH[@]}" "$R/rootfolder"      # list root folders + free space
curl -s "${AH[@]}" "$R/movie" | python3 -c "import sys,json;print(len(json.load(sys.stdin)),'movies in library')"
# Is a specific movie already in the library?
curl -s "${AH[@]}" "$R/movie?tmdbId=693134" | python3 -c "import sys,json;d=json.load(sys.stdin);print('in library, hasFile:',d[0]['hasFile']) if d else print('not in library')"
```

---

## 6. Report back (task comment)

When you finish, report concretely:
- **What** you added (title + year + TMDb id + profile).
- **State**: searching / downloading (X %) / imported to NAS / no German release found.
- If **no German release** exists yet: say so plainly — the movie stays monitored, Radarr
  keeps searching automatically. Don't grab a non-German or CAM release to "make it work".

Example: *"Added **Dune Part Two (2024, tmdb 693134)** on the German profile. Grabbed a
German DL WEB-1080p (8.4 GB), downloading at 12 MB/s (~60 %). Will import to
`/data/usenet/movies` automatically — Plex sees it when done."*

---

## Troubleshooting
- **Radarr 401** → API key derivation failed; re-run the login flow (section 0).
- **`MovieExistsValidator` (400)** → already added; re-search the existing id (section 3).
- **NZBGet 401** → wrong creds; the working user is in the vault `nzbget` credential.
- **Grabbed release stuck / stalled** → check `GET $R/queue` `trackedDownloadStatus`; a
  warning there usually means the release failed and Radarr will grab the next candidate.
- **Wrong language slipped through** → verify the movie file: `GET $R/moviefile?movieId=<id>`,
  field `languages`. Report it; do not silently keep a non-German file.
