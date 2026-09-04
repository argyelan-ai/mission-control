// Unit test for turn-end-hook.mjs — the streaming heartbeat (message_update)
// must keep the no-progress watchdog fed during long single generations WITHOUT
// spamming one signal line per token. Run: node tests/test_turn_end_hook.mjs
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const dir = mkdtempSync(join(tmpdir(), "omp-hook-"));
const signal = join(dir, "turn-signal.ndjson");
writeFileSync(signal, "");
process.env.OMP_TURN_SIGNAL_FILE = signal;
process.env.OMP_TOOL_HEARTBEAT_MS = "20";

// Deterministic clock so throttle windows are exact (no sleeping).
let fakeNow = 1_000_000;
const realNow = Date.now;
Date.now = () => fakeNow;

const handlers = {};
const api = { on: (ev, cb) => { (handlers[ev] ||= []).push(cb); } };

const hook = (await import("../turn-end-hook.mjs")).default;
hook(api);

function fire(ev) { for (const cb of handlers[ev] || []) cb(); }
function records() {
  return readFileSync(signal, "utf-8").trim().split("\n").filter(Boolean).map(JSON.parse);
}

let failed = 0;
function assert(cond, msg) {
  if (cond) { console.log("PASS " + msg); } else { failed++; console.log("FAIL " + msg); }
}

// message_update must be a registered handler.
assert(Array.isArray(handlers.message_update) && handlers.message_update.length === 1,
  "message_update handler registered");

// 100 rapid deltas within one throttle window -> exactly ONE heartbeat.
for (let i = 0; i < 100; i++) fire("message_update");
let hb = records().filter((r) => r.kind === "progress" && r.at === "message_update");
assert(hb.length === 1, "rapid deltas throttled to one heartbeat (got " + hb.length + ")");

// After the throttle window elapses, the next delta emits again.
fakeNow += 3001;
fire("message_update");
hb = records().filter((r) => r.kind === "progress" && r.at === "message_update");
assert(hb.length === 2, "heartbeat re-emits after throttle window (got " + hb.length + ")");

// Existing boundary heartbeat still works (regression guard).
fire("turn_start");
assert(records().some((r) => r.kind === "progress" && r.at === "turn_start"),
  "turn_start heartbeat intact");

// --- Running tool = alive (04.09.2026) --------------------------------------
// A 17-minute test suite is ONE silent bash call: no tool_execution_end, no
// message_update. The hook must (a) announce tool start/end with the tool's
// name and call id, (b) stamp a periodic heartbeat for as long as at least one
// tool is running, (c) stop the heartbeat once every tool has ended.
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
function fireWith(ev, payload) { for (const cb of handlers[ev] || []) cb(payload); }

assert(Array.isArray(handlers.tool_execution_start), "tool_execution_start handler registered");
assert(Array.isArray(handlers.tool_execution_update), "tool_execution_update handler registered");

fireWith("tool_execution_start", { toolName: "bash", toolCallId: "call-1", args: { command: "sleep 5" } });
let ts = records().filter((r) => r.kind === "tool_start");
assert(ts.length === 1 && ts[0].toolName === "bash" && ts[0].toolCallId === "call-1",
  "tool_start record carries toolName + toolCallId");

// Heartbeat interval is OMP_TOOL_HEARTBEAT_MS (test: 20 ms, default 30 s).
await sleep(120);
let hbTool = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat");
assert(hbTool.length >= 3, "heartbeats while a tool runs (got " + hbTool.length + ")");

// Streaming tool output is a (throttled) heartbeat too.
for (let i = 0; i < 50; i++) fireWith("tool_execution_update", { toolCallId: "call-1" });
const upd = records().filter((r) => r.kind === "progress" && r.at === "tool_execution_update");
assert(upd.length === 1, "tool_execution_update throttled to one heartbeat (got " + upd.length + ")");

// Two tools in flight: ending one keeps the heartbeat alive.
fireWith("tool_execution_start", { toolName: "read", toolCallId: "call-2" });
fireWith("tool_execution_end", { toolName: "read", toolCallId: "call-2" });
const before = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
await sleep(80);
const during = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
assert(during > before, "heartbeat continues while another tool still runs");

// Last tool ends -> tool_end record, heartbeat stops.
fireWith("tool_execution_end", { toolName: "bash", toolCallId: "call-1", isError: false });
const te = records().filter((r) => r.kind === "tool_end");
assert(te.length === 2 && te[1].toolCallId === "call-1", "tool_end record per finished tool");
const after = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
await sleep(80);
const later = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
assert(later === after, "no heartbeat once every tool has ended");

// turn_end / agent_end must never leave a stale "running" tool behind
// (a missed tool_execution_end would otherwise heartbeat forever).
fireWith("tool_execution_start", { toolName: "bash", toolCallId: "call-3" });
fireWith("turn_end", { turnIndex: 1, message: { stopReason: "toolUse", content: [] }, toolResults: [] });
const atTurnEnd = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
await sleep(80);
const afterTurnEnd = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
assert(afterTurnEnd === atTurnEnd, "turn_end clears running tools (no stray heartbeat)");

// ── Open model request = alive (04.09.2026, follow-up to the tool fix) ──────
// Measured live on omp 16.4.6 / GLM-5.3 (26k-token prompt): between turn_start
// and the assistant message_start there were 58 s with NO event at all
// (prefill); a hidden-reasoning model or a bigger context stretches that to
// minutes. So the request window turn_start → assistant message_end must
// heartbeat exactly like a running tool.
fireWith("session_start", {});
const mhb = () => records().filter((r) => r.kind === "progress" && r.at === "model_heartbeat").length;
fireWith("turn_start", { turnIndex: 2 });
const ms = records().filter((r) => r.kind === "model_start");
assert(ms.length >= 1, "turn_start emits a model_start record");
await sleep(80);
assert(mhb() >= 2, "model_heartbeat while the request is open, before any token (got " + mhb() + ")");
fireWith("message_start", { message: { role: "assistant", content: [] } });
fireWith("message_update", { message: { role: "assistant" } });
fireWith("message_end", { message: { role: "assistant", content: [] } });
const me = records().filter((r) => r.kind === "model_end");
assert(me.length === 1, "assistant message_end emits one model_end record");
const mAtEnd = mhb();
await sleep(80);
assert(mhb() === mAtEnd, "no model_heartbeat once the assistant message ended");
// user/toolResult messages must NOT toggle the request window
fireWith("turn_start", { turnIndex: 3 });
fireWith("message_start", { message: { role: "user" } });
fireWith("message_end", { message: { role: "user" } });
const mUser = mhb();
await sleep(80);
assert(mhb() > mUser, "a user message_end does not close the model window");
// turn_end closes everything
fireWith("turn_end", { turnIndex: 3, message: { stopReason: "stop", content: [] }, toolResults: [] });
const mTe = mhb();
await sleep(80);
assert(mhb() === mTe, "turn_end closes the model window (no stray heartbeat)");
// heartbeat label follows the phase: tool wins while a tool runs
fireWith("turn_start", { turnIndex: 4 });
fireWith("message_end", { message: { role: "assistant", content: [] } });
fireWith("tool_execution_start", { toolName: "bash", toolCallId: "call-9" });
const tBefore = records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length;
await sleep(60);
assert(records().filter((r) => r.kind === "progress" && r.at === "tool_heartbeat").length > tBefore,
  "tool phase heartbeats as tool_heartbeat");
fireWith("tool_execution_end", { toolCallId: "call-9" });
fireWith("turn_end", { turnIndex: 4, message: { stopReason: "stop", content: [] }, toolResults: [] });

Date.now = realNow;
console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
