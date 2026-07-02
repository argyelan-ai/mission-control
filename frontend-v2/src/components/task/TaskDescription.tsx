"use client";

import ReactMarkdown from "react-markdown";

interface TaskDescriptionProps {
  description: string;
}

export function TaskDescription({ description }: TaskDescriptionProps) {
  return (
    <div
      className="px-4 pb-3 border-b"
      style={{ borderColor: "rgba(255, 255, 255, 0.06)" }}
    >
      <div className="prose-description">
        <ReactMarkdown>{description}</ReactMarkdown>
      </div>
    </div>
  );
}
