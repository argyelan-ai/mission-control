"use client";

import React from "react";
import ReactMarkdown from "react-markdown";
import { parseComment, type ParsedComment } from "@/lib/parseComment";
import { timeAgo } from "@/lib/utils";
import type { TaskComment } from "@/lib/types";
import { C, STATUS_TEXT, LANE } from "@/lib/colors";

// ── Types ──────────────────────────────────────────────────────────────────────

type CommentType =
  | "progress"
  | "blocker"
  | "feedback"
  | "resolution"
  | "checkpoint"
  | "handoff"
  | "message"
  | "reflection"
  | "waiting_on_callback";

interface CommentCardProps {
  comment: TaskComment;
  agentMap?: Record<string, { name: string; emoji: string | null }>;
}

// ── Constants ──────────────────────────────────────────────────────────────────

const TYPE_COLORS: Record<CommentType, string> = {
  progress: C.online,
  checkpoint: C.accent,
  blocker: C.error,
  feedback: C.warning,
  resolution: C.online,
  handoff: C.accent,
  message: "transparent",
  reflection: STATUS_TEXT.info,
  waiting_on_callback: C.warning,
};

const TYPE_LABELS: Record<CommentType, string> = {
  progress: "Progress",
  checkpoint: "Checkpoint",
  blocker: "Blocker",
  feedback: "Feedback",
  resolution: "Resolution",
  handoff: "Handoff",
  message: "Message",
  reflection: "Self-Reflection",
  waiting_on_callback: "Waiting on Callback",
};

const STATUS_PILL_COLORS: Record<string, { bg: string; text: string }> = {
  blocked: { bg: `${C.error}26`, text: C.error },
  failed: { bg: `${C.error}26`, text: C.error },
  done: { bg: `${C.online}26`, text: C.online },
  in_progress: { bg: LANE.in_progress + "26", text: LANE.in_progress },
  review: { bg: `${C.warning}26`, text: C.warning },
  inbox: { bg: `${C.textSecondary}26`, text: C.textSecondary },
  user_test: { bg: C.accentSubtle, text: C.accent },
  aborted: { bg: `${C.textMuted}26`, text: C.textMuted },
};

// ── Helpers ────────────────────────────────────────────────────────────────────

function resolveType(comment: TaskComment): CommentType {
  const ct = comment.comment_type;
  if (ct && ct in TYPE_COLORS) return ct as CommentType;
  return "message";
}

type AuthorKind = "user" | "agent" | "system";

function resolveAuthor(
  comment: TaskComment,
  agentMap?: Record<string, { name: string; emoji: string | null }>
): { label: string; kind: AuthorKind } {
  if (comment.author_type === "user") {
    return { label: "Du", kind: "user" };
  }

  if (comment.author_type === "system") {
    return { label: "⚙ System", kind: "system" };
  }

  const name = comment.author_agent_name;
  const emoji = comment.author_agent_emoji;
  if (name) {
    return { label: `${emoji || ""} ${name}`.trim(), kind: "agent" };
  }

  if (agentMap && comment.author_agent_id) {
    const agent = agentMap[comment.author_agent_id];
    if (agent) {
      return {
        label: `${agent.emoji || ""} ${agent.name}`.trim(),
        kind: "agent",
      };
    }
  }

  return { label: "Agent", kind: "agent" };
}

/** Rendert Status-Transitionen wie "blocked -> done" als farbige Pills */
function renderContentWithStatusPills(text: string): React.ReactNode[] {
  const transitionPattern =
    /\b(inbox|in_progress|review|user_test|done|blocked|failed|aborted)\s*(?:->|-->|→)\s*(inbox|in_progress|review|user_test|done|blocked|failed|aborted)\b/g;

  const parts: React.ReactNode[] = [];
  let lastIndex = 0;
  let m: RegExpExecArray | null;

  while ((m = transitionPattern.exec(text)) !== null) {
    if (m.index > lastIndex) {
      parts.push(text.slice(lastIndex, m.index));
    }

    const from = m[1];
    const to = m[2];
    const fromColors = STATUS_PILL_COLORS[from] || STATUS_PILL_COLORS.inbox;
    const toColors = STATUS_PILL_COLORS[to] || STATUS_PILL_COLORS.inbox;

    parts.push(
      <span key={`transition-${m.index}`} style={{ display: "inline-flex", alignItems: "center", gap: 4 }}>
        <span
          style={{
            display: "inline-block",
            padding: "1px 8px",
            borderRadius: 9999,
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.02em",
            background: fromColors.bg,
            color: fromColors.text,
          }}
        >
          {from}
        </span>
        <span style={{ color: C.textMuted, fontSize: 11 }}>&rarr;</span>
        <span
          style={{
            display: "inline-block",
            padding: "1px 8px",
            borderRadius: 9999,
            fontSize: 11,
            fontWeight: 600,
            letterSpacing: "0.02em",
            background: toColors.bg,
            color: toColors.text,
          }}
        >
          {to}
        </span>
      </span>
    );

    lastIndex = m.index + m[0].length;
  }

  if (lastIndex < text.length) {
    parts.push(text.slice(lastIndex));
  }

  if (parts.length === 1 && typeof parts[0] === "string") {
    return [];
  }

  return parts;
}

// ── Sub-Components ─────────────────────────────────────────────────────────────

function TypeBadge({ type, color }: { type: CommentType; color: string }) {
  if (type === "message") return null;

  return (
    <span
      style={{
        display: "inline-block",
        padding: "2px 8px",
        borderRadius: 9999,
        fontSize: 10,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: color,
        background: `${color}1a`,
        border: `1px solid ${color}33`,
      }}
    >
      {TYPE_LABELS[type]}
    </span>
  );
}

function SectionLabel({ label }: { label: string }) {
  return (
    <div
      style={{
        fontSize: 10,
        fontWeight: 700,
        textTransform: "uppercase",
        letterSpacing: "0.06em",
        color: C.accent,
        marginBottom: 4,
      }}
    >
      {label}
    </div>
  );
}

function SectionContent({ content }: { content: string }) {
  const pills = renderContentWithStatusPills(content);
  if (pills.length > 0) {
    return (
      <div
        style={{
          fontSize: 13,
          color: C.textSecondary,
          lineHeight: 1.65,
        }}
      >
        {pills}
      </div>
    );
  }

  return (
    <div className="prose-comment">
      <ReactMarkdown>{content}</ReactMarkdown>
    </div>
  );
}

function ChecklistSection({ content }: { content: string }) {
  const lines = content.split("\n");

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
      {lines.map((line, i) => {
        const checkMatch = line.match(/^\s*-\s*\[(x| )\]\s*(.*)/i);
        if (!checkMatch) {
          if (line.trim()) {
            return (
              <div key={i} className="prose-comment">
                <ReactMarkdown>{line}</ReactMarkdown>
              </div>
            );
          }
          return null;
        }

        const checked = checkMatch[1].toLowerCase() === "x";
        const text = checkMatch[2];

        return (
          <div
            key={i}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 8,
              fontSize: 13,
              color: checked ? C.textSecondary : C.textSecondary,
              lineHeight: 1.5,
            }}
          >
            <span
              style={{
                display: "inline-flex",
                alignItems: "center",
                justifyContent: "center",
                width: 16,
                height: 16,
                marginTop: 2,
                borderRadius: 4,
                flexShrink: 0,
                border: checked
                  ? `1.5px solid ${C.online}`
                  : "1.5px solid rgba(255, 255, 255, 0.2)",
                background: checked
                  ? `${C.online}26`
                  : "transparent",
                color: C.online,
                fontSize: 10,
                fontWeight: 700,
              }}
            >
              {checked ? "✓" : ""}
            </span>
            <span
              style={{
                textDecoration: checked ? "line-through" : "none",
                opacity: checked ? 0.7 : 1,
              }}
            >
              {text}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function StructuredBody({ parsed }: { parsed: ParsedComment }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 14 }}>
      {parsed.sections.map((section, i) => {
        const hasChecklist = /^\s*-\s*\[(x| )\]/im.test(section.content);

        return (
          <div key={i}>
            {section.label !== "Intro" && (
              <SectionLabel label={section.label} />
            )}
            {hasChecklist ? (
              <ChecklistSection content={section.content} />
            ) : (
              <SectionContent content={section.content} />
            )}
          </div>
        );
      })}
    </div>
  );
}

// ── Main Component ─────────────────────────────────────────────────────────────

export function CommentCard({ comment, agentMap }: CommentCardProps) {
  const type = resolveType(comment);
  const color = TYPE_COLORS[type];
  const author = resolveAuthor(comment, agentMap);
  const parsed = parseComment(comment.content, comment.comment_type);
  const isReflection = type === "reflection";
  const isWaitingCallback = type === "waiting_on_callback";
  const isSystem = author.kind === "system";

  // Background tint for special types
  let bgTint = "rgba(255, 255, 255, 0.03)";
  if (type === "blocker") bgTint = `${C.error}08`;
  if (type === "resolution") bgTint = `${C.online}05`;
  if (isReflection) bgTint = `${C.info}0A`;
  if (isWaitingCallback) bgTint = `${C.warning}0A`;
  if (isSystem) bgTint = `${C.textSecondary}0D`;

  const leftBorderColor = isSystem ? C.textMuted : color;
  const leftBorderStyle = isSystem ? "dashed" : "solid";

  return (
    <div
      style={{
        position: "relative",
        background: bgTint,
        borderRadius: 12,
        border: `1px solid ${C.border}`,
        borderLeft:
          leftBorderColor !== "transparent"
            ? `3px ${leftBorderStyle} ${leftBorderColor}`
            : `1px solid ${C.border}`,
        overflow: "hidden",
        opacity: isSystem ? 0.92 : 1,
      }}
    >
      {/* Top highlight line */}
      <div
        style={{
          position: "absolute",
          top: 0,
          left: 0,
          right: 0,
          height: 1,
          background: "linear-gradient(90deg, transparent, rgba(255, 255, 255, 0.06) 30%, rgba(255, 255, 255, 0.1) 50%, rgba(255, 255, 255, 0.06) 70%, transparent)",
          pointerEvents: "none",
        }}
      />

      {/* Header */}
      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 8,
          padding: "10px 14px",
          borderBottom: `1px solid ${C.borderSubtle}`,
        }}
      >
        <TypeBadge type={type} color={color} />
        {isSystem && (
          <span
            style={{
              display: "inline-block",
              padding: "2px 8px",
              borderRadius: 9999,
              fontSize: 10,
              fontWeight: 700,
              textTransform: "uppercase",
              letterSpacing: "0.06em",
              color: C.textSecondary,
              background: `${C.textSecondary}1F`,
              border: `1px solid ${C.textSecondary}40`,
            }}
            title="Automatisch vom System generiert (z.B. Auto-Draft Fallback). Nicht vom Agent verfasst."
          >
            Auto
          </span>
        )}

        <span
          style={{
            fontSize: 12,
            fontWeight: 500,
            color:
              author.kind === "user"
                ? C.accent
                : author.kind === "system"
                  ? C.textSecondary
                  : C.textPrimary,
            fontStyle: author.kind === "system" ? "italic" : "normal",
          }}
        >
          {author.kind === "user"
            ? `👤 ${author.label}`
            : author.label}
        </span>

        <span
          style={{
            marginLeft: "auto",
            fontSize: 11,
            color: C.textMuted,
            flexShrink: 0,
          }}
        >
          {timeAgo(comment.created_at)}
        </span>
      </div>

      {/* Body */}
      <div style={{ padding: "12px 14px" }}>
        {parsed.type === "structured" ? (
          <StructuredBody parsed={parsed} />
        ) : (
          <div className="prose-comment">
            <ReactMarkdown>{comment.content}</ReactMarkdown>
          </div>
        )}
      </div>
    </div>
  );
}

export default CommentCard;
