// Thin client for the journey-test stack: operator API calls, fixture SQL,
// and the browser login. Everything here talks to the throw-away stack only
// (MC_JOURNEYS_BASE, default http://localhost:18080) — see docs/journeys.md.
import { execFileSync } from "node:child_process";
import type { Page } from "@playwright/test";

export const BASE = process.env.MC_JOURNEYS_BASE ?? "http://localhost:18080";
export const PROJECT = "mc-journeys";

// Guard: the journeys write boards, agents, tasks and usage rows. The ports
// a normal MC install listens on are refused outright.
{
  const url = new URL(BASE);
  const port = url.port || (url.protocol === "https:" ? "443" : "80");
  if (["80", "443", "3000", "8000"].includes(port)) {
    throw new Error(`MC_JOURNEYS_BASE=${BASE} looks like a real MC install — journeys only run against the test stack`);
  }
}

// The operator account exists only in the throw-away stack; run-journeys.sh
// generates its password per run (also written to the stack env file, so a
// stack kept up with --keep can be reused: `set -a; . <stack.env>; set +a`).
function operator() {
  const password = process.env.MC_JOURNEYS_OPERATOR_PASSWORD;
  if (!password) throw new Error("MC_JOURNEYS_OPERATOR_PASSWORD is not set — run through e2e/run-journeys.sh");
  return { email: "operator@journeys.test", password, name: "Operator" };
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly body: string,
    what: string,
  ) {
    super(`${what} -> HTTP ${status}: ${body.slice(0, 300)}`);
  }
}

export async function api<T = any>(
  method: string,
  path: string,
  token: string,
  body?: unknown,
  headers: Record<string, string> = {},
): Promise<T> {
  const res = await fetch(`${BASE}/api/v1${path}`, {
    method,
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await res.text();
  if (!res.ok) throw new ApiError(res.status, text, `${method} ${path}`);
  return (text ? JSON.parse(text) : null) as T;
}

/** Operator token: registers the first admin on a fresh stack, logs in after. */
export async function operatorToken(): Promise<string> {
  const post = (path: string, body: unknown) =>
    fetch(`${BASE}/api/v1${path}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  const account = operator();
  let res = await post("/auth/register", account);
  if (!res.ok) res = await post("/auth/login", { email: account.email, password: account.password });
  if (!res.ok) throw new ApiError(res.status, await res.text(), "operator login");
  return ((await res.json()) as { access_token: string }).access_token;
}

/** Runs SQL inside the test stack's database container (fixtures only).
 *  The container is found by its compose labels, so no compose file is
 *  parsed (the product docker-compose.yml is never read). */
export function sql(statement: string): string {
  const ids = execFileSync(
    "docker",
    ["ps", "-q", "--filter", `label=com.docker.compose.project=${PROJECT}`, "--filter", "label=com.docker.compose.service=db"],
    { encoding: "utf8" },
  )
    .trim()
    .split("\n")
    .filter(Boolean);
  if (ids.length !== 1) throw new Error(`expected one ${PROJECT} db container, found ${ids.length}`);
  return execFileSync("docker", ["exec", ids[0], "psql", "-U", "mc", "mission_control", "-Atqc", statement], {
    encoding: "utf8",
  }).trim();
}

/** Opens the UI already signed in (same token hand-off the UI probe uses)
 *  with `boardId` as the active board, so each journey sees its own data. */
export async function signIn(page: Page, token: string, boardId?: string): Promise<void> {
  // English UI, pinned: the journeys match visible labels.
  await page.context().addCookies([{ name: "NEXT_LOCALE", value: "en", url: BASE }]);
  await page.addInitScript(
    ([t, b]) => {
      window.localStorage.setItem("mc_auth_token", t);
      if (b) window.localStorage.setItem("mc-app-state", JSON.stringify({ state: { activeBoardId: b }, version: 0 }));
    },
    [token, boardId ?? ""] as const,
  );
}

export async function createBoard(token: string, name: string): Promise<{ id: string }> {
  const slug = `${name.toLowerCase().replace(/[^a-z0-9]+/g, "-")}-${Date.now().toString(36)}`;
  return api("POST", "/boards", token, { name, slug });
}

export async function createTask(
  token: string,
  boardId: string,
  title: string,
  assignedAgentId?: string,
): Promise<{ id: string; status: string }> {
  return api("POST", `/boards/${boardId}/tasks`, token, {
    title,
    assigned_agent_id: assignedAgentId,
  });
}

export async function getTask(token: string, boardId: string, taskId: string): Promise<{ status: string }> {
  return api("GET", `/boards/${boardId}/tasks/${taskId}`, token);
}
