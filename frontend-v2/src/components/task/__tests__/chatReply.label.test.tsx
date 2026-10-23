/**
 * Round-5 usertest t1 NICE-1: the chat session's reply on a card
 * (`chat_reply`) read "ⓘ Chat reply" in the timeline — the generic info
 * icon and the backend's raw English type name — and in "Comments" it looked
 * like a comment of the run, without any tag. The operator could not tell
 * that this entry came from the chat and is no sign of the run (the rule the
 * backend now applies in every activity reader).
 */
import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import de from "../../../../messages/de.json";
import en from "../../../../messages/en.json";
import { TaskTimeline } from "../TaskTimeline";
import { CommentCard } from "../CommentCard";
import type { TaskComment, TaskTimelineEntry } from "@/lib/types";

const timelineEntry: TaskTimelineEntry = {
  ts: "2026-10-23T15:01:54Z",
  source: "comment",
  kind: "chat_reply",
  title: "Chat reply", // what the backend sends (raw type name)
  detail: "Status 17:01: task taken and started, the run is going.",
  actor: "Helper",
  meta: null,
};

const comment: TaskComment = {
  id: "c1",
  task_id: "t1",
  author_type: "agent",
  author_agent_id: "a1",
  author_agent_name: "Helper",
  author_agent_emoji: null,
  comment_type: "chat_reply",
  content: "Status 17:01: task taken and started, the run is going.",
  created_at: "2026-10-23T15:01:54Z",
};

describe("chat_reply carries its own label", () => {
  it("timeline: translated label with the 'not activity' hint, not the raw type", () => {
    render(<TaskTimeline entries={[timelineEntry]} isLoading={false} />);
    const label = screen.getByTestId("timeline-title-chat_reply");
    expect(label).toHaveTextContent(en.tasks.chatReply.label);
    expect(label).toHaveAttribute("title", en.tasks.chatReply.hint);
  });

  it("comments tab: the reply gets a chat tag instead of looking like a run comment", () => {
    render(<CommentCard comment={comment} />);
    const badge = screen.getByTestId("comment-type-badge");
    expect(badge).toHaveTextContent(en.tasks.chatReply.label);
    expect(badge).toHaveAttribute("title", en.tasks.chatReply.hint);
  });

  it("a plain message still has no tag", () => {
    render(<CommentCard comment={{ ...comment, comment_type: "message" }} />);
    expect(screen.queryByTestId("comment-type-badge")).toBeNull();
  });

  it("German UI says Chat-Antwort", () => {
    expect(de.tasks.chatReply.label).toBe("Chat-Antwort");
    expect(de.tasks.chatReply.hint).toMatch(/nicht als Aktivität/);
  });
});
