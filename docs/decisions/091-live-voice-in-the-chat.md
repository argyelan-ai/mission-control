# ADR-091 — Live voice in the chat: the chat agent's ears and mouth over OpenAI Realtime (WebRTC from the browser)

**Status:** Proposed (draft, needs the operator's approval) · would amend ADR-083 decision 2 and ADR-089 §6 (order "voice note first, live later")
**Datum:** 2026-10-12
**Scope:** Frontend/Chat · Backend/Services · Voice

## Context

- ADR-089 §6: the voice is the lead agent's voice — ears and a mouth, no
  hands of its own; everything goes into the agent's chat. It ordered a local
  voice note first and live voice later. ADR-090 made the MC chat the
  operator's remote control away from the desk, phone first.
- The operator approved the full live-voice feature with a cloud realtime
  model on 2026-10-12, ahead of the local voice note.
- Today's live voice (Jarvis, ADR-061/082/083) is a separate server worker
  on LiveKit with its own brain and its own tools; it has no path back to the
  chat agent. Its GPT-Live delegation runs to a backend model (ADR-083
  decision 2), not to the lead agent.
- Checked against OpenAI's documentation on 2026-10-12: `gpt-realtime-2.1`
  is a Realtime model with function tools; WebRTC goes to
  `POST /v1/realtime/calls`; short-lived client secrets come from
  `POST /v1/realtime/client_secrets` (10–7200 s, session config fixed at
  minting); `session.audio.input.turn_detection` takes `semantic_vad` with
  `create_response` / `interrupt_response`; a call can be ended server-side
  with `POST /v1/realtime/calls/{call_id}/hangup`. Price per 1M tokens:
  audio in $32, audio out $64, cached $0.40, text in $4 / out $24.
- A live probe from MC's backend minted a client secret for
  `gpt-realtime-2.1` with this session config (HTTP 200, no audio started).

## Decision (proposed)

1. **Live voice lives in the chat, per chat agent.** A mic in the composer of
   an agent's chat starts a call with *that* agent's voice. (Top-bar mic →
   lead agent comes in PR 5.)
2. **Transport: WebRTC from the browser straight to OpenAI Realtime.** The
   backend mints a 60-second client secret from its own OpenAI key
   (`POST /api/v1/voice/live/session`, admin only); the long-lived key never
   reaches the browser, a response or a log. The session config — model,
   voice, semantic VAD with barge-in, input transcription, persona — is fixed
   server-side inside the secret, so the browser cannot change it. No LiveKit,
   no extra server worker.
3. **Model `gpt-realtime-2.1`** (setting `VOICE_LIVE_MODEL`), not `gpt-live-1`:
   PR 2–4 need function tools, VAD settings and barge-in, which GPT-Live's
   client delegation does not offer.
4. **The voice has no hands.** It talks; the work stays with the chat agent.
   In PR 1 it cannot pass anything on and says so. From PR 2 it forwards the
   operator's words (always with the literal transcript) as a chat message
   marked "by voice"; stop only on a whole-sentence "stop"; a spoken "yes" is
   never an approval.
5. **Off by default** (`VOICE_LIVE_ENABLED=false`). Off or no OpenAI key =
   the endpoints answer 404 and no mic is shown. Turning it off is the undo.
6. **Amends ADR-083 decision 2 and ADR-089 §6** once accepted: the live
   voice delegates to the chat agent (not to a backend model), and live comes
   before the local voice note. Jarvis stays as it is until the operator
   decides on pausing it after two weeks of daily use (ADR-089 §6).

## Alternatives

- **Backend exchanges the SDP** (browser → MC → `/v1/realtime/calls` with
  the server key): the backend learns the `call_id` and can hang up by
  itself. Not chosen for PR 1 — an extra hop for every call setup and the
  same key isolation is reached with the client secret. Reconsidered in PR 6
  for the server-side caps (the browser can report the `call_id`, or the
  backend takes over the exchange).
- **Extend Jarvis/LiveKit** → rejected: separate brain and tools, no path
  back to the chat agent, open plugin bugs (incident 2026-09-12).
- **`gpt-live-1`** → cheaper per minute, but no tools and no VAD settings in
  client delegation; can be added later as a second transport.

## Consequences

### Positive
- Talk to any chat agent from the phone, without a desk, without new infra.
- One truth: the work and its record stay in the chat.

### Negative
- Cloud cost per minute (audio tokens; rough estimate $0.05–0.15 per talking
  minute, to be measured in PR 6). Until PR 6: only a 20-minute cap per call
  in the browser; no server-side cap, no daily budget yet.
- The operator's speech goes to OpenAI. From PR 3 short, redacted progress
  digests do too; never tool output, files or keys.
- Mic only over HTTPS (the browser's rule): via the tailnet HTTPS address.

## References

- Files: `backend/app/services/voice_live.py`, `backend/app/routers/voice_live.py`,
  `frontend-v2/src/components/voice/live/*`, `frontend-v2/src/components/chat/Composer.tsx`
- Related ADRs: ADR-061, ADR-082, ADR-083, ADR-089 §6, ADR-090
- OpenAI docs: guides `realtime-webrtc`, `realtime-vad`; API reference
  `realtime/client_secrets`, `realtime/calls/{call_id}/hangup`; model page `gpt-realtime-2.1`
