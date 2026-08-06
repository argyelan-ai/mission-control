# Connecting Telegram

Telegram gives Mission Control a direct line to your phone: approval requests
arrive as messages with **Entblocken/Abbrechen buttons**, finished tasks
deliver their reports (text and files), and — optionally — the whole team
chat is mirrored into a Telegram group, one topic per conversation. With that
mirror on, voice messages work too: record one, MC transcribes it and treats
it as typed text.

This is entirely optional. MC runs fine without Telegram, and it runs fine
with **both** Telegram and Slack at once — messages fan out to every
configured channel, they do not fail over. If you only want one channel,
[Slack](slack.md) is the more capable of the two (per-agent identities,
in-app token settings); Telegram's strength is that it is the fastest way to
get approval buttons onto your phone.

Everything below happens once and takes about ten minutes.

## How MC talks to Telegram (read this first)

MC never asks Telegram to call your server. Outbound messages are plain HTTPS
calls to `api.telegram.org`, and inbound messages — only needed for the team
chat — are fetched by MC itself via long polling (`getUpdates`). Nothing is
exposed to the internet, no webhook, no reverse proxy, no tunnel. If you only
want approval buttons and reports, MC does not even start the poller.

**Two bots, on purpose.** MC uses one bot for the *conversation* (approvals,
team chat) and a second bot for *reports* (task deliverables). They could
technically be one bot, but reports are high-volume completion artifacts —
with a separate bot they arrive as a separate chat on your phone, with its
own notification settings, instead of burying the conversation. Each bot is
optional: configure only the pair you want.

| Bot | Env pair | What it carries |
|---|---|---|
| Command bot | `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` | Approval buttons, team-chat mirror, voice input |
| Reports bot | `TELEGRAM_REPORTS_BOT_TOKEN` + `TELEGRAM_REPORTS_CHAT_ID` | Final task reports (`mc report`, text and files) |

## 1. Create the bot(s)

Open a chat with [@BotFather](https://t.me/BotFather) in Telegram and send
`/newbot`. Answer the two questions (display name, username ending in `bot`)
and BotFather replies with the bot token — a string like
`1234567890:AA…`. Copy it.

Repeat `/newbot` for the reports bot if you want reports on Telegram.

## 2. Find your chat ID

A bot cannot message you until you have messaged it once. Open your new bot,
press **Start**, send it any message. Then ask the Bot API who wrote:

```
curl -s "https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates"
```

In the JSON answer, `"chat":{"id":123456789,…}` is your chat ID. For a
private 1:1 chat it is a positive number.

For a **group** (needed for the team-chat mirror, next section): add the bot
to the group, write a message in the group, call `getUpdates` again — the
group's chat ID is negative (usually starting `-100`).

If `getUpdates` returns an empty `result`, send another message and retry;
updates are only kept for a short time.

## 3. Decide the chat shape

**Buttons and reports only** — a private 1:1 chat with each bot is all you
need. Use its positive chat ID and skip ahead.

**Full team chat** — the mirror puts every MC conversation into its own
**topic**, so it needs a group with topics:

1. Create a group, open its settings, and enable **Topics** (Telegram calls
   this a *forum*; only supergroups can do it).
2. Add the command bot and make it an **admin** with the **Manage Topics**
   permission — it creates and renames a topic per MC conversation.
3. Use the group's (negative) chat ID as `TELEGRAM_CHAT_ID`.

If the chat turns out not to be a forum, MC degrades gracefully: everything
lands in the plain chat instead of topics, and the log says so. It does not
crash, it just gets noisier.

## 4. Enter the values in Settings → Telegram

**Settings → Telegram** in the MC UI is the home of this configuration: paste
both bot tokens (stored encrypted in the `secrets` table, like Slack's),
enter the chat IDs, and hit **Test connection** — it runs `getMe` against
both bots and tells you exactly which one is unhappy. Changes apply to the
running backend immediately, no restart. The same page has per-function
toggles (reports, approvals, team chat, Jarvis), so you can switch a
configured function off without deleting any token.

`.env` still works as the fallback for headless installs:

```
TELEGRAM_BOT_TOKEN=1234567890:AA…
TELEGRAM_CHAT_ID=-1001234567890
TELEGRAM_REPORTS_BOT_TOKEN=9876543210:BB…
TELEGRAM_REPORTS_CHAT_ID=123456789
```

Then `docker compose up -d backend`. Values saved in the settings page win
over `.env`. Approval buttons and reports are live now — they need nothing
further. (One deliberate exception: the health watchdog
`scripts/poll-health-check.sh` reads its Telegram credentials from `.env`
only — it must be able to alarm you while MC itself is down.)

## 5. Turn on the team-chat mirror (optional)

Two more `.env` settings control the conversation mirror:

```
TELEGRAM_TEAM_CHAT_ENABLED=true
CHAT_CHANNELS=telegram,slack
```

| Setting | What it does |
|---|---|
| `TELEGRAM_TEAM_CHAT_ENABLED` | The channel's own on/off switch. Off by default: without it, Telegram does approvals and reports but stays out of the conversation. |
| `CHAT_CHANNELS` | Which chat channels may run at all. `telegram,slack` runs both side by side; `slack` silences Telegram without removing anything. Empty = every channel whose own switch is on. |

With the mirror on, MC also starts its inbound poller: what you write in the
group flows back into the matching MC conversation. Write inside a topic and
the message lands in exactly that conversation; write in the general chat and
it goes to Boss, the fleet's contact person. Telegram shows every message
under the same bot name — agents are distinguished by a `Rex:` prefix rather
than by avatar (a Telegram bot has exactly one identity; this is the one
thing Slack simply does better).

## About the approval buttons

The buttons under an approval are plain links to MC's own confirmation page —
no Telegram-side interactivity, no callback server. That has one practical
consequence: **`MC_BASE_URL` must be reachable from your phone.** On a
Tailscale network, set it to the machine's Tailscale address; the click then
works from anywhere. If the link times out on your phone but works on the
Mac, this is what to check.

The same links also go out via Slack if `SLACK_APPROVALS_CHANNEL` is set —
whichever button you press first wins, the other message is answered with the
result.

## Security

MC applies a hard chat-ID gate on everything inbound: messages from any chat
other than `TELEGRAM_CHAT_ID` are logged and dropped, never answered. Keep
the bot tokens out of git — `.env` is git-ignored for a reason. Anyone with
the token can read and write as that bot.

## Jarvis on Telegram (optional)

With `JARVIS_TELEGRAM_ENABLED=true` (plus an OpenAI key or a local STT
endpoint), text and voice messages in the command chat are answered by the
Jarvis brain instead of being mirrored into the team chat — a mobile
assistant mode. The two inbound modes share one poller, so flipping
`TELEGRAM_TEAM_CHAT_ENABLED` cleanly switches between them. See the
`JARVIS_*` block in `.env.example` for the model settings.
