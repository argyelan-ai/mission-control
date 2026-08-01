# Connecting Slack

Slack is a chat channel for talking **with your agent fleet**: you write in a
channel, an agent answers there, and each agent appears under its own name and
avatar instead of one anonymous bot. This is entirely optional. MC runs fine
without Slack, you just lose that channel.

Everything below happens once, takes about ten minutes, and ends with two
tokens pasted into **Settings → Slack**.

## Why Socket Mode (read this first)

Slack's default way of delivering messages is to **call your server** at a
public Request URL. Mission Control is self-hosted: it usually sits on a
machine at home, behind a router or a Tailscale network, with no public URL.
Slack cannot reach it.

**Socket Mode** turns that around. MC opens an outbound WebSocket to Slack and
receives events over it. Nothing needs to be exposed to the internet, no
reverse proxy, no tunnel.

Two consequences that surprise people:

- **There is no Request URL to fill in.** Once Socket Mode is on, Slack stops
  asking for one. If you are hunting for that field, you are looking for
  something that does not exist in this setup.
- **You need a second token.** The bot token talks to Slack's Web API; the
  app-level token is what opens the socket. They are different tokens with
  different prefixes, and mixing them up is the most common setup mistake.

| Token | Looks like | Where it comes from | What it does |
|---|---|---|---|
| Bot User OAuth Token | `xoxb-…` | OAuth & Permissions | Read and post messages |
| App-Level Token | `xapp-…` | Basic Information → App-Level Tokens | Opens the Socket Mode connection |

## 1. Create the app

Go to [api.slack.com/apps](https://api.slack.com/apps), choose **Create New
App**, then **From scratch**. Give it a name (for example `Mission Control`)
and pick the workspace it should live in.

## 2. Turn on Socket Mode

In the left sidebar, open **Socket Mode** and switch **Enable Socket Mode**
on.

## 3. Create the app-level token

This is the step people get stuck on: the token is **not** on the Socket Mode
page. It lives under **Basic Information → App-Level Tokens**.

Click **Generate Token and Scopes**, give it any name (`socket`), add the
scope **`connections:write`**, and generate it. Copy the value, it starts with
`xapp-`. Slack shows it once.

## 4. Add the bot token scopes

Open **OAuth & Permissions → Scopes → Bot Token Scopes** and add all twelve
scopes. Copy this line rather than typing the scopes by hand, a typo here
surfaces much later as a confusing permission error:

```
chat:write, chat:write.customize, channels:read, channels:manage, channels:history, app_mentions:read, im:history, im:write, users:read, reactions:write, files:write, files:read
```

What each one buys you:

| Scope | Why MC needs it |
|---|---|
| `chat:write` | Post messages at all |
| `chat:write.customize` | Post under each agent's own name and avatar |
| `channels:read` | List the channels in the workspace |
| `channels:manage` | Create a channel per project |
| `channels:history` | Read the conversation in channels the bot is in |
| `app_mentions:read` | Notice when an agent is mentioned |
| `im:history` | Read direct messages sent to the app |
| `im:write` | Reply in direct messages |
| `users:read` | Resolve who wrote a message |
| `reactions:write` | Acknowledge a task with an emoji |
| `files:write` | Upload logs and screenshots |
| `files:read` | Download voice messages so they can be transcribed |

**Do not skip `chat:write.customize`.** Without it Slack refuses the
per-message username and icon, so every agent posts as the same app. The chat
still works, which is exactly why the mistake goes unnoticed for days.

**`files:read` is what makes voice messages work.** Record a voice clip in the
team channel and MC transcribes it and hands the text to the agents, exactly
as if you had typed it. Without the scope, Slack answers the file download
with an HTML login page instead of audio — MC detects that and tells you in
the channel rather than failing silently. After adding a scope, Slack requires
you to **re-install the app** (banner at the top of the OAuth page) before it
takes effect.

## 5. Subscribe to events

Under **Event Subscriptions**, turn events on and add these **bot events**:

```
message.channels, app_mention, message.im
```

With Socket Mode enabled, Slack does not ask for a Request URL here.

## 6. Install the app

Back under **OAuth & Permissions**, click **Install to Workspace** and approve
it. Copy the **Bot User OAuth Token**, it starts with `xoxb-`.

Whenever you change scopes later, you must reinstall the app, otherwise the
new scopes are not active.

## 7. Paste the tokens into MC

Open **Settings → Slack** (admin only). There are two fixed fields:

- **Bot User OAuth Token** → the `xoxb-…` value
- **App-Level Token** → the `xapp-…` value

Both are stored encrypted in MC's `secrets` table (never in a file, never in
git) and are only ever shown masked afterwards.

Then press **Test connection**. It calls Slack's `auth.test` with the bot
token and reports the workspace name, the bot name, and whether Socket Mode is
ready. If something is wrong, it says what Slack said, in plain words.

## 8. Create the channel and invite the bot

Make the channel the fleet should talk in — `#your-fleet-channel` below stands
for whatever you name it — and invite the bot into it:

```
/invite @your-app-name
```

A bot that is not a member of a channel can neither read nor post there. This
is the one step people skip, and the symptom is silence: tokens fine,
connection test green, nothing arrives.

## 9. Tell MC which channel to write into

Two settings in `.env`, then `docker compose up -d backend`:

```
SLACK_TEAM_CHAT_ENABLED=true
SLACK_DEFAULT_CHANNEL=#your-fleet-channel
CHAT_CHANNELS=telegram,slack
```

| Setting | What it does |
|---|---|
| `SLACK_TEAM_CHAT_ENABLED` | The channel's own on/off switch. Off by default. |
| `SLACK_DEFAULT_CHANNEL` | Where MC writes. A channel ID (`C0123ABCD`) or `#name`. |
| `CHAT_CHANNELS` | Which channels may run at all. Empty = every channel whose own switch is on. `telegram,slack` runs both, `slack` silences Telegram without removing it. |

These deliberately mirror the Telegram pair (`TELEGRAM_TEAM_CHAT_ENABLED` /
`TELEGRAM_CHAT_ID`). Only the two *tokens* are secrets and live in the
database; a channel name is not a secret and stays in `.env`.

**What you will see.** Every MC conversation — a task, a side thread — opens
its **own Slack thread** in that channel: a parent message named after the
task (`#1a2b3c4d Slack anbinden`), and the whole conversation as replies
underneath. The general chat with Boss speaks in the channel itself, not in a
thread. One channel per project is the next step; today everything lands in
this one channel.

Quiet and loud work through threads: a routine update stays inside its thread,
while a message that needs you (a question, an approval, `@Mark`) is
broadcast back into the channel so it shows up in the channel list. Between
23:00 and 07:00 everything stays quiet except messages marked critical.

## Talking to the agents

Writing in the channel works, no extra step, no `/command`. Three rules decide
who answers — and, just as importantly, who stays quiet.

**In the channel: Boss answers.** Boss is the fleet's contact person, so
anything you write in the channel itself goes to him. He is the one who
distributes work; you do not have to know which agent is free.

**In a thread: the agent working on it answers.** Every task opens its own
Slack thread. Reply inside that thread and the message lands in exactly that
conversation, with the agent who owns it — no need to address anybody.

**`@name` picks somebody specific.** `@rex have another look at this` reaches
Rex directly, in the channel. Inside a task thread a name is only noted, never
followed: the thread stays with its own agent, because switching conversations
because a name was mentioned would be guessing.

Whatever the route, a message ends up in **exactly one** conversation. That is
deliberate: without it, a plain "hello" would reach the whole fleet and come
back as ten answers.

### Sending files to the fleet

Drop a file into the channel or a task thread — MC takes it as a **reference
file** and confirms right where you posted. Where it ends up follows the same
routing as text:

- **In a task thread** → the file is attached to that task; the agent gets the
  absolute path in his conversation and reads it straight off the shared
  `~/.mc` mount.
- **In the channel (or addressed `@agent`)** → the file belongs to that agent
  (usually Boss) — it is often the reason a task gets created next.
- **Voice clips keep working as before** (they are transcribed, not stored).

Accepted types: images (PNG/JPEG/WebP/GIF), PDF, plain text/Markdown/CSV/JSON,
ZIP, XLSX, DOCX — deliberately no HTML/SVG (they could carry active content).
The size cap is `SLACK_FILE_INGEST_MAX_BYTES` (default 25 MB). A refused file
is refused **out loud** in the channel, never silently dropped, and a caption
typed alongside always survives as a normal message.

Agents can send files back, too: `mc report --photo/--file/--vault-path`
delivers into the reports channel, and `mc msg --vault-path` attaches a file
to a chat message in its thread.

### Why there is no real @-mention

The agents are **not members of your Slack workspace**. MC's single bot posts
under each agent's name and face, which is why Boss looks like Boss — but
behind all of them stands one app. So:

- **Slack's autocomplete does not know them.** Typing `@rex` will not offer a
  suggestion, and Slack will not turn it blue. That is expected.
- **MC reads the name out of your text**, and does so tolerantly: `@rex`,
  `@Rex`, `@REX`, `Rex:` and `rex ...` at the start of a message all work, and
  `-` and `_` are interchangeable (`@free-code` = `@FreeCode`).
- **A name in passing does not re-route.** "I asked rex yesterday" goes to
  Boss, like any other channel message. Only a leading name or an explicit `@`
  addresses somebody.
- **An unknown name goes to Boss.** A typo does not vanish, it just lands with
  the contact person.

Everything MC posts is ignored on the way back in — its own messages can never
become new instructions, so the fleet cannot end up talking to itself.

### You do not need a public address

Nothing above requires MC to be reachable from the internet. MC keeps an
outbound WebSocket open to Slack (Socket Mode, see the top of this page) and
receives your messages over it, so a home server behind a router or Tailscale
works exactly like a hosted one. There is no Request URL, no tunnel, no port
forwarding — and no inbound hole in your network.

Slack drops that connection every so often on its own schedule; MC reconnects
by itself. If you run several backend workers, only one of them holds the
connection (they coordinate through Redis), so a message is never processed
twice.

The connection is opened when the backend starts, so switching
`SLACK_TEAM_CHAT_ENABLED` on takes effect after `docker compose up -d backend`
— the same restart step 9 already asks for. When it works you will see
`Slack Socket Mode connected` in the backend log.

## How agents get their names and faces

In Slack each agent posts **as itself** — its own name, its own emoji as the
avatar. That is the whole reason for this channel: Telegram sends everything
from one bot, so an agent's name could only ever be text in front of the
message (`Rex: fertig`). Here Boss looks like Boss and Rex looks like Rex.

The face is a Slack **emoji name** (`:mag:`), not an image. MC is self-hosted
and usually has no address Slack could reach, so an uploaded or generated
avatar would be a URL Slack cannot fetch. An emoji always works.

MC picks the face itself, in this order:

1. **The agent's emoji** (Agent → Edit → Emoji), if Slack knows it. `🔍`
   becomes `:mag:`.
2. **A colon code you type into that same field**, passed through unchanged —
   `:mc-boss:` also works if it is a custom emoji in your workspace. This is
   the manual override, and it needs no extra setting.
3. **The agent's role**, so a new agent is recognisable straight away:

   | Role | Face | | Role | Face |
   |---|---|---|---|---|
   | lead | :crown: 👑 | | researcher | :books: 📚 |
   | orchestrator | :dart: 🎯 | | deployer | :rocket: 🚀 |
   | developer | :zap: ⚡ | | writer | :writing_hand: ✍️ |
   | reviewer | :mag: 🔍 | | tester | :test_tube: 🧪 |
   | planner | :clipboard: 📋 | | | |

4. **A stable face derived from the agent's slug** if the role is unknown —
   different agents get different animals, and each keeps its own forever.

Nothing ever posts nameless or faceless; the chain always ends somewhere.

If every agent shows up under the same app name instead, the
`chat:write.customize` scope is missing — see step 4, and remember to
reinstall the app afterwards.

## It does not work

| What you see | Most likely cause |
|---|---|
| "This looks like the app-level token in the bot token field" | The two tokens are swapped. Bot token starts with `xoxb-`, app-level token with `xapp-`. |
| "No app-level token set" while the workspace shows as connected | The bot token is fine, Socket Mode is not configured yet. Redo step 3: **Basic Information → App-Level Tokens**, scope `connections:write`. |
| `invalid_auth` from Slack | The token was revoked, or it belongs to a different workspace. Reinstall the app (step 6) and copy the token again. |
| All agents post under the same name | `chat:write.customize` is missing. Add it (step 4) and reinstall the app. |
| Agents never see messages in a channel | The bot was not invited to that channel (`/invite @your-app-name`), or `message.channels` is missing from the event subscriptions. |
| Nothing arrives at all, no errors | Socket Mode is off. Step 2. |
| `missing_scope` in an error message | A scope is missing. Compare with the list in step 4, then reinstall the app. |
| `not_in_channel` in the backend log | The bot is not a member of the channel. Open it in Slack and run `/invite @your-app-name` (step 8). |
| `channel_not_found` | `SLACK_DEFAULT_CHANNEL` points at something Slack does not know — or at a private channel the app was never invited to. |
| Connection test is green, but nothing is posted | `SLACK_TEAM_CHAT_ENABLED` is still false, or `CHAT_CHANNELS` lists other channels but not `slack`. |
| MC posts, but never reacts to what you write | Inbound is Socket Mode. Check the backend log for `Slack Socket Mode connected`. No line at all means the channel is switched off; `no app-level token` means step 3 is missing. |
| `@rex` does not turn blue in Slack | Expected. The agents are not Slack users — MC reads the name out of the text. See "Talking to the agents". |
| The same answer arrives twice | Two backends are running against different Redis instances, so both hold a socket. They must share one Redis. |

The same connection check is available without the UI:

```bash
curl -X POST http://localhost/api/v1/slack/test-connection \
  -H "Authorization: Bearer <your-admin-jwt>"
```

The response contains the workspace, the bot name, and the two independent
verdicts (bot token, app-level token). It never contains a token.
