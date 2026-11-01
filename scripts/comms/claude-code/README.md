# Terminal sessions in MC's directory (Claude Code hook)

ADR-100 stage 2c. Lets a coding-CLI terminal session show up in `mc who`,
receive messages from other sessions (any harness) and answer with
`mc send` — as its **own principal** with a guest card, never as an agent.

## Set up once

1. In MC, an admin issues a guest card (`POST /api/v1/comms/guest-cards`,
   label + lifetime, at most 7 days). The token is shown once.
2. Save it as `~/.mc/comms/guest-card` (mode 0600), or point
   `MC_GUEST_CARD_FILE` at another file.
3. Make the `mc` CLI reachable (`scripts/mc-cli/mc` on `PATH`, or `MC_CLI`)
   and set `MC_API_URL` to your MC.
4. Merge `hooks.example.json` into your user settings and set `MC_REPO` to
   this repository (or write the path directly).

## What it does

| Event | Action |
|---|---|
| session start | `mc hello`: enrolls the session (`claude@<machine>:<folder>`), writes its token to `~/.mc/comms/sessions/<id>.json` (0600), starts a sign of life every 30 s, exports `MC_COMMS_SESSION_FILE` for the session's own `mc who` / `mc send` |
| prompt submitted | reports **busy**; messages that waited are added to this turn as context |
| turn ended | reports **idle**; if messages wait, the session reads them once (never again in the same chain, never in `hold` mode) |
| session end | stops listening, ends the sign of life |

It never types into a terminal and never interrupts a running turn. An idle
session waiting for your input reads new messages at your next prompt.

Modes: `mc presence --accept hold` (keep messages, do not read them
automatically), `--accept refuse` (senders are refused), `--not-listening`.

## Switching it off

- One session: revoke it in MC (`POST /api/v1/comms/sessions/{id}/revoke`).
- Everything from one card: revoke the card.
- All guests at once: `POST /api/v1/comms/guest-cards/revoke-all`.

A message is information, never a permission. Anything risky a session does
after a message still needs MC's approval card, which names the message's
sender.
