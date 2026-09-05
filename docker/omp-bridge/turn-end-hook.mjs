// docker/omp-bridge/turn-end-hook.mjs — completion oracle for the native-TUI
// omp runtime (ADR-049, supersedes the headless one-shot of ADR-045).
//
// Loaded into the PERSISTENT native omp TUI (tmux Window 0) via
//   omp --hook /opt/omp-bridge/turn-end-hook.mjs ...
// Instead of screen-scraping the pane, it subscribes to omp's structured
// lifecycle events and appends ONE compact JSON line per event to a signal
// file that bridge.py (Window 1) tails. That signal file — never the pane text
// — is the sole source of truth for "did this task finish / abort".
//
// Contract (verified hands-on against omp v16.2.13):
//   * Registration:  export default (api) => { api.on(<event>, cb) }
//     api keys observed: pi, extension, runtime, cwd, events, logger, ...
//   * turn_end fires on EVERY turn incl. errors. ev = {type, turnIndex,
//     message, toolResults}; ev.message.stopReason ∈
//     {stop, toolUse, error, aborted, length}; ev.message.errorMessage /
//     errorStatus are set on errors. A NON-toolUse turn_end is terminal for
//     the current user message (the agentic loop emits toolUse turns first,
//     then exactly one stop|error|aborted|length turn).
//   * session_start fires when a (re)started conversation begins — the bridge
//     uses it as the per-task demarcation after a TUI relaunch/reset.
//   * agent_end fires when the agent finishes responding to one user message —
//     a secondary terminal backstop.
//   * tool_execution_start / _update / _end fire around every tool call
//     (ev.toolName, ev.toolCallId; present in omp 16.4.6) — liveness only.
//
// ROBUSTNESS (non-negotiable): a hook that throws can wedge the TUI. Every
// handler is wrapped; missing fields degrade to null; an unwritable signal
// file is swallowed. This file must NEVER propagate an exception into omp.

import { appendFileSync } from "node:fs";

const SIGNAL_FILE =
  process.env.OMP_TURN_SIGNAL_FILE ||
  ((process.env.OMP_HOME ||
    (process.env.HOME || "/home/agent") + "/.omp") +
    "/turn-signal.ndjson");

function emit(rec) {
  try {
    appendFileSync(SIGNAL_FILE, JSON.stringify(rec) + "\n");
  } catch (_e) {
    /* signal file unavailable — never propagate into the TUI */
  }
}

function assistantText(message) {
  try {
    const parts = [];
    for (const c of (message && message.content) || []) {
      if (c && c.type === "text" && typeof c.text === "string") parts.push(c.text);
    }
    return parts.join("");
  } catch (_e) {
    return "";
  }
}

function sawToolError(ev) {
  try {
    for (const r of (ev && ev.toolResults) || []) {
      if (r && (r.isError || (r.result && r.result.isError))) return true;
    }
  } catch (_e) {
    /* ignore */
  }
  return false;
}

export default (api) => {
  try {
    if (!api || typeof api.on !== "function") {
      emit({ kind: "hook_error", ts: Date.now(), detail: "api.on unavailable" });
      return;
    }

    const STREAM_HEARTBEAT_MS = 3000; // throttle for per-token / per-chunk events

    // Per-task demarcation: a fresh conversation (boot or relaunch/reset).
    api.on("session_start", () =>
      emit({ kind: "session_start", ts: Date.now() })
    );

    // The completion oracle. One line per turn, including error/abort turns.
    api.on("turn_end", (ev) => {
      const m = (ev && ev.message) || {};
      emit({
        kind: "turn_end",
        ts: Date.now(),
        turnIndex:
          ev && typeof ev.turnIndex === "number" ? ev.turnIndex : null,
        stopReason: m.stopReason || null,
        errorMessage: m.errorMessage || null,
        errorStatus: m.errorStatus || null,
        toolError: sawToolError(ev),
        text: assistantText(m),
      });
    });

    // Terminal backstop for one user message's full agentic loop.
    api.on("agent_end", () => emit({ kind: "agent_end", ts: Date.now() }));

    // Liveness heartbeats so the bridge's no-progress watchdog can tell a
    // legitimately-busy TUI (streaming / long tool run) from a wedged one.
    // These carry NO decision weight — they only prove forward progress.
    api.on("turn_start", () => {
      emit({ kind: "progress", at: "turn_start", ts: Date.now() });
      openModelRequest();
    });

    // Running tool = alive (04.09.2026). One silent bash call (a 17-minute test
    // suite) produces NO event between tool_execution_start and _end, so the
    // bridge's idle watchdog (OMP_TURN_IDLE_TIMEOUT) SIGKILLed a working TUI
    // mid-run. Three signals fix that for good:
    //   tool_start / tool_end  — the bridge learns WHICH tool runs (named in the
    //                            diagnosis if omp still wedges inside it);
    //   tool_heartbeat         — a timer stamps liveness every
    //                            OMP_TOOL_HEARTBEAT_MS (30 s) while ≥1 tool is
    //                            in flight. It runs on omp's event loop, so it
    //                            proves the process is alive, not just the tool;
    //   tool_execution_update  — streamed tool output, throttled like deltas.
    // The task deadline (OMP_TASK_DEADLINE) is untouched: a tool that never
    // ends is still stopped by the wall clock.
    const TOOL_HEARTBEAT_MS = Math.max(
      5, Number(process.env.OMP_TOOL_HEARTBEAT_MS) || 30000
    );
    // Open model request = alive (04.09.2026, same day, second lesson). Measured
    // on a 26k-token prompt: between turn_start and the assistant's
    // message_start omp emits NOTHING for 58 s (prefill on the local box);
    // hidden reasoning or a bigger context stretches that to minutes — still
    // no token, still no event. So the request window
    //   turn_start  →  assistant message_end
    // heartbeats too (model_heartbeat). model_start / model_end let the bridge
    // name the phase („Modell-Anfrage offen") if omp wedges inside it. User
    // and toolResult messages do not touch the window.
    const runningTools = new Map(); // toolCallId -> toolName
    let modelInFlight = false;
    let heartbeat = null;
    const stopHeartbeat = () => {
      if (heartbeat) { clearInterval(heartbeat); heartbeat = null; }
    };
    const startHeartbeat = () => {
      if (heartbeat) return;
      heartbeat = setInterval(() => {
        try {
          if (runningTools.size === 0 && !modelInFlight) return stopHeartbeat();
          emit({
            kind: "progress",
            at: runningTools.size ? "tool_heartbeat" : "model_heartbeat",
            ts: Date.now(),
          });
        } catch (_e) { /* never propagate into the TUI */ }
      }, TOOL_HEARTBEAT_MS);
      if (heartbeat && typeof heartbeat.unref === "function") heartbeat.unref();
    };
    const openModelRequest = () => {
      if (modelInFlight) return;
      modelInFlight = true;
      emit({ kind: "model_start", ts: Date.now() });
      startHeartbeat();
    };
    const closeModelRequest = () => {
      if (!modelInFlight) return;
      modelInFlight = false;
      emit({ kind: "model_end", ts: Date.now() });
      if (runningTools.size === 0) stopHeartbeat();
    };
    const isAssistant = (ev) =>
      !!(ev && ev.message && ev.message.role === "assistant");
    api.on("message_start", (ev) => { if (isAssistant(ev)) openModelRequest(); });
    api.on("message_end", (ev) => { if (isAssistant(ev)) closeModelRequest(); });
    const clearTools = () => {
      runningTools.clear(); modelInFlight = false; stopHeartbeat();
    };
    const callId = (ev, fallback) =>
      String((ev && (ev.toolCallId || ev.toolCallID || ev.id)) || fallback);

    api.on("tool_execution_start", (ev) => {
      const id = callId(ev, "tool-" + (runningTools.size + 1));
      const name = String((ev && (ev.toolName || ev.name)) || "?");
      runningTools.set(id, name);
      emit({ kind: "tool_start", toolName: name, toolCallId: id, ts: Date.now() });
      startHeartbeat();
    });
    let lastToolUpdate = 0;
    api.on("tool_execution_update", () => {
      const now = Date.now();
      if (now - lastToolUpdate < STREAM_HEARTBEAT_MS) return;
      lastToolUpdate = now;
      emit({ kind: "progress", at: "tool_execution_update", ts: now });
    });
    api.on("tool_execution_end", (ev) => {
      const id = callId(ev, null);
      if (id !== "null" && runningTools.has(id)) runningTools.delete(id);
      else if (runningTools.size) runningTools.delete([...runningTools.keys()].pop());
      emit({
        kind: "tool_end", toolCallId: id === "null" ? null : id,
        isError: !!(ev && ev.isError), ts: Date.now(),
      });
      if (runningTools.size === 0 && !modelInFlight) stopHeartbeat();
    });
    // No tool survives a turn / response boundary — a missed end event must
    // never leave a phantom "running" tool heartbeating forever.
    api.on("turn_end", clearTools);
    api.on("agent_end", clearTools);
    api.on("session_start", clearTools);

    // Streaming heartbeat. turn_start / tool_execution_end fire only at turn and
    // tool boundaries — so a SINGLE long generation (e.g. the model writing a
    // 2000-line file as one tool call: no tool_execution_end until the args are
    // fully generated) emits no progress for minutes, and the bridge's
    // no-progress watchdog (OMP_TURN_IDLE_TIMEOUT, default 300s) SIGKILLs a
    // genuinely-busy TUI mid-write. message_update fires on every streamed
    // assistant delta (verified: 60 events over a 40-line generation on omp
    // 16.3.8), so we stamp a THROTTLED progress heartbeat — enough to keep the
    // watchdog's last_progress fresh without appending one line per token.
    let lastStreamHeartbeat = 0;
    api.on("message_update", () => {
      const now = Date.now();
      if (now - lastStreamHeartbeat < STREAM_HEARTBEAT_MS) return;
      lastStreamHeartbeat = now;
      emit({ kind: "progress", at: "message_update", ts: now });
    });

    emit({ kind: "hook_ready", ts: Date.now() });
  } catch (e) {
    emit({
      kind: "hook_error",
      ts: Date.now(),
      detail: String(e && e.message ? e.message : e),
    });
  }
};
