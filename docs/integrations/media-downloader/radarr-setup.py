#!/usr/bin/env python3
"""Configure Radarr for German, CAM-free, Apple-TV/Plex-friendly movie downloads.

Idempotent + additive. Creates two custom formats (CAM/Telesync/Screener block,
German DL preference), optimizes the German quality profiles (drops the
Remux/BR-DISK/HDTV monsters, keeps WEB + Bluray for HD and 4K), and caps global
file sizes so nothing exceeds ~20 GB (Apple TV 4K Plex direct-plays without
transcoding). Non-German profiles get only the CAM block.

Values are sourced from the TRaSH-Guides and Radarr's own QualityParser.cs.

Usage:
    export RADARR_URL="http://<nas-ip>:7878"
    export RADARR_API_KEY="<Settings → General → API Key>"
    # optional overrides:
    export GERMAN_PROFILE_IDS="6,7"     # profiles that should prefer German DL
    export CAM_ONLY_PROFILE_IDS="9"     # e.g. a Hungarian profile — CAM block only
    python3 radarr-setup.py

The API key is never stored in the repo. In Mission Control the Downloader agent
derives it at runtime from the vault-stored web login (see SKILL.md §0).
"""
import json, os, sys, urllib.request, urllib.error

RADARR_URL = os.environ.get("RADARR_URL", "").rstrip("/")
API_KEY = os.environ.get("RADARR_API_KEY", "")
if not RADARR_URL or not API_KEY:
    sys.exit("Set RADARR_URL and RADARR_API_KEY environment variables first.")
BASE = f"{RADARR_URL}/api/v3"
GERMAN_PROFILES = {int(x) for x in os.environ.get("GERMAN_PROFILE_IDS", "6,7").split(",") if x.strip()}
CAM_ONLY_PROFILES = {int(x) for x in os.environ.get("CAM_ONLY_PROFILE_IDS", "9").split(",") if x.strip()}


def api(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
        headers={"X-Api-Key": API_KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            raw = r.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        print(f"HTTP {e.code} on {method} {path}: {e.read().decode()[:400]}")
        raise


# ── 1. Custom formats (fields as ARRAY — Radarr API requirement) ─────────────
CAM = {
    "name": "CAM / Telesync / Screener (Unwanted)",
    "includeCustomFormatWhenRenaming": False,
    "specifications": [
        {"name": "CAM", "implementation": "ReleaseTitleSpecification", "negate": False,
         "required": False, "fields": [{"name": "value", "value": r"\b(?:CAMRIP|(?:NEW)?CAM|HD-?CAM(?:Rip)?|HQCAM)\b"}]},
        {"name": "Telesync", "implementation": "ReleaseTitleSpecification", "negate": False,
         "required": False, "fields": [{"name": "value", "value": r"\b(?:TS[-_. ]|TELESYNCH?|HD-?TS|PDVD|(?:HD)?TSRip)"}]},
        {"name": "Telecine", "implementation": "ReleaseTitleSpecification", "negate": False,
         "required": False, "fields": [{"name": "value", "value": r"\b(?:TELECINE|HD-?TC)\b"}]},
        {"name": "Screener", "implementation": "ReleaseTitleSpecification", "negate": False,
         "required": False, "fields": [{"name": "value", "value": r"\b(?:SCR|(?:DVD)?SCREENER|DVDSCR)\b"}]},
        {"name": "Workprint", "implementation": "ReleaseTitleSpecification", "negate": False,
         "required": False, "fields": [{"name": "value", "value": r"\b(?:WORKPRINT)\b"}]},
    ],
}
GERMAN_DL = {
    "name": "German DL",
    "includeCustomFormatWhenRenaming": True,
    "specifications": [
        {"name": "German", "implementation": "LanguageSpecification", "negate": False,
         "required": True, "fields": [{"name": "value", "value": 4}]},
        {"name": "Original Language", "implementation": "LanguageSpecification", "negate": False,
         "required": True, "fields": [{"name": "value", "value": -2}]},
    ],
}

existing = {c["name"]: c for c in api("GET", "/customformat")}
for cf in (CAM, GERMAN_DL):
    if cf["name"] in existing:
        print(f"CF exists: {cf['name']} (id {existing[cf['name']]['id']})")
    else:
        print(f"CF created: {cf['name']} (id {api('POST', '/customformat', cf)['id']})")
all_cfs = api("GET", "/customformat")

# ── 2. Profile optimization ──────────────────────────────────────────────────
DISABLE_NAMES = {"Remux-1080p", "Remux-2160p", "BR-DISK", "Raw-HD",
                 "HDTV-1080p", "HDTV-2160p", "HDTV-720p"}


def score_map(pid):
    if pid in GERMAN_PROFILES:
        return {"German DL": 11000, "CAM / Telesync / Screener (Unwanted)": -10000}
    return {"CAM / Telesync / Screener (Unwanted)": -10000}


for pid in sorted(GERMAN_PROFILES | CAM_ONLY_PROFILES):
    try:
        p = api("GET", f"/qualityprofile/{pid}")
    except urllib.error.HTTPError:
        print(f"Profile {pid} not found — skipping")
        continue
    enabled_ids = []
    for item in p["items"]:
        if item.get("items"):
            for sub in item["items"]:
                if sub["quality"]["name"] in DISABLE_NAMES:
                    sub["allowed"] = False
            item["allowed"] = any(s.get("allowed") for s in item["items"])
            if item["allowed"]:
                enabled_ids.append(item["id"])
        else:
            if (item.get("quality") or {}).get("name") in DISABLE_NAMES:
                item["allowed"] = False
            if item.get("allowed"):
                enabled_ids.append(item["quality"]["id"])
    if p["cutoff"] not in enabled_ids:
        p["cutoff"] = 7 if 7 in enabled_ids else enabled_ids[-1]
    sc = score_map(pid)
    p["formatItems"] = [{"format": c["id"], "name": c["name"], "score": sc.get(c["name"], 0)}
                        for c in all_cfs]
    p["minFormatScore"] = 0
    api("PUT", f"/qualityprofile/{pid}", p)
    print(f"Profile {pid} '{p['name']}': monsters disabled, CAM/German scored, cutoff={p['cutoff']}")

# ── 3. Global size caps (MB/min) — no 20 GB+ grabs ───────────────────────────
CAPS = {  # name: (preferred, max)  →  2h film ≈ max*120/1024 GB
    "WEBDL-1080p": (50, 90), "WEBRip-1080p": (50, 90), "Bluray-1080p": (85, 130),
    "WEBDL-2160p": (120, 170), "WEBRip-2160p": (120, 170), "Bluray-2160p": (150, 170),
}
defs = api("GET", "/qualitydefinition")
for d in defs:
    if d["quality"]["name"] in CAPS:
        d["preferredSize"], d["maxSize"] = CAPS[d["quality"]["name"]]
api("PUT", "/qualitydefinition/update", defs)
print(f"Size caps applied: {sorted(CAPS)} (1080p ≤ ~15 GB, 4K ≤ ~20 GB per 2h film)")
print("\nDONE.")
