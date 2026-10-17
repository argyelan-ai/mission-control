/**
 * Demo data for the README screenshots (`readme-shots.mjs`).
 *
 * Everything here is invented: two agents ("Lead" on Claude Code, "Forge" on
 * a local model through omp), one repository (acme/webshop) and a few weeks
 * of token counts. No transcript, host, path or name comes from a real
 * install. The shapes mirror `src/lib/chatTypes.ts` and `src/lib/usage.ts`.
 */

const NOW = Date.now();
export const iso = (msAgo) => new Date(NOW - msAgo).toISOString();
const MIN = 60_000;

// ── Agents (the two chats the remote control is for, ADR-090) ──────────────

const agentBase = {
  archived_at: null, dispatch_config: {}, rules_md: null, last_task_activity_at: iso(2 * MIN),
  context_tokens: 0, updated_at: iso(MIN), discord_channel_name: null, identity_md: null,
  memory_md: null, current_task_id: null, last_trigger_at: null, context_max: 200000,
  agent_token_hash: null, skills: [], soul_persona_md: null, session_message_count: 0,
  board_id: null, role: null, secret_id: null, recycler_enabled: null, last_dispatch_error: null,
  operational_mode: "active", total_tasks_completed: 0, workspace_path: "/workspace",
  skill_filter: [], auto_promote_on_resolution: true, pending_recreate: false,
  total_compactions: 0, emoji: null, heartbeat_config: {}, cli_plugins: [],
  pending_runtime_sync: false, use_operating_card: false, created_at: iso(30 * 24 * 60 * MIN),
  provision_status: "provisioned", last_seen_at: iso(MIN), cli_skills: null, soul_md: null,
  comm_v2: true, operator_language: "en", template_id: null, provisioned_at: iso(30 * 24 * 60 * MIN),
  discord_channel_id: null, mcp_servers: null, tools_md: null, requires_git_workflow: true,
  work_language: "en", runtime_switchable: true, runtime_switch_blocked_reason: null,
  is_board_lead: false, scopes: [],
};

export const LEAD = {
  ...agentBase,
  id: "0000a11d-0000-4000-8000-00000000c0de",
  name: "Lead",
  // The transcript gate in sessions/page.tsx knows the lead agent by slug.
  slug: "boss",
  role: "Lead agent — plans, builds, asks before risky steps",
  harness: "claude",
  agent_runtime: "host",
  runtime_id: null,
  model: "claude-sonnet-4-5",
  status: "working",
  run_state: "working",
  headless_chat: false,
  session_name: "lead",
  session_running: true,
};

export const FORGE = {
  ...agentBase,
  id: "0000f0e9-0000-4000-8000-00000000beef",
  name: "Forge",
  slug: "forge",
  role: "Local coder — runs on your own GPU box",
  harness: "omp",
  agent_runtime: "cli-bridge",
  runtime_id: null,
  model: "qwen3-coder-30b",
  status: "working",
  run_state: "working",
  headless_chat: true,
  container_state: "running",
};

// ── Lead: "fix the coupon rounding" (desktop hero) ──────────────────────────

const cartDiff = {
  path: "src/cart.ts",
  kind: "edit",
  additions: 3,
  deletions: 2,
  truncated: false,
  lines: [
    { t: "hunk", text: "@@ -38,9 +38,10 @@ export function cartTotal(cart: Cart): Money {" },
    { t: "ctx", text: "  const subtotal = sum(cart.items.map(lineTotal));" },
    { t: "del", text: "  const rounded = roundCents(subtotal);" },
    { t: "del", text: "  return applyCoupon(rounded, cart.coupon);" },
    { t: "add", text: "  // Discount first, round once (issue #214)." },
    { t: "add", text: "  const discounted = applyCoupon(subtotal, cart.coupon);" },
    { t: "add", text: "  return roundCents(discounted);" },
    { t: "ctx", text: "}" },
  ],
};

const testDiff = {
  path: "src/cart.test.ts",
  kind: "edit",
  additions: 9,
  deletions: 0,
  truncated: false,
  lines: [
    { t: "hunk", text: "@@ -71,3 +71,12 @@ describe(\"cartTotal\", () => {" },
    { t: "ctx", text: "  });" },
    { t: "add", text: "" },
    { t: "add", text: "  it(\"rounds once, after a percentage coupon\", () => {" },
    { t: "add", text: "    const cart = makeCart([" },
    { t: "add", text: "      { sku: \"mug\", price: 1299, qty: 3 }," },
    { t: "add", text: "    ], { percentOff: 15 });" },
    { t: "add", text: "    expect(cartTotal(cart)).toEqual(cents(3312));" },
    { t: "add", text: "  });" },
    { t: "add", text: "" },
    { t: "ctx", text: "});" },
  ],
};

export function leadEvents() {
  return [
    { kind: "message", uuid: "l1", ts: iso(9 * MIN), role: "user", sidechain: false, model: null,
      text: "Checkout total is one cent off when a percentage coupon applies (issue #214). Fix it and add a test." },
    { kind: "tool", uuid: "l2", ts: iso(8.6 * MIN), name: "Grep", title: 'Search "applyCoupon"', detail: { pattern: "applyCoupon" },
      toolUseId: "t-l2", result: "src/cart.ts:42\nsrc/coupons.ts:7", status: "done", stats: null, sidechain: false },
    { kind: "tool", uuid: "l3", ts: iso(8.4 * MIN), name: "Read", title: "Read cart.ts", detail: { file_path: "/workspace/webshop/src/cart.ts" },
      toolUseId: "t-l3", result: "(84 lines)", status: "done", stats: null, sidechain: false,
      locations: ["/workspace/webshop/src/cart.ts"] },
    { kind: "message", uuid: "l4", ts: iso(8 * MIN), role: "assistant", sidechain: false, model: "claude-sonnet-4-5",
      text: "Found it: `cartTotal` rounds the subtotal **before** the coupon, then the coupon works on the rounded value. I'll discount first and round once." },
    { kind: "tool", uuid: "l5", ts: iso(7.5 * MIN), name: "Edit", title: "Edit cart.ts",
      detail: { file_path: "/workspace/webshop/src/cart.ts" }, toolUseId: "t-l5", result: null, status: "done",
      stats: { additions: 3, deletions: 2 }, sidechain: false, diffs: [cartDiff], locations: ["/workspace/webshop/src/cart.ts"] },
    { kind: "tool", uuid: "l6", ts: iso(6.8 * MIN), name: "Edit", title: "Edit cart.test.ts",
      detail: { file_path: "/workspace/webshop/src/cart.test.ts" }, toolUseId: "t-l6", result: null, status: "done",
      stats: { additions: 9, deletions: 0 }, sidechain: false, diffs: [testDiff], locations: ["/workspace/webshop/src/cart.test.ts"] },
    { kind: "tool", uuid: "l7", ts: iso(5.9 * MIN), name: "Bash", title: "$ npm test -- cart", detail: { command: "npm test -- cart" },
      toolUseId: "t-l7", result: " ✓ src/cart.test.ts (15 tests) 41ms\n\n Test Files  1 passed (1)\n      Tests  15 passed (15)",
      status: "done", stats: null, sidechain: false },
    { kind: "message", uuid: "l8", ts: iso(5.5 * MIN), role: "assistant", sidechain: false, model: "claude-sonnet-4-5",
      text: "Fixed and covered:\n\n- `src/cart.ts` — coupon first, one rounding step\n- `src/cart.test.ts` — new case for a 15 % coupon\n\nAll 15 cart tests pass. The changes are on `fix/coupon-rounding` — commit and open a PR from the Changes panel when you're happy." },
    { kind: "usage", uuid: "l9", ts: iso(5.5 * MIN), inputTokens: 41200, outputTokens: 1850, model: "claude-sonnet-4-5",
      effort: null, contextWindow: 200000, usedPct: 22, source: "cli",
      components: { input: 1200, cacheRead: 38900, cacheCreation: 1100, output: 1850 }, cacheHitRate: 0.94 },
  ];
}

export const LEAD_TODOS = {
  kind: "todo", uuid: "lt1", ts: iso(5.5 * MIN), source: "TodoWrite",
  items: [
    { content: "Find where the coupon is applied", status: "completed" },
    { content: "Discount first, round once", status: "completed" },
    { content: "Add a regression test", status: "completed" },
    { content: "Run the cart tests", status: "completed" },
  ],
};

const REPO = { kind: "session", repo: "webshop", branch: "fix/coupon-rounding", path: "/workspace/webshop", ahead: 0, behind: 0 };

export function leadChanges() {
  return {
    files: [
      { path: "src/cart.ts", additions: 3, deletions: 2, edits: 1, created: false, written: false, lastAt: iso(7.5 * MIN), diff: cartDiff },
      { path: "src/cart.test.ts", additions: 9, deletions: 0, edits: 1, created: false, written: false, lastAt: iso(6.8 * MIN), diff: testDiff },
    ],
    stats: { files: 2, additions: 12, deletions: 2 },
    repo: REPO,
    actions: { commit: { ok: true, reason: null, files: 2 }, pr: { ok: true, reason: null } },
  };
}

export const LEAD_COMMIT_DRAFT = {
  branch: "fix/coupon-rounding",
  files: [
    { path: "src/cart.ts", status: "modified", additions: 3, deletions: 2 },
    { path: "src/cart.test.ts", status: "modified", additions: 9, deletions: 0 },
  ],
  message: "fix(cart): apply the coupon before rounding the total\n\nRounding the subtotal first lost a cent on percentage coupons.\nAdds a regression test for a 15 % coupon. Closes #214.",
  source: "llm",
  mechanicalMessage: "chore: update cart.ts, cart.test.ts",
};

// ── Forge: local model, plan mode, questions (phone + GIF) ──────────────────

const ASK = {
  type: "object",
  properties: {
    q0: { type: "string", title: "Where should sessions live?", oneOf: [
      { const: "Signed cookie", title: "Signed cookie", description: "no server state" },
      { const: "Redis", title: "Redis", description: "already in the stack" },
    ], default: "Signed cookie" },
    q0__other: { type: "string", title: "Other (type your own)" },
    q1: { type: "array", title: "Which checks before the PR?", items: { anyOf: [
      { const: "lint", title: "lint" }, { const: "unit tests", title: "unit tests" }, { const: "e2e", title: "e2e" },
    ] } },
    q1__other: { type: "string", title: "Other (type your own)" },
  },
};

const PLAN = { type: "object", properties: { value: { type: "string", enum: ["Approve and execute", "Refine plan"] } }, required: ["value"] };

export function forgeEvents(stage = "ask") {
  const ev = [
    { kind: "message", uuid: "f1", ts: iso(6 * MIN), role: "user", sidechain: false, model: null,
      text: "Add a login form to the webshop. Plan first, ask me if something is unclear." },
    { kind: "tool", uuid: "f2", ts: iso(5.6 * MIN), name: "read", title: "Read routes/index.ts", detail: {},
      toolUseId: "t-f2", result: "(52 lines)", status: "done", stats: null, sidechain: false, locations: ["/workspace/webshop/src/routes/index.ts"] },
    { kind: "message", uuid: "f3", ts: iso(5.2 * MIN), role: "assistant", sidechain: false, model: "qwen3-coder-30b",
      text: "The shop has no auth yet. Two quick questions before I write the plan." },
    { kind: "elicitation", uuid: "f4", ts: iso(5 * MIN), elicitationId: "el-ask", message: "Answer 2 questions", schema: ASK },
  ];
  if (stage === "ask") return ev;
  ev.push(
    { kind: "elicitation_result", uuid: "f5", ts: iso(4 * MIN), elicitationId: "el-ask", status: "answered",
      content: { q0: "Signed cookie", q1: ["lint", "unit tests"] } },
  );
  if (stage === "answered") return ev;
  ev.push(
    { kind: "message", uuid: "f6", ts: iso(3.6 * MIN), role: "assistant", sidechain: false, model: "qwen3-coder-30b",
      text: "Got it — signed cookie, lint + unit tests. Here is the plan." },
    { kind: "elicitation", uuid: "f7", ts: iso(3.4 * MIN), elicitationId: "el-plan", planApproval: true, schema: PLAN,
      message: "Approve plan \"Login form\" and start implementation?\n\n# Login form\n1. `/login` route with email + password form\n2. Signed session cookie (`src/auth/session.ts`)\n3. Guard `/account` behind the session\n4. Unit tests for the session helpers, then lint" },
  );
  return ev;
}

/** What Forge does right after the operator answered (streamed live in the GIF). */
export function forgeContinues() {
  return [
    { kind: "elicitation_result", uuid: "f5", ts: iso(0), elicitationId: "el-ask", status: "answered",
      content: { q0: "Signed cookie", q1: ["lint", "unit tests"] } },
    { kind: "tool", uuid: "f8", ts: iso(0), name: "write", title: "Write session.ts", detail: {},
      toolUseId: "t-f8", result: null, status: "done", stats: { additions: 7, deletions: 0 }, sidechain: false,
      locations: ["/workspace/webshop/src/auth/session.ts"],
      diffs: [{ path: "src/auth/session.ts", kind: "create", additions: 7, deletions: 0, truncated: false, lines: [
        { t: "add", text: "import { sign, verify } from \"./hmac\";" },
        { t: "add", text: "" },
        { t: "add", text: "export const SESSION_COOKIE = \"shop_session\";" },
        { t: "add", text: "" },
        { t: "add", text: "export const issue = (userId: string) => sign({ userId, at: Date.now() });" },
        { t: "add", text: "export const read = (cookie: string | undefined) =>" },
        { t: "add", text: "  cookie ? verify(cookie) : null;" },
      ] }] },
    { kind: "message", uuid: "f9", ts: iso(0), role: "assistant", sidechain: false, model: "qwen3-coder-30b",
      text: "Signed cookie it is. Session helpers are in — writing the `/login` route next." },
  ];
}

export const FORGE_CAPS = {
  effortLevels: ["low", "medium", "high"], canSwitchEffort: true, effort: "medium", effortLive: true,
  modelOptions: [{ command: "local/qwen3-coder-30b", label: "qwen3-coder-30b" }], model: "local/qwen3-coder-30b",
  modeOptions: [{ value: "default", name: "Default" }, { value: "plan", name: "Plan" }], mode: "plan",
};

export const FORGE_USAGE = {
  kind: "usage", uuid: "fu1", ts: iso(5 * MIN), inputTokens: 18400, outputTokens: 900, model: "qwen3-coder-30b",
  effort: "medium", contextWindow: 131072, usedPct: 14, source: "cli", components: null, cacheHitRate: null,
};

// ── Insights: 26 weeks of invented usage ─────────────────────────────────────

function rng(seed) {
  let s = seed;
  return () => ((s = (s * 1103515245 + 12345) % 2147483648) / 2147483648);
}

const emptyTotals = () => ({
  events: 0, input_tokens: 0, output_tokens: 0, cache_read_tokens: 0, cache_write_tokens: 0,
  total_tokens: 0, generated_tokens: 0, cost_usd: 0, unpriced_events: 0, local_tokens: 0,
  local_output_tokens: 0, local_share: null, local_output_share: null, box: null, via_mc_local: null, external: null,
});

/** `today` is a "YYYY-MM-DD" in UTC (the shots run with timezoneId UTC). */
export function usageByDay(days = 182) {
  const r = rng(7);
  const out = [];
  const today = new Date(new Date().toISOString().slice(0, 10) + "T00:00:00Z").getTime();
  for (let i = days - 1; i >= 0; i--) {
    const ms = today - i * 86_400_000;
    const date = new Date(ms).toISOString().slice(0, 10);
    const dow = new Date(ms).getUTCDay();
    const ramp = 0.35 + 0.65 * ((days - i) / days); // usage grows over the half-year
    const weekend = dow === 0 || dow === 6 ? 0.25 : 1;
    const idle = r() < 0.12;
    const gen = idle ? 0 : Math.round((180_000 + r() * 900_000) * ramp * weekend);
    const localShare = Math.min(0.92, 0.25 + 0.6 * ((days - i) / days) + (r() - 0.5) * 0.1);
    const local = Math.round(gen * localShare);
    const t = emptyTotals();
    Object.assign(t, {
      events: gen ? Math.round(gen / 4000) : 0,
      input_tokens: Math.round(gen * 0.8), output_tokens: Math.round(gen * 0.2),
      cache_read_tokens: gen * 6, total_tokens: gen * 7, generated_tokens: gen,
      cost_usd: Math.round(((gen - local) / 1e6) * 9 * 100) / 100,
      local_tokens: local, local_output_tokens: Math.round(local * 0.2),
      local_share: gen ? localShare : null, local_output_share: gen ? localShare : null,
    });
    out.push({ ...t, date, local_generated_tokens: local, top_source: gen ? (localShare > 0.5 ? "agents:omp" : "lead") : null });
  }
  return { generated_at: new Date().toISOString(), start: out[0].date, tz: "UTC", box_tracking_since: null, days: out };
}

function isoWeek(ms) {
  const d = new Date(ms);
  const day = (d.getUTCDay() + 6) % 7;
  d.setUTCDate(d.getUTCDate() - day + 3);
  const firstThu = new Date(Date.UTC(d.getUTCFullYear(), 0, 4));
  const wk = 1 + Math.round(((d - firstThu) / 86_400_000 - 3 + ((firstThu.getUTCDay() + 6) % 7)) / 7);
  return `${d.getUTCFullYear()}-W${String(wk).padStart(2, "0")}`;
}

export function usageByWeek(weeks = 12) {
  const byDay = usageByDay(weeks * 7 + 7).days;
  const groups = new Map();
  for (const d of byDay) {
    const ms = Date.parse(d.date + "T00:00:00Z");
    const monday = ms - ((new Date(ms).getUTCDay() + 6) % 7) * 86_400_000;
    const key = new Date(monday).toISOString().slice(0, 10);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(d);
  }
  const keys = [...groups.keys()].slice(-weeks);
  const SOURCES = [
    ["agents:omp", 0.42, 1], ["lead", 0.3, 0], ["heads:local", 0.12, 1], ["operator", 0.1, 0], ["agents:claude", 0.06, 0],
  ];
  const out = keys.map((k, idx) => {
    const ds = groups.get(k);
    const totals = emptyTotals();
    for (const d of ds) {
      for (const f of ["events", "input_tokens", "output_tokens", "cache_read_tokens", "total_tokens", "generated_tokens", "cost_usd", "local_tokens", "local_output_tokens"]) {
        totals[f] += d[f];
      }
    }
    totals.cost_usd = Math.round(totals.cost_usd * 100) / 100;
    totals.local_share = totals.generated_tokens ? totals.local_tokens / totals.generated_tokens : null;
    totals.local_output_share = totals.local_share;
    const cloudCost = totals.cost_usd;
    const sources = SOURCES.map(([source, share, isLocal]) => {
      const s = emptyTotals();
      s.generated_tokens = Math.round(totals.generated_tokens * share);
      s.total_tokens = Math.round(totals.total_tokens * share);
      s.output_tokens = Math.round(totals.output_tokens * share);
      s.input_tokens = Math.round(totals.input_tokens * share);
      s.local_tokens = isLocal ? s.generated_tokens : 0;
      s.local_output_tokens = isLocal ? s.output_tokens : 0;
      s.local_share = isLocal ? 1 : 0;
      s.local_output_share = s.local_share;
      s.events = Math.round(totals.events * share);
      // Local runs show the list price they would have cost in the cloud.
      s.cost_usd = Math.round(cloudCost * (isLocal ? share * 0.9 : share * 1.6) * 100) / 100;
      return { source, ...s };
    });
    return { week: isoWeek(Date.parse(k + "T00:00:00Z")), week_start: k, partial: idx === keys.length - 1, totals, sources };
  });
  return { generated_at: new Date().toISOString(), start: out[0].week_start, tz: "UTC", box_tracking_since: null, weeks: out };
}

// ── Runtimes: two invented GPU boxes (TEST-NET addresses, RFC 5737) ─────────

export function demoHosts() {
  const base = { kind: "ssh", role: null, ssh_user: "mc", ssh_key_path: null, ssh_credential_id: null, control_url: null,
    wol_mac_address: null, power_managed: false, notes: null, enabled: true, autostart_enabled: false,
    autostart_recipe_slug: null, created_at: iso(40 * 86_400_000), updated_at: iso(86_400_000) };
  return [
    { ...base, id: "00000000-0000-4000-8000-0000000b0c01", slug: "gpu-box-1", display_name: "gpu-box-1", fabric_ip: "192.0.2.10", ssh_host: "192.0.2.10", ui_order: 1 },
    { ...base, id: "00000000-0000-4000-8000-0000000b0c02", slug: "gpu-box-2", display_name: "gpu-box-2", fabric_ip: "192.0.2.11", ssh_host: "192.0.2.11", ui_order: 2 },
  ];
}

const hostRef = (h) => ({ id: h.id, slug: h.slug, display_name: h.display_name, kind: h.kind, ssh_host: h.ssh_host, enabled: true, power_managed: false });

/** Take the install's own seeded runtimes as the template, keep two local
 *  recipes on the invented boxes and the cloud entries, drop the rest. */
export function demoRuntimes(real, hosts) {
  const pick = (slug) => real.find((r) => r.slug === slug);
  const tpl = pick("qwen-general") ?? real.find((r) => r.locality === "local");
  const local = (over) => ({ ...tpl, state: "running", http_reachable: true, container_status: "running",
    memory_notes: null, startup_notes: null, display_name_drift: [], member_hosts: [], ...over });
  const out = [
    local({ id: "00000000-0000-4000-8000-00000000a001", slug: "qwen3-coder-30b", display_name: "Qwen3 Coder 30B",
      model_identifier: "qwen3-coder-30b", endpoint: "http://192.0.2.10:8000/v1", host_id: hosts[0].id, host: hostRef(hosts[0]),
      max_context_len: 262144, preferred_context_len: 131072, serving_since: iso(5 * 3600e3), role_tags: ["coding"], ui_order: 1 }),
    local({ id: "00000000-0000-4000-8000-00000000a002", slug: "qwen3-35b-fp8", display_name: "Qwen 3.6 35B FP8",
      model_identifier: "qwen3.6-35b-fp8", endpoint: "http://192.0.2.10:8000/v1", host_id: hosts[0].id, host: hostRef(hosts[0]),
      state: "stopped", http_reachable: false, container_status: null, serving_since: null, role_tags: ["general"], ui_order: 2 }),
    local({ id: "00000000-0000-4000-8000-00000000a003", slug: "glm-air", display_name: "GLM 4.6 Air",
      model_identifier: "glm-4.6-air", endpoint: "http://192.0.2.11:8000/v1", host_id: hosts[1].id, host: hostRef(hosts[1]),
      max_context_len: 131072, serving_since: iso(26 * 3600e3), role_tags: ["reasoning"], ui_order: 3 }),
  ];
  for (const slug of ["anthropic-claude-opus", "anthropic-claude-sonnet"]) {
    const r = pick(slug);
    if (r) out.push({ ...r, memory_notes: null, startup_notes: null });
  }
  return out;
}

export function demoLiveStatus() {
  const s = (model, ms, since) => ({ reachable: true, served_model: model, served_context_len: 131072, latency_ms: ms,
    last_probe_at: iso(20_000), consecutive_failures: 0, drift: false, context_drift: false, status: "serving", serving_since: since });
  return { live: {
    "qwen3-coder-30b": s("qwen3-coder-30b", 14, iso(5 * 3600e3)),
    "glm-air": s("glm-4.6-air", 19, iso(26 * 3600e3)),
  }, watcher_enabled: true, interval: 90 };
}

export function demoHostMetrics(i) {
  return [
    { reachable: true, gpu_util_pct: 71, vram_used_mb: 88_000, vram_total_mb: 122_000, gpu_temp_c: 61, ram_used_mb: 96_000, ram_total_mb: 128_000, status: "serving" },
    { reachable: true, gpu_util_pct: 23, vram_used_mb: 64_000, vram_total_mb: 122_000, gpu_temp_c: 48, ram_used_mb: 70_000, ram_total_mb: 128_000, status: "serving" },
  ][i];
}

export function demoHostHistory(i) {
  const now = Math.floor(Date.now() / 1000);
  const points = Array.from({ length: 120 }, (_, k) => {
    const wave = (k * (i ? 13 : 29)) % 17;
    return { t: now - (119 - k) * 30, gpu: (i ? 15 : 55) + wave, ram_used: i ? 70_000 : 96_000, ram_total: 128_000, temp: (i ? 46 : 58) + (wave % 5), fan: null };
  });
  return { points, window: 3600, sample_seconds: 30 };
}

export function demoPulse(i) {
  const now = Math.floor(Date.now() / 1000);
  const points = Array.from({ length: 48 }, (_, k) => ({ t: now - (47 - k) * 75, tps: k % 9 === 0 ? 0 : (i ? 38 : 64) + ((k * 7) % 23) }));
  return { points, now_tps: i ? 41 : 72, idle_seconds: 0, available: true };
}
