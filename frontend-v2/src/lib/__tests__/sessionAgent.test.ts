/**
 * The chat list rows (`/host-sessions/agents`, `/docker-sessions/agents`) are
 * slim: the backend sends only `SessionAgentRow`'s fields, not the whole
 * agent. The frontend type for those rows must say the same, so a read of a
 * dropped field (context_tokens, role, skills, ...) fails `tsc` instead of
 * silently getting `undefined`.
 *
 * Two guards:
 *  1. The type guard below: `@ts-expect-error` lines turn `tsc --noEmit` red
 *     if anyone widens the row type back to the full `Agent`.
 *  2. The drift test: the frontend key list equals the backend model's
 *     fields, so adding a field on one side only goes red.
 */
import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

import type { api } from "../api";
import { SESSION_AGENT_KEYS, type SessionAgent } from "../types";

type HostRow = Awaited<ReturnType<typeof api.agents.listHostSessions>>[number];
type DockerRow = Awaited<ReturnType<typeof api.agents.listDockerSessions>>[number];

// Type guard — never called, only type-checked by `tsc --noEmit`.
export function _typeGuard(host: HostRow, docker: DockerRow, row: SessionAgent) {
  // Fields the backend still sends compile.
  const ok: unknown[] = [
    host.id,
    host.current_task_id,
    host.session_running,
    docker.container_state,
    row.headless_chat,
  ];
  // @ts-expect-error context_tokens is not in a chat list row
  ok.push(host.context_tokens);
  // @ts-expect-error role is not in a chat list row
  ok.push(docker.role);
  // @ts-expect-error soul_md must never be in a chat list row
  ok.push(row.soul_md);
  return ok;
}

function backendRowFields(): string[] {
  const src = readFileSync(
    join(__dirname, "../../../../backend/app/routers/cli_terminal.py"),
    "utf8",
  );
  const start = src.indexOf("class SessionAgentRow(BaseModel):");
  expect(start).toBeGreaterThan(-1);
  const fields: string[] = [];
  for (const line of src.slice(start).split("\n").slice(1)) {
    if (/^\S/.test(line) || /^\s+(@|def |# )/.test(line)) break;
    const m = /^ {4}(\w+): /.exec(line);
    if (m) fields.push(m[1]);
  }
  return fields;
}

describe("SessionAgent (slim chat list row)", () => {
  it("lists exactly the fields the backend row model sends", () => {
    expect([...SESSION_AGENT_KEYS].sort()).toEqual(backendRowFields().sort());
  });

  it("never lists prompt documents or the token hash", () => {
    for (const key of ["agent_token_hash", "soul_md", "tools_md", "memory_md"]) {
      expect(SESSION_AGENT_KEYS as readonly string[]).not.toContain(key);
    }
  });
});
