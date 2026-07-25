"use client";

import ReactMarkdown from "react-markdown";

interface TaskDescriptionProps {
  description: string;
}

export function TaskDescription({ description }: TaskDescriptionProps) {
  return (
    <div
      className="px-4 pb-3 border-b"
      style={{ borderColor: "var(--color-border)" }}
    >
      <div className="prose-description">
        <ReactMarkdown>{description}</ReactMarkdown>
      </div>
    </div>
  );
}
