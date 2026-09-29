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

  // Defekt 3 (Operator-Befund 15.09.2026): Die erste Nachricht eines neuen
  // Tasks ist das Dispatch-Briefing — ein ganzes Dokument mit `##`-Abschnitten,
  // `**Fettschrift**` und dem internen Marker `<!-- mc:briefing:attempt=… -->`.
  // Es lief durch denselben Zweig wie eine einzeilige System-Notiz und stand
  // deshalb als roher Text in `text-center font-mono text-[10px]`: der Marker
  // war sichtbar, die Gliederung weg, Pfade und Portnummern standen als
  // Fliesstext in der Zeile.
  const BRIEFING_BODY =
    "<!-- mc:briefing:attempt=abc123 -->\n" +
    "# New Task: Mobile chat\n" +
    "**Working directory:** `/workspace/wt`\n" +
    "**Dev server port:** 3001\n" +
    "## Approach\n" +
    "- Schritt eins\n";

  it("rendert das Dispatch-Briefing als Markdown und versteckt den internen Marker", async () => {
    listMock.mockResolvedValue(
      mkResponse({
        messages: [
          mkMsg({
            id: "msg_brief",
            direction: "system",
            author: { kind: "system", id: "dispatch", display: "System" },
            body_format: "markdown",
            body: BRIEFING_BODY,
          }),
        ],
      }),
    );
    const { container } = render(<ThreadPanel taskId="t1" />);

    const card = await screen.findByTestId("thread-briefing");
    // Der Marker ist ein internes Detail der Zustellung, keine Information
    // fuer den Leser — er darf nirgends mehr im DOM stehen.
    expect(container.innerHTML).not.toContain("mc:briefing");
    expect(container.innerHTML).not.toContain("<!--");
    // Dokumentstruktur statt Fliesstext: Ueberschrift als Ueberschrift,
    // Fettschrift als <strong>, Pfad und Port als <code>.
    expect(screen.getByRole("heading", { level: 1, name: /New Task: Mobile chat/ })).toBeTruthy();
    expect(screen.getByText("/workspace/wt")).toBeTruthy();
    expect(card.querySelector("strong")).toBeTruthy();
    expect(card.textContent).toContain("3001");
    // Und eben nicht mehr in der einzeiligen System-Notiz-Optik.
    expect(container.querySelector(".text-center.font-mono")).toBeNull();
  });

  it("laesst eine echte einzeilige System-Notiz in der alten Optik", async () => {
    listMock.mockResolvedValue(
      mkResponse({
        messages: [
          mkMsg({
            id: "msg_note",
            direction: "system",
            author: { kind: "system", id: "migration", display: "System" },
            body_format: "text",
            body: "Migration: bisheriger Verlauf liegt in den Kommentaren dieses Tasks.",
          }),
        ],
      }),
    );
    const { container } = render(<ThreadPanel taskId="t1" />);

    const note = await screen.findByText(/Migration: bisheriger Verlauf/);
    expect(note.className).toContain("text-center");
    expect(note.className).toContain("font-mono");
    expect(screen.queryByTestId("thread-briefing")).toBeNull();
    expect(container.querySelector("h1")).toBeNull();
  });
});
