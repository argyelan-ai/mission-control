"""LiveKit-level smoke test for the GPT-Live test worker — ADR-083.

Proves the full path: LiveKit room (created with an EXPLICIT agents list,
so automatic dispatch never fires for it) -> our isolated ephemeral worker
-> AgentSession(llm=GPTLiveModel(...)) -> Jarvis persona + tools -> spoken
reply back to a joining "caller" participant.

ISOLATION WARNING (live incident, 10.09.2026): an earlier version of this
script only called `CreateAgentDispatchRequest` after the room already
existed with default config — that ADDS a dispatch job but does NOT stop
LiveKit's automatic dispatch from ALSO sending the job to every worker with
`agent_name=""` (that's the production Jarvis worker). The production
worker really did serve one of our test rooms this way. Fixed: the room is
now created up front via `room.create_room(CreateRoomRequest(agents=[...]))`
— an explicit `agents` list at room-creation time replaces automatic
dispatch for that room entirely, so the production worker is never even
considered for it.

Steps:
1. Create the room with `agents=[RoomAgentDispatch(agent_name=...)]` set —
   this is what actually provides isolation, not a later dispatch call.
2. Join the room as a normal participant ("smoke-caller"), publish a PCM16
   24kHz mono test utterance on a mic-source audio track.
3. Record whatever audio comes back from the remote (agent) participant for
   N seconds, and print a summary.

Usage (inside a container that can reach the LiveKit server, e.g. on the
`mission-control_default` docker network):

    LIVEKIT_URL=ws://livekit:7880 \
    LIVEKIT_API_KEY=... LIVEKIT_API_SECRET=... \
    python3 scripts/gpt_live_livekit_smoke.py \
        --agent-name jarvis-gpt-live-test \
        --room jarvis-gpt-live-smoke-<ts> \
        --audio /smoke/test_de_24k_s16le.pcm \
        --out /smoke/reply.pcm
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

from livekit import api, rtc

SAMPLE_RATE = 24000
NUM_CHANNELS = 1


async def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--agent-name", required=True)
    p.add_argument("--room", required=True)
    p.add_argument("--audio", required=True, help="PCM16 24kHz mono input file")
    p.add_argument("--out", required=True, help="where to write the received PCM16 reply")
    p.add_argument("--listen-seconds", type=float, default=20.0)
    args = p.parse_args()

    url = os.environ["LIVEKIT_URL"]
    ws_url = url.replace("http://", "ws://").replace("https://", "wss://")
    api_key = os.environ["LIVEKIT_API_KEY"]
    api_secret = os.environ["LIVEKIT_API_SECRET"]

    http_url = url.replace("ws://", "http://").replace("wss://", "https://")
    lk_api = api.LiveKitAPI(url=http_url, api_key=api_key, api_secret=api_secret)

    # 1) PRE-CREATE the room with an explicit `agents` list. This is the part
    # that actually matters for isolation (found live, 10.09.2026): a bare
    # CreateAgentDispatchRequest call ADDS an explicit dispatch job, but does
    # NOT stop LiveKit's automatic dispatch from ALSO firing for the room
    # once the first participant creates it — a worker with agent_name=""
    # (the production Jarvis worker) still gets auto-dispatched into it.
    # Team-lead confirmed the production worker served one of our earlier
    # ephemeral test rooms this way. Creating the room up front with
    # `agents=[RoomAgentDispatch(agent_name=...)]` set replaces automatic
    # dispatch for that room entirely — only the named agent gets a job.
    await lk_api.room.create_room(
        api.CreateRoomRequest(
            name=args.room,
            agents=[api.RoomAgentDispatch(agent_name=args.agent_name)],
        )
    )
    print(f"-> room created with explicit agents=[{args.agent_name!r}] (no automatic dispatch)", file=sys.stderr)
    await lk_api.aclose()

    # 2) join as a normal participant ("caller")
    token = (
        api.AccessToken(api_key, api_secret)
        .with_identity("smoke-caller")
        .with_name("Smoke Caller")
        .with_grants(api.VideoGrants(room_join=True, room=args.room))
        .to_jwt()
    )

    room = rtc.Room()
    received_frames: list[bytes] = []
    agent_joined = asyncio.Event()
    track_subscribed = asyncio.Event()

    @room.on("participant_connected")
    def _on_participant(participant: rtc.RemoteParticipant) -> None:
        print(f"<- participant_connected: {participant.identity}", file=sys.stderr)
        agent_joined.set()

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, pub, participant) -> None:
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            print(f"<- subscribed to audio track from {participant.identity}", file=sys.stderr)
            track_subscribed.set()
            asyncio.create_task(_drain_audio(track))

    async def _drain_audio(track: rtc.Track) -> None:
        stream = rtc.AudioStream(track, sample_rate=SAMPLE_RATE, num_channels=NUM_CHANNELS)
        async for ev in stream:
            frame = ev.frame
            received_frames.append(bytes(frame.data))

    await room.connect(ws_url, token)
    print(f"-> connected to room {args.room!r} as smoke-caller", file=sys.stderr)

    try:
        await asyncio.wait_for(agent_joined.wait(), timeout=15.0)
    except asyncio.TimeoutError:
        print(json.dumps({"ok": False, "reason": "agent_never_joined"}))
        await room.disconnect()
        return 1

    # 3) publish the test utterance on a mic-source track
    source = rtc.AudioSource(SAMPLE_RATE, NUM_CHANNELS)
    local_track = rtc.LocalAudioTrack.create_audio_track("smoke-mic", source)
    pub_options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
    await room.local_participant.publish_track(local_track, pub_options)
    print("-> published mic track, streaming test audio", file=sys.stderr)

    with open(args.audio, "rb") as f:
        pcm = f.read()

    frame_ms = 20
    bytes_per_frame = int(SAMPLE_RATE * frame_ms / 1000) * 2  # s16le mono
    for i in range(0, len(pcm), bytes_per_frame):
        chunk = pcm[i : i + bytes_per_frame]
        if len(chunk) < bytes_per_frame:
            chunk = chunk + b"\x00" * (bytes_per_frame - len(chunk))
        frame = rtc.AudioFrame(
            data=chunk,
            sample_rate=SAMPLE_RATE,
            num_channels=NUM_CHANNELS,
            samples_per_channel=len(chunk) // 2,
        )
        await source.capture_frame(frame)
        await asyncio.sleep(frame_ms / 1000)

    print(f"-> finished streaming {len(pcm)} bytes of test audio", file=sys.stderr)

    # 4) wait for + collect the agent's spoken reply
    try:
        await asyncio.wait_for(track_subscribed.wait(), timeout=args.listen_seconds)
    except asyncio.TimeoutError:
        pass

    await asyncio.sleep(args.listen_seconds)

    await room.disconnect()

    total_bytes = sum(len(b) for b in received_frames)
    with open(args.out, "wb") as f:
        for b in received_frames:
            f.write(b)

    result = {
        "ok": total_bytes > 0,
        "room": args.room,
        "agent_name": args.agent_name,
        "reply_audio_bytes": total_bytes,
        "reply_audio_frames": len(received_frames),
        "reply_pcm_file": args.out,
    }
    print(json.dumps(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
