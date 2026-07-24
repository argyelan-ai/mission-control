import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { ThreadPanel } from "../ThreadPanel";
import type { TaskThreadResponse, ThreadMessage } from "@/lib/types";

const listMock = vi.fn();
const postMock = vi.fn();
const markReadMock = vi.fn();

vi.mock("@/lib/api", () => ({
  api: {
    tasks: {
      thread: {
        list: (...args: unknown[]) => listMock(...args),
        post: (...args: unknown[]) => postMock(...args),
        markRead: (...args: unknown[]) => markReadMock(...args),
      },
    },
  },
}));

const mkMsg = (overrides: Partial<ThreadMessage> = {}): ThreadMessage => ({
  seq: 1,
  id: "msg_1",
  direction: "agent_to_user",
  author: { kind: "agent", id: "boss", display: "Boss" },
  body: "hello from agent",
  body_format: "text",
  created_at: "2026-07-23T09:12:03Z",
  ...overrides,
});

const mkResponse = (overrides: Partial<TaskThreadResponse> = {}): TaskThreadResponse => ({
  task_id: "t1",
  recipient: { kind: "agent", id: "boss", display: "Boss", listening: true, reason: "assignee" },
  messages: [mkMsg()],
  has_more_before: false,
  latest_seq: 1,
  my_read_seq: 1,
  ...overrides,
});

beforeEach(() => {
  vi.clearAllMocks();
  markReadMock.mockResolvedValue(undefined);
});

describe("ThreadPanel", () => {
  it("renders recipient line, messages and the always-on composer", async () => {
    listMock.mockResolvedValue(mkResponse());
    render(<ThreadPanel taskId="t1" />);

    expect((await screen.findAllByText("Boss")).length).toBeGreaterThan(0);
    expect(screen.getByText(/listening/)).toBeTruthy();
    expect(screen.getByText("hello from agent")).toBeTruthy();
    // Composer must be there on every status (post-done delivery is comm_v2's point)
    expect(screen.getByLabelText("Thread message")).toBeTruthy();
    expect(screen.getByLabelText("Send message")).toBeTruthy();
  });

  it("sends via Enter, then appends own message from the since_seq delta", async () => {
    listMock.mockResolvedValue(mkResponse());
    postMock.mockResolvedValue({ message_id: "msg_2", thread_id: "th1", task_status: "done" });
    render(<ThreadPanel taskId="t1" />);
    await screen.findByText("hello from agent");

    // after post, the panel refetches the delta and gets our own message back
    listMock.mockResolvedValue(
      mkResponse({
        messages: [
          mkMsg({
            seq: 2,
            id: "msg_2",
            direction: "user_to_agent",
            author: { kind: "user", id: "mark", display: "Operator" },
            body: "thanks, same pattern tomorrow",
            delivery: "queued",
          }),
        ],
        latest_seq: 2,
        my_read_seq: 1,
      }),
    );

    fireEvent.change(screen.getByLabelText("Thread message"), {
      target: { value: "thanks, same pattern tomorrow" },
    });
    fireEvent.keyDown(screen.getByLabelText("Thread message"), { key: "Enter" });

    await waitFor(() => expect(postMock).toHaveBeenCalledWith("t1", "thanks, same pattern tomorrow"));
    expect(await screen.findByText("thanks, same pattern tomorrow")).toBeTruthy();
    expect(listMock).toHaveBeenLastCalledWith("t1", { sinceSeq: 1 });
    // input cleared after send
    expect((screen.getByLabelText("Thread message") as HTMLInputElement).value).toBe("");
  });

  it("shows the NEW divider above the first unread incoming message", async () => {
    listMock.mockResolvedValue(
      mkResponse({
        messages: [
          mkMsg({ seq: 1, body: "old news" }),
          mkMsg({ seq: 2, id: "msg_2", body: "fresh reply" }),
        ],
        latest_seq: 2,
        my_read_seq: 1,
      }),
    );
    render(<ThreadPanel taskId="t1" />);

    const divider = await screen.findByText("New");
    expect(divider).toBeTruthy();
    // debounced read-marker fires with the latest seq
    await waitFor(() => expect(markReadMock).toHaveBeenCalledWith("t1", 2), { timeout: 4000 });
  });

  it("offers Load older only when has_more_before, paging backwards via before_seq", async () => {
    listMock.mockResolvedValue(mkResponse({ has_more_before: true }));
    render(<ThreadPanel taskId="t1" />);

    const btn = await screen.findByText("Load older");
    listMock.mockResolvedValue(
      mkResponse({
        messages: [mkMsg({ seq: 0, id: "msg_0", body: "ancient" })],
        has_more_before: false,
      }),
    );
    fireEvent.click(btn);

    await waitFor(() => expect(listMock).toHaveBeenLastCalledWith("t1", { beforeSeq: 1 }));
    expect(await screen.findByText("ancient")).toBeTruthy();
  });

  it("degrades to an unavailable note when the read API is not there", async () => {
    listMock.mockRejectedValue(new Error("404"));
    render(<ThreadPanel taskId="t1" />);
    expect(await screen.findByText(/Thread unavailable/)).toBeTruthy();
  });
});
