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
    api.on("turn_start", () =>
      emit({ kind: "progress", at: "turn_start", ts: Date.now() })
    );
    api.on("tool_execution_end", () =>
      emit({ kind: "progress", at: "tool_execution_end", ts: Date.now() })
    );

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
    const STREAM_HEARTBEAT_MS = 3000;
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
