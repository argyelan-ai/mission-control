# README screenshots

The pictures in `docs/assets/mc-*.png` and `mc-remote-phone.gif` come from
this folder and `frontend-v2/playwright/readme-shots.mjs`. Nothing in them
comes from a real install:

- **Pages** (home, insights, runtimes, settings, setup wizard) are the real
  frontend and backend of a throwaway demo stack, seeded with
  `scripts/demo-seed.py`.
- **Chat content** (the lead agent "Lead", the local agent "Forge", the
  `acme/webshop` repository, diffs, questions, the voice call) and the numbers
  on Insights and Runtimes are invented fixtures in
  `frontend-v2/playwright/readme-fixtures.mjs`, served to the real pages by
  Playwright. Hosts use TEST-NET addresses (`192.0.2.x`, RFC 5737).

## Regenerate

Needs Docker, Node with `npm ci` done in `frontend-v2/`, and `ffmpeg` (GIF).

```bash
scripts/readme-shots/demo-stack.sh build   # images mc-demo-backend:demo / mc-demo-frontend:demo
scripts/readme-shots/demo-stack.sh up      # project "mc-demo", UI on 127.0.0.1:13000
scripts/readme-shots/demo-stack.sh admin   # demo admin (operator@example.com)
scripts/readme-shots/demo-stack.sh seed    # demo board + crew

cd frontend-v2
npx playwright install chromium            # once
node playwright/readme-shots.mjs           # all shots → docs/assets/
node playwright/readme-shots.mjs mc-runtimes mc-insights   # or only some

cd .. && scripts/readme-shots/demo-stack.sh down   # containers, volume, images
```

Ports: `DEMO_WEB_PORT` (default 13000) and `DEMO_API_PORT` (default 18000).
Output folder: `SHOT_OUT` (relative to `frontend-v2/playwright/`).

## Why a separate compose file

`docker-compose.yml` pins the network name `mission-control_default`, mounts
`~/.mc` and gives the backend Docker access. A second stack started from it
would join a running install. `docker-compose.demo.yml` has its own project
name, network and volume, no Docker access (`DOCKER_HOST` points nowhere), no
worker, no agents, no voice or browser services, and the frontend shares the
backend's network namespace instead of needing Caddy.

## What each picture shows

| File | Shows | Size |
|---|---|---|
| `mc-chat-desktop.png` | Lead agent's chat: a fix with per-edit diff, Changes panel with Revert | 1440×900 @2x |
| `mc-chat-commit.png` | Commit from the Changes panel, message suggested by the local model; Open PR next to it | 1440×900 @2x |
| `mc-phone-question.png` | Local agent (omp) asks two questions before planning; Plan mode chip | 390×844 @2x |
| `mc-phone-plan.png` | Plan approval card: approve, refine, or stop the agent | 390×844 @2x |
| `mc-remote-phone.gif` | Monitor + intervene: answer the question card, the agent carries on | 390×844 @2x |
| `mc-runtimes.png` | Own GPU boxes: what each one serves, speed, GPU/RAM | 1440×900 @2x |
| `mc-insights.png` | Tokens per day, where usage comes from, local share per week | 1440×900 @2x |
| `mc-autonomy.png` | Settings → Autonomy: auto / notify / approve per action | 1440×900 @2x |
| `mc-home.png` | Home after the first start with the demo board (classic board view) | 1440×900 @2x |
| `mc-setup-wizard.png` | First-start wizard, last step (demo board is optional) | 1440×900 @2x |

Before committing new pictures: look at every one of them, and run
`python3 scripts/privacy-scan.py`.
