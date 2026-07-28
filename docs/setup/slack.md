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

Open **OAuth & Permissions → Scopes → Bot Token Scopes** and add all eleven
scopes. Copy this line rather than typing the scopes by hand, a typo here
surfaces much later as a confusing permission error:

```
chat:write, chat:write.customize, channels:read, channels:manage, channels:history, app_mentions:read, im:history, im:write, users:read, reactions:write, files:write
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

**Do not skip `chat:write.customize`.** Without it Slack refuses the
per-message username and icon, so every agent posts as the same app. The chat
still works, which is exactly why the mistake goes unnoticed for days.

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

Finally, invite the bot into the channel you want to use:

```
/invite @your-app-name
```

A bot that is not a member of a channel can neither read nor post there.

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

The same connection check is available without the UI:

```bash
curl -X POST http://localhost/api/v1/slack/test-connection \
  -H "Authorization: Bearer <your-admin-jwt>"
```

The response contains the workspace, the bot name, and the two independent
verdicts (bot token, app-level token). It never contains a token.
