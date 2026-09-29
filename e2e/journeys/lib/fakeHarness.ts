// The fake harness (concept C6): a stand-in for a real agent runtime that can
// do only the bare minimum — take a task, ask, block, request an approval,
// and record everything MC delivers to it. It speaks exactly the HTTP the
// real bridges speak (GET /agent/me/poll + the agent-scoped endpoints with
// the agent token), so a journey ends on the EFFECT: "the agent received the
// answer", not "a button was clicked".
//
// The agent row is created through the normal API with agent_runtime
// "manual" (no container provisioning, no docker). comm_v2 has no API
// setter (pilot flag), so the fixture sets it in the test database.
import { api, sql } from "./mc";

type Poll = {
  state: string;
  task?: { id: string; board_id: string; title: string; status: string; dispatch_attempt_id?: string };
  new_comments?: Array<{ content?: string; comment_type?: string }>;
  new_messages?: Array<{ body?: string; message_type?: string; thread_id?: string; seq?: number }>;
};

export class FakeAgent {
  /** Everything the agent ever received through the poll, in order. */
  readonly received: Poll[] = [];
  private acked: Record<string, number> = {};
  private attemptId?: string;
  taskId?: string;

  private constructor(
    readonly id: string,
    readonly name: string,
    readonly boardId: string,
    private readonly token: string,
  ) {}

  /** `lead`: a board lead. Only leads hold the active-task lock that a
   *  blocking ask needs (workers run parallel sessions, see the
   *  use_subagent_dispatch branches in agent_task_status.py). */
  static async create(
    operatorToken: string,
    boardId: string,
    name: string,
    opts: { lead?: boolean } = {},
  ): Promise<FakeAgent> {
    const agent = await api<{ id: string; token: string }>("POST", "/agents", operatorToken, {
      name,
      board_id: boardId,
      agent_runtime: "manual",
      is_board_lead: opts.lead ?? false,
    });
    if (!/^[0-9a-f-]{36}$/.test(agent.id)) throw new Error(`unexpected agent id ${agent.id}`);
    sql(`update agents set comm_v2 = true where id = '${agent.id}'`);
    return new FakeAgent(agent.id, name, boardId, agent.token);
  }

  private call<T = any>(method: string, path: string, body?: unknown): Promise<T> {
    const headers: Record<string, string> = this.attemptId ? { "X-Dispatch-Attempt-Id": this.attemptId } : {};
    return api<T>(method, `/agent${path}`, this.token, body, headers);
  }

  /** One poll cycle, acking delivered thread messages like the real bridge. */
  async poll(): Promise<Poll> {
    const ack = Object.keys(this.acked).length ? `?acked_seq=${encodeURIComponent(JSON.stringify(this.acked))}` : "";
    const res = await this.call<Poll>("GET", `/me/poll${ack}`);
    for (const m of res.new_messages ?? []) {
      if (m.thread_id && typeof m.seq === "number") {
        this.acked[m.thread_id] = Math.max(this.acked[m.thread_id] ?? 0, m.seq);
      }
    }
    this.received.push(res);
    if (res.task?.dispatch_attempt_id) this.attemptId = res.task.dispatch_attempt_id;
    return res;
  }

  /** Polls until a task is handed over, then ACKs it (status in_progress). */
  async takeTask(timeoutMs = 20_000): Promise<string> {
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
      const res = await this.poll();
      if (res.state === "new_task" && res.task) {
        this.taskId = res.task.id;
        await this.call("PATCH", `/boards/${this.boardId}/tasks/${res.task.id}`, { status: "in_progress" });
        return res.task.id;
      }
      await sleep(500);
    }
    throw new Error(`${this.name}: no task handed over within ${timeoutMs} ms`);
  }

  /** Blocking question: MC parks the task in `waiting` until the operator answers. */
  async askBlocking(question: string): Promise<void> {
    await this.call("POST", "/tasks/current/ask", {
      question,
      blocking: true,
      to: "mark", // the operator target (comm_constants.QUESTION_TARGETS)
    });
  }

  /** Reports a blocker that needs the operator's decision. MC itself turns
   *  it into a `blocker_decision` approval (the "unblock" path, F21). */
  async blockForDecision(description: string, question: string): Promise<void> {
    await this.call("PATCH", `/boards/${this.boardId}/tasks/${this.taskId}`, {
      status: "blocked",
      blocker_type: "decision_needed",
      blocker_description: description,
      blocker_question: question,
    });
  }

  /** Every delivered text (thread messages + comments) over `ms` of polling. */
  async deliveriesDuring(ms: number): Promise<string[]> {
    const texts: string[] = [];
    const until = Date.now() + ms;
    while (Date.now() < until) {
      const res = await this.poll();
      texts.push(...(res.new_messages ?? []).map((m) => m.body ?? ""), ...(res.new_comments ?? []).map((c) => c.content ?? ""));
      await sleep(500);
    }
    return texts;
  }

  /** Polls until `match` finds something in a delivery; returns that text. */
  async waitForDelivery(match: (text: string) => boolean, timeoutMs = 20_000): Promise<string> {
    const until = Date.now() + timeoutMs;
    while (Date.now() < until) {
      const res = await this.poll();
      const texts = [
        ...(res.new_messages ?? []).map((m) => m.body ?? ""),
        ...(res.new_comments ?? []).map((c) => c.content ?? ""),
      ];
      const hit = texts.find(match);
      if (hit !== undefined) return hit;
      await sleep(500);
    }
    throw new Error(`${this.name}: expected delivery did not arrive within ${timeoutMs} ms`);
  }
}

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
