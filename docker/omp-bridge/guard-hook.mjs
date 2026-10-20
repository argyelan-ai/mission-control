// docker/omp-bridge/guard-hook.mjs — the lesson guards (tools/guards) at the
// place where the fleet's mistakes happen: every `bash` tool call of omp.
//
// Same scripts as the Claude Code hooks, so there is one rule set, not two:
//   tool_call   (before)  → guards/bash-hook.sh       — gate chained with ';',
//                           `set -- $var`, push with cut output / no scan,
//                           preview under /tmp, checkout over uncommitted work …
//   tool_result (after)   → guards/post-push-hook.sh  — after a push: does the
//                           remote branch really point at HEAD?
//
// Mode comes from the scripts themselves: GUARD_HOOK_MODE, else ~/.mc/guards/mode,
// else shadow. shadow = only logged (~/.mc/guards/hook-log.jsonl). block = the
// bash call is refused with the guard's reason; after a push that is not proven,
// the reason is appended to the tool result so the agent cannot miss it.
//
// Loaded with `omp --hook` / `omp acp --hook` (launch-omp.sh, acp_client.py).
// Kill switch: OMP_GUARD_HOOK=0 at launch. Guards folder: OMP_GUARDS_DIR
// (default /opt/omp-bridge/guards, copied in at image build).
//
// ROBUSTNESS (non-negotiable): omp turns a THROWING tool_call hook into "blocked"
// for every tool call. Every handler is wrapped; any error, timeout or missing
// script fails OPEN (returns nothing) — a guard bug must never stop the agent.

import { spawnSync } from "node:child_process";
import { appendFileSync, existsSync, mkdirSync } from "node:fs";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

const TIMEOUT_PRE_MS = 60_000;   // the privacy scan before a push can take a while
const TIMEOUT_POST_MS = 30_000;  // one ls-remote

function guardsDir() {
  return process.env.OMP_GUARDS_DIR || "/opt/omp-bridge/guards";
}

// Run one guard script with Claude-Code-shaped hook JSON. Returns the reason
// (stderr) when the script says "block" (exit 2), else null.
function runGuard(script, event, command, cwd, timeout) {
  const path = join(guardsDir(), script);
  if (!existsSync(path)) return null;
  const input = JSON.stringify({
    session_id: process.env.MC_AGENT_NAME || "omp",
    cwd,
    hook_event_name: event,
    tool_name: "Bash",
    tool_input: { command },
  });
  const r = spawnSync("bash", [path], { input, encoding: "utf-8", timeout });
  if (r.error || r.status !== 2) return null;
  return (r.stderr || "").trim() || `${script}: blocked`;
}

// Shape-drift detector: omp changed the event shape (no input.command) while the
// result looks like a push. Without this line the fleet's push proof would just
// be absent; with it the shadow log shows that the hook went blind.
const PUSH_OUTPUT = /(^|\n)To \S+|\[rejected\]|\[remote rejected\]|\s->\s/;
function logShapeDrift(ev, why) {
  try {
    const text = (Array.isArray(ev && ev.content) ? ev.content : [])
      .map((c) => (c && typeof c.text === "string" ? c.text : "")).join("\n");
    if (!PUSH_OUTPUT.test(text)) return;
    const log = process.env.GUARD_HOOK_LOG || join(homedir(), ".mc", "guards", "hook-log.jsonl");
    mkdirSync(dirname(log), { recursive: true });
    appendFileSync(log, JSON.stringify({
      ts: new Date().toISOString(), hook: "omp-shape", session: process.env.MC_AGENT_NAME || "omp",
      reason: why, input_keys: Object.keys((ev && ev.input) || {}),
    }) + "\n");
  } catch (_e) { /* logging must never break the agent */ }
}

function commandOf(ev) {
  const c = ev && ev.input && ev.input.command;
  return typeof c === "string" && c.trim() ? c : null;
}

function cwdOf(ev, ctx, api) {
  const c = (ev && ev.input && ev.input.cwd) || (ctx && ctx.cwd) || (api && api.cwd);
  return typeof c === "string" && c ? c : process.cwd();
}

export default function guardHook(api) {
  try {
    api.on("tool_call", (ev, ctx) => {
      try {
        if (!ev || ev.toolName !== "bash") return undefined;
        const command = commandOf(ev);
        if (!command) return undefined;
        const reason = runGuard("bash-hook.sh", "PreToolUse", command, cwdOf(ev, ctx, api), TIMEOUT_PRE_MS);
        return reason ? { block: true, reason } : undefined;
      } catch (_e) {
        return undefined;
      }
    });
    api.on("tool_result", (ev, ctx) => {
      try {
        if (!ev || ev.toolName !== "bash") return undefined;
        const command = commandOf(ev);
        if (!command) {
          logShapeDrift(ev, "no command in a bash tool_result (input.command missing: omp event shape changed?)");
          return undefined;
        }
        if (!/\bpush\b/.test(command)) return undefined;
        const reason = runGuard("post-push-hook.sh", "PostToolUse", command, cwdOf(ev, ctx, api), TIMEOUT_POST_MS);
        if (!reason) return undefined;
        const content = Array.isArray(ev.content) ? ev.content : [];
        return { content: [...content, { type: "text", text: reason }] };
      } catch (_e) {
        return undefined;
      }
    });
  } catch (_e) {
    /* an omp without these events: the guards are simply not active */
  }
}
