"use client";

import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { CommentCard } from "@/components/task/CommentCard";
import { ReflectionForm } from "@/components/task/ReflectionForm";
import type { Agent, Task } from "@/lib/types";
import { C, STATUS_TEXT } from "@/lib/colors";

interface TaskCommentsProps {
  task: Task;
  boardId: string;
  agents: Agent[];
}

export function TaskComments({ task, boardId, agents }: TaskCommentsProps) {
  const qc = useQueryClient();

  const agentMap = Object.fromEntries(
    agents.map((a) => [a.id, { name: a.name, emoji: a.emoji }])
  );

  const { data: comments } = useQuery({
    queryKey: ["task-comments", task.id],
    queryFn: () => api.tasks.comments.list(boardId, task.id),
  });

  const [newComment, setNewComment] = useState("");
  const [commentFilter, setCommentFilter] = useState<string>("all");
  const [newCommentType, setNewCommentType] = useState<string>("progress");

  const addCommentMutation = useMutation({
    mutationFn: ({ content, type }: { content: string; type: string }) =>
      api.tasks.comments.create(boardId, task.id, content, type),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["task-comments", task.id] });
      setNewComment("");
    },
  });

  const typeColors: Record<string, string> = {
    progress: C.online,
    blocker: C.error,
    feedback: C.warning,
    resolution: C.online,
    reflection: STATUS_TEXT.info,
  };

  return (
    <div className="space-y-3">
      {/* Filter pills */}
      {comments && comments.length > 1 && (
        <div className="flex gap-1 flex-wrap">
          {["all", "progress", "blocker", "feedback", "resolution", "reflection"].map((f) => (
            <button
              key={f}
              onClick={() => setCommentFilter(f)}
              className="text-[10px] px-2 py-0.5 rounded-full capitalize transition-colors cursor-pointer"
              style={{
                backgroundColor: commentFilter === f ? C.accent : "rgba(255, 255, 255, 0.03)",
                color: commentFilter === f ? C.textPrimary : C.textMuted,
                border: `1px solid ${commentFilter === f ? C.borderAccent : C.border}`,
              }}
            >
              {f === "all" ? `Alle (${comments.length})` : f}
            </button>
          ))}
        </div>
      )}

      {/* Comment list */}
      <div className="space-y-2">
        {comments
          ?.filter((c) => commentFilter === "all" || c.comment_type === commentFilter)
          .map((c) => (
            <CommentCard key={c.id} comment={c} agentMap={agentMap} />
          ))}
      </div>

      {/* Comment input */}
      <div className="space-y-2">
        {/* Type selector */}
        <div className="flex gap-1">
          {["progress", "blocker", "feedback", "resolution", "reflection"].map((t) => {
            const isActive = newCommentType === t;
            const color = typeColors[t] ?? C.textMuted;
            return (
              <button
                key={t}
                onClick={() => setNewCommentType(t)}
                className="text-[10px] px-2 py-0.5 rounded capitalize transition-colors cursor-pointer"
                style={{
                  backgroundColor: isActive ? `${color}33` : "rgba(255, 255, 255, 0.03)",
                  color: isActive ? color : C.textMuted,
                  border: `1px solid ${isActive ? color : C.border}`,
                }}
              >
                {t}
              </button>
            );
          })}
        </div>

        {/* Input: Reflection-Formular oder normaler Freitext */}
        {newCommentType === "reflection" ? (
          <ReflectionForm
            isSubmitting={addCommentMutation.isPending}
            onSubmit={(content) =>
              addCommentMutation.mutate({ content, type: "reflection" })
            }
          />
        ) : (
          <div className="flex gap-2">
            <input
              value={newComment}
              onChange={(e) => setNewComment(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && newComment.trim()) {
                  addCommentMutation.mutate({
                    content: newComment.trim(),
                    type: newCommentType,
                  });
                }
              }}
              placeholder={
                newCommentType === "blocker"
                  ? "Blockierung beschreiben... (Enter)"
                  : newCommentType === "feedback"
                  ? "Feedback geben... (Enter)"
                  : "Fortschritt kommentieren... (Enter)"
              }
              aria-label="Kommentar eingeben"
              className="flex-1 px-2.5 py-2 rounded-lg text-xs outline-none"
              style={{
                backgroundColor: "rgba(255, 255, 255, 0.03)",
                color: C.textPrimary,
                border: `1px solid ${C.border}`,
              }}
            />
          </div>
        )}
      </div>
    </div>
  );
}
