// Unit test for guard-hook.mjs — the lesson guards (tools/guards) inside omp.
// Run: node tests/test_guard_hook.mjs   (from docker/omp-bridge; tools/guards must exist)
//
// The hook must (1) block a wrong bash command in block mode with the guard's
// reason, (2) let a right one through, (3) only log in shadow mode, (4) append the
// push-proof finding to a bash result after a push that did not reach the remote,
// and (5) never throw — a throwing omp hook blocks EVERY tool call.
import { mkdtempSync, mkdirSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { execFileSync } from "node:child_process";

const here = dirname(fileURLToPath(import.meta.url));
const guards = process.env.TEST_GUARDS_DIR || join(here, "..", "..", "..", "tools", "guards");
if (!existsSync(join(guards, "bash-hook.sh"))) {
  console.log("FAIL tools/guards not found at " + guards);
  process.exit(1);
}
const dir = mkdtempSync(join(tmpdir(), "omp-guard-"));
process.env.OMP_GUARDS_DIR = guards;
process.env.GUARD_HOOK_LOG = join(dir, "log.jsonl");
process.env.HOME = dir; // no operator mode file leaks into the test

const handlers = {};
const api = { cwd: dir, on: (ev, cb) => { (handlers[ev] ||= []).push(cb); } };
const hook = (await import("../guard-hook.mjs")).default;
hook(api);

let failed = 0;
function assert(cond, msg) {
  if (cond) console.log("PASS " + msg);
  else { failed++; console.log("FAIL " + msg); }
}
async function call(ev, payload) {
  let out;
  for (const cb of handlers[ev] || []) out = (await cb(payload, { cwd: dir })) ?? out;
  return out;
}
const G = "g" + "it", P = "pu" + "sh";

assert((handlers.tool_call || []).length === 1, "tool_call handler registered");
assert((handlers.tool_result || []).length === 1, "tool_result handler registered");

// --- block mode -----------------------------------------------------------
process.env.GUARD_HOOK_MODE = "block";
let r = await call("tool_call", { type: "tool_call", toolName: "bash", toolCallId: "1",
  input: { command: 'set -- $ITEM; ' + G + ' worktree add "$1" -b "$2" origin/main' } });
assert(r && r.block === true, "block mode: set -- $var is blocked");
assert(r && /zsh-set-split/.test(r.reason || ""), "block mode: the reason names the guard");

r = await call("tool_call", { type: "tool_call", toolName: "bash", toolCallId: "2",
  input: { command: "preflight.sh --worktrees 1 | tail -1; docker compose up -d" } });
assert(r && r.block === true, "block mode: a gate chained with ';' is blocked");

r = await call("tool_call", { type: "tool_call", toolName: "bash", toolCallId: "3",
  input: { command: "ls -la && " + G + " status --short" } });
assert(!r || !r.block, "block mode: a right command passes");

r = await call("tool_call", { type: "tool_call", toolName: "read", toolCallId: "4",
  input: { path: "/etc/hosts" } });
assert(!r || !r.block, "other tools are never checked");

// --- post hook: push that never reached the remote ------------------------
const repo = join(dir, "repo");
mkdirSync(repo);
const remote = join(dir, "remote.git");
execFileSync("git", ["init", "-q", "--bare", remote]);
execFileSync("git", ["init", "-q", "-b", "feat/x", repo]);
execFileSync("git", ["-C", repo, "-c", "user.email=t@example.invalid", "-c", "user.name=t",
  "commit", "-q", "--allow-empty", "-m", "x"]);
execFileSync("git", ["-C", repo, "remote", "add", "origin", remote]);
const pushCmd = G + " -C " + repo + " " + P + " -u origin feat/x 2>&1 | tail -1";
const content = [{ type: "text", text: "ok" }];
r = await call("tool_result", { type: "tool_result", toolName: "bash", toolCallId: "5",
  input: { command: pushCmd }, content, isError: false });
assert(r && Array.isArray(r.content) && r.content.length === 2, "post: an unproven push appends a finding");
assert(r && /NOT proven/.test(r.content[1].text || ""), "post: the finding says the push is not proven");
assert(r && r.content[0] === content[0], "post: the original output is kept");

execFileSync("git", ["-C", repo, P, "-q", "-u", "origin", "feat/x"]);
r = await call("tool_result", { type: "tool_result", toolName: "bash", toolCallId: "6",
  input: { command: pushCmd }, content, isError: false });
assert(!r, "post: a proven push changes nothing");

// --- shadow mode ----------------------------------------------------------
process.env.GUARD_HOOK_MODE = "shadow";
r = await call("tool_call", { type: "tool_call", toolName: "bash", toolCallId: "7",
  input: { command: 'set -- $ITEM; echo "$1"' } });
assert(!r || !r.block, "shadow mode: nothing is blocked");
const log = existsSync(process.env.GUARD_HOOK_LOG) ? readFileSync(process.env.GUARD_HOOK_LOG, "utf-8") : "";
assert(/zsh-set-split/.test(log) && /"mode": "shadow"/.test(log), "shadow mode: the finding is logged");

// --- never throw ----------------------------------------------------------
process.env.GUARD_HOOK_MODE = "block";
let threw = false;
try {
  r = await call("tool_call", { type: "tool_call", toolName: "bash", toolCallId: "8", input: null });
  r = await call("tool_call", undefined);
  r = await call("tool_result", { toolName: "bash", input: { command: 42 }, content: null });
} catch (_e) { threw = true; }
assert(!threw, "broken events never throw");

process.env.OMP_GUARDS_DIR = join(dir, "missing");
r = await call("tool_call", { type: "tool_call", toolName: "bash", toolCallId: "9",
  input: { command: 'set -- $ITEM; echo "$1"' } });
assert(!r || !r.block, "missing guards folder: fail open, nothing blocked");

console.log(failed ? `${failed} FAILED` : "ALL PASS");
process.exit(failed ? 1 : 0);
