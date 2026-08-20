# How to: connect Slack, Discord, Telegram and voice

All four integrations are optional and off by default. Mission Control runs
fine with none of them, with one of them, or with several at once — messages
**fan out** to every configured channel, they do not fail over.

| Feature | How to configure it |
|---|---|
| Slack team chat with the fleet | Settings → Slack (in-app) + a few `.env` settings |
| Discord notifications + per-agent channels | `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID` |
| Telegram approvals / reports / team chat | Settings → Telegram (in-app), `TELEGRAM_*` in `.env` as fallback |
| Voice assistant (LiveKit + realtime speech) | `COMPOSE_PROFILES=voice` plus `LIVEKIT_*`, `JARVIS_AGENT_TOKEN` |

## Slack — team chat with the fleet

Slack is the most capable of the chat channels: each agent posts under its own
name and emoji instead of one anonymous bot, files go both directions, and
every MC conversation gets its own Slack thread.

MC uses **Socket Mode**, so nothing needs a public URL — MC opens an outbound
WebSocket to Slack. There is no Request URL to fill in, and you need **two**
tokens: a bot token (`xoxb-…`, from OAuth & Permissions) and an app-level
token (`xapp-…`, from Basic Information → App-Level Tokens, scope
`connections:write`).

Roughly ten minutes of work:

1. Create an app at [api.slack.com/apps](https://api.slack.com/apps).
2. Enable Socket Mode, then create the app-level token.
3. Add exactly these bot token scopes: `chat:write`, `chat:write.customize`,
   `channels:read`, `channels:history`, `files:write`, `files:read`.
4. Subscribe to the bot event `message.channels`.
5. Install the app and copy the bot token.
6. Paste both tokens into **Settings → Slack** and press **Test connection**.
7. Create the channel and `/invite` the bot into it.
8. Set the channel in `.env`, then `docker compose up -d backend`.

```bash
SLACK_TEAM_CHAT_ENABLED=true
SLACK_DEFAULT_CHANNEL=#your-fleet-channel
CHAT_CHANNELS=telegram,slack     # empty = every channel whose own switch is on
```

Two optional companion channels: `SLACK_REPORTS_CHANNEL` collects final task
reports so completion artifacts stay out of the conversation, and
`SLACK_APPROVALS_CHANNEL` delivers approval requests with buttons. Invite the
bot into each of them too. Files you drop into a channel or thread become
reference files, capped by `SLACK_FILE_INGEST_MAX_BYTES` (default 25 MB).

Two mistakes account for most failures: forgetting `chat:write.customize` (all
agents then post under the same app name, and nothing else looks wrong), and
forgetting to invite the bot into the channel (tokens fine, test green, total
silence). Full walkthrough and troubleshooting table:
[docs/setup/slack.md](../../setup/slack.md).

## Discord — notifications and per-agent channels

Discord has no dedicated setup page and no Settings section yet; configure it
in `.env` and manage channels through the API or the agent UI.

```bash
DISCORD_BOT_TOKEN=          # bot token, required for anything channel-related
DISCORD_GUILD_ID=           # the server MC creates agent channels in
DISCORD_CATEGORY_ID=        # category the per-agent channels are grouped under
DISCORD_WEBHOOK_OPS=        # webhook for ops/error notifications only
```

The webhook path (`DISCORD_WEBHOOK_OPS`) is independent of the bot: it only
pushes ops and error alerts and needs no bot token.

With a bot token, each agent can get its **own text channel**. The endpoints
are `POST` / `PATCH` / `DELETE /api/v1/discord/agents/{agent_id}/channel`, and
`GET /api/v1/discord/channels` lists the guild's text channels. Guild and
category are also editable at runtime through
`GET` / `PATCH /api/v1/discord/config`, which is the authoritative source once
set — the `.env` values are the bootstrap.

Agents with the `chat:write` scope can post into a channel themselves via
`POST /api/v1/agent/discord/send`.

## Telegram — approvals and reports on your phone

Telegram's strength is speed to your phone: approval requests arrive as
messages with **Entblocken/Abbrechen** buttons, and finished tasks deliver
their reports. Optionally the whole team chat is mirrored into a Telegram
group, one topic per conversation, with voice messages transcribed.

MC never asks Telegram to call your server. Outbound is plain HTTPS to
`api.telegram.org`; inbound (only needed for the team chat) is long polling.
No webhook, no tunnel.

**Two bots, on purpose** — one for conversation, one for reports, so
high-volume completion artifacts land in their own chat with their own
notification settings:

| Bot | Env pair | Carries |
|---|---|---|
| Command bot | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Approval buttons, team-chat mirror, voice input |
| Reports bot | `TELEGRAM_REPORTS_BOT_TOKEN` + `TELEGRAM_REPORTS_CHAT_ID` | Final task reports, text and files |

Create the bots with [@BotFather](https://t.me/BotFather), find your chat ID by
messaging the bot and calling `getUpdates`, then enter everything in
**Settings → Telegram** and press **Test connection**. Values saved there apply
without a restart and win over `.env`; per-function toggles let you switch
reports, approvals, team chat or Jarvis off without deleting a token.

Two things to watch:

- The approval buttons are plain links to MC's own confirmation page, so
  **`MC_BASE_URL` must be reachable from your phone** — on a Tailscale network,
  set it to the machine's Tailscale address.
- The health watchdog `scripts/poll-health-check.sh` reads its Telegram
  credentials from `.env` only, deliberately: it has to be able to alarm you
  while MC itself is down.

Turn on the team-chat mirror with `TELEGRAM_TEAM_CHAT_ENABLED=true` and
`CHAT_CHANNELS`. Full walkthrough:
[docs/setup/telegram.md](../../setup/telegram.md).

## Voice — talk to the fleet

The voice assistant runs behind a compose profile, so it is not part of the
lean default boot. Enable it in `.env`:

```bash
COMPOSE_PROFILES=voice          # add ,browser for the Playwright sidecars
```

That brings up two services: a LiveKit server and the voice worker that joins
the LiveKit room as the Jarvis agent and calls back into the MC API with an
agent token.

```bash
LIVEKIT_KEYS=                   # YAML mapping "<api_key>: <api_secret>" — the space matters
LIVEKIT_API_KEY=                # same pair, split, for backend token signing
LIVEKIT_API_SECRET=
LIVEKIT_PUBLIC_URL=             # WS URL the browser connects to; empty = derived from origin
LIVEKIT_NODE_IP=                # RTC candidate IP LiveKit advertises; 127.0.0.1 = local only
VOICE_PROVIDER=openai           # FALLBACK ONLY — see below
VOICE_MODEL=gpt-realtime-2.1    # FALLBACK ONLY — see below
VOICE_OPENAI_VOICE_ID=          # empty = "marin"
VOICE_XAI_VOICE_ID=             # empty = "ara"
OPENAI_API_KEY=                 # for the openai arm
XAI_API_KEY=                    # for the xai arm
JARVIS_AGENT_TOKEN=             # create the Jarvis agent first, then paste its token
```

**Picking the provider (ADR-074).** `VOICE_PROVIDER` and `VOICE_MODEL` are only
the emergency default. The provider Jarvis actually speaks to is a *runtime
binding*: open the Jarvis agent, use the runtime selector, pick "Jarvis Voice —
OpenAI Realtime" or "Jarvis Voice — Grok (xAI)". The voice service reads that
binding at the start of every call, so the change takes effect on the **next
call** — no restart, and a call in progress is not cut off. The env values apply
only when the backend does not answer or nothing is bound yet.

The two arms keep separate voice variables because their voice names do not
overlap: "cedar" exists at OpenAI, "ara" at xAI, and a shared value breaks
whichever arm does not know the name.

API keys stay in the container's environment. MC never stores or forwards them —
the config endpoint returns the provider and model only.

The order matters for `JARVIS_AGENT_TOKEN`: create the agent in MC, then paste
its token here. If you want voice reachable from a phone rather than only the
local machine, `LIVEKIT_NODE_IP` must be your LAN or VPN address — the default
`127.0.0.1` advertises a candidate nobody else can reach.

A separate, lighter mode exists for Telegram: `JARVIS_TELEGRAM_ENABLED=true`
answers text and voice messages in the command chat with the Jarvis brain
instead of mirroring them into the team chat. See the `JARVIS_*` block in
`.env.example`.

## Where to go next

- [Get your first agent running](first-agent.md) — you need agents before a
  chat channel has anything to say.
- [Connect GitHub](github-workflow.md) — so approvals actually gate a merge.
