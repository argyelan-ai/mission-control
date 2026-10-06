"use client";

/**
 * HeadChatRow — one row in the Chats list' "Heads" section (bauplan
 * `heads-sichtbar` PR 2 §3.2), in the same cut as the existing agent row
 * (`SessionSidebar`) and `GroupRow`: a status dot, the title, a second
 * line that carries the pair and a single time fact. Never a second
 * dimension in that line beyond one `·` (K15 proposal, anhang.md H — "ein
 * Zustand, zwei Worte" was a review finding against the prototype).
 */
import { useLocale, useTranslations } from "next-intl";
import { ChevronRight } from "lucide-react";
import { C } from "@/lib/colors";
import { StatusDot } from "@/components/shared/StatusDot";
import { formatAgeRounded } from "@/lib/taskDetail/format";
import { headContextLine, headListTitle, midLineStateWord, pairShort, type HeadRun } from "@/lib/heads";

/** Dot per state (bauplan §3.2): needs_you = accent (the one thing asking
 *  for the operator), running/starting = the SAME busy pulse an agent row
 *  shows while it works, passed = online, failed = error, stopped = idle.
 *  `needs_you` has no `StatusDot` status of its own (that palette is
 *  status/online/error/idle/busy/offline, not "accent") — a small bespoke
 *  dot, same pattern `GroupRow` already uses for its own "waiting" dot.
 *  `superseded` overrides a `needs_you` run back to the plain idle dot — a
 *  later run on the same task already answered or replaced it, so nothing
 *  here is actually asking for the operator any more (review finding on
 *  PR #756 round 4). */
function HeadDot({ state, superseded }: { state: HeadRun["state"]; superseded: boolean }) {
  if (state === "needs_you" && !superseded) {
    return (
      <span
        className="relative inline-flex shrink-0 h-1.5 w-1.5 rounded-full animate-pulse"
        style={{ background: C.accent }}
        aria-hidden="true"
      />
    );
  }
  if (state === "needs_you") return <StatusDot status="idle" size="sm" />;
  const map: Record<Exclude<HeadRun["state"], "needs_you">, { status: "busy" | "online" | "error" | "idle"; pulse?: boolean }> = {
    starting: { status: "busy", pulse: true },
    running: { status: "busy", pulse: true },
    passed: { status: "online" },
    failed: { status: "error" },
    stopped: { status: "idle" },
  };
  const { status, pulse } = map[state];
  return <StatusDot status={status} size="sm" pulse={pulse} />;
}

interface HeadChatRowProps {
  run: HeadRun;
  selected: boolean;
  onSelect: (runId: string) => void;
  /** Rail = dense desktop column. List = the mobile stack screen. */
  variant?: "rail" | "list";
  /** This run's `needs_you` question has been superseded by a later run on
   *  the same task (`lib/heads.ts::supersededNeedsYouIds`) — render it like
   *  an ended, answered run instead of the pulsing "needs you" row (review
   *  finding on PR #756 round 4: an answered `needs_you` stayed pinned at
   *  the top of the Heads section forever). Ignored for every other state. */
  superseded?: boolean;
}

export function HeadChatRow({ run, selected, onSelect, variant = "rail", superseded = false }: HeadChatRowProps) {
  const t = useTranslations("heads");
  const locale = useLocale();
  const stack = variant === "list";
  const supersededNeedsYou = run.state === "needs_you" && superseded;

  // The pair + exactly ONE state/time fact — never a second word for the
  // same state on top of it (the prototype review's actual finding,
  // anhang.md H: "braucht dich · Frage offen" is ONE state in two words).
  // Dropping the pair entirely (as an earlier revision did, citing K15) was
  // an over-correction: K15 itself is "nicht gesetzt" (anhang.md H — a
  // proposal, not an adopted rule), and without the pair there is no way to
  // tell, from this row alone, which harness the waiting/ended head ran on.
  // `headContextLine` (lib/heads.ts, shared with the task list and Inbox
  // rows, PR 3) is this exact rule; the superseded branch alone still needs
  // its own text ("answered" is not one of `headContextLine`'s states).
  let line2: string;
  if (supersededNeedsYou) {
    const pair = pairShort(run);
    const age = formatAgeRounded(run.exited_at ?? run.created_at, locale);
    const stateWord = midLineStateWord(t("answered"));
    line2 = age ? `${pair} · ${stateWord} · ${t("time.ago", { age })}` : `${pair} · ${stateWord}`;
  } else {
    line2 = headContextLine(run, t, locale);
  }

  return (
    <button
      type="button"
      role="option"
      aria-selected={selected}
      data-testid="head-chat-row"
      data-head-state={run.state}
      onClick={() => onSelect(run.run_id)}
      className={`flex items-center gap-2 text-left w-full transition-colors cursor-pointer ${
        stack ? "px-4 min-h-[52px] py-2" : "px-3 py-2 rounded-sm"
      }`}
      style={{
        background: selected ? C.accentSubtle : "transparent",
        borderTop: stack ? `1px solid ${C.borderSubtle}` : undefined,
      }}
    >
      <HeadDot state={run.state} superseded={supersededNeedsYou} />
      <span className="flex-1 min-w-0">
        <span
          className="block font-medium truncate text-sm"
          style={{ color: selected ? C.textPrimary : C.textSecondary }}
        >
          {headListTitle(run)}
        </span>
        <span className={`block truncate text-xs ${stack ? "mt-1" : ""}`} style={{ color: C.textMuted }}>
          {line2}
        </span>
      </span>
      {stack && <ChevronRight size={15} className="shrink-0" style={{ color: C.textMuted }} aria-hidden="true" />}
    </button>
  );
}
