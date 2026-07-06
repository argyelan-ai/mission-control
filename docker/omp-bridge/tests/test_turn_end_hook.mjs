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

// Existing boundary heartbeats still work (regression guard).
fire("turn_start");
fire("tool_execution_end");
const progress = records().filter((r) => r.kind === "progress");
assert(progress.some((r) => r.at === "turn_start"), "turn_start heartbeat intact");
assert(progress.some((r) => r.at === "tool_execution_end"), "tool_execution_end heartbeat intact");

Date.now = realNow;
console.log(failed ? `\n${failed} failed` : "\nall passed");
process.exit(failed ? 1 : 0);
