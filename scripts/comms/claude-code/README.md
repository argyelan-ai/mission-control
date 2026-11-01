# Terminal sessions in MC's directory (Claude Code hook)

ADR-100 stage 2c. Lets a coding-CLI terminal session show up in `mc who`,
receive messages from other sessions (any harness) and answer with
`mc send` — as its **own principal** with a guest card, never as an agent.

## Set up once

1. In MC, an admin issues a guest card (`POST /api/v1/comms/guest-cards`,
   label + lifetime, at most 7 days). The token is shown once.
2. Save it as `~/.mc/comms/guest-card` with mode 0600 (a card others can
   read is refused), or point `MC_GUEST_CARD_FILE` at another file. One card
   enrolls at most 5 live sessions; they share one rate limit.
3. Make the `mc` CLI reachable (`scripts/mc-cli/mc` on `PATH`, or `MC_CLI`)
   and set `MC_API_URL` to your MC.
4. Merge `hooks.example.json` into your user settings and set `MC_REPO` to
   this repository (or write the path directly).

## What it does

| Event | Action |
|---|---|
| session start | `mc hello`: enrolls the session (`claude@<machine>:<folder>`), writes its token to `~/.mc/comms/sessions/<id>.json` (0600), starts a sign of life every 30 s, exports `MC_COMMS_SESSION_FILE` for the session's own `mc who` / `mc send` |
| prompt submitted | reports **busy**; if messages wait, adds one line ("N messages waiting — `mc inbox` reads them"), never the foreign text itself |
| turn ended | reports **idle**. Only with `MC_COMMS_AUTO_READ=1`: if messages wait, the session reads them once (never again in the same chain, never in `hold` mode) |
| session end | `mc bye` (the session token ends), stops the sign of life, deletes the session file |

It never types into a terminal and never interrupts a running turn. A second
terminal in the same folder stays outside while the first one lives (one
address, one session).

**Why reading is opt-in:** a terminal session runs with your local rights.
MC's approval cards guard what goes through MC (merges, deploys, runtime
switches, credentials), not what a session does on its own machine. A
message is foreign text — let a session act on it by itself only where you
accept that.

Modes: `mc presence --accept hold` (keep messages, do not read them
automatically), `--accept refuse` (senders are refused), `--not-listening`.

## Switching it off

- One session: revoke it in MC (`POST /api/v1/comms/sessions/{id}/revoke`).
- Everything from one card: revoke the card.
- All guests at once: `POST /api/v1/comms/guest-cards/revoke-all`.

A message is information, never a permission. Inside MC, an approval card
raised by an agent within 30 minutes after a session message names that
message's sender. Outside MC — a terminal session's own commands — there is
no MC gate; that is the convention tier of ADR-100 §6 rule 4.
