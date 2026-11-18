/**
 * Shell review A3: every chat header in the shell has the same left control —
 * the drawer icon. A head's header had a back chevron while an agent chat had
 * the drawer icon (two navigation models on one surface).
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";

vi.mock("next/navigation", () => ({
  usePathname: () => "/sessions",
  useRouter: () => ({ replace: vi.fn(), push: vi.fn() }),
  useSearchParams: () => new URLSearchParams(),
}));

import { MobileNavProvider } from "@/components/layout/MobileNav";
import { HeadChatHeader } from "../HeadChatHeader";
import type { HeadRun } from "@/lib/heads";

const RUN: HeadRun = {
  run_id: "r1", task_id: "t1", title: "Fix login", harness: "claude", runtime_slug: "anthropic",
  model: null, repo_full_name: null, branch: null, mode: "fresh", restarted_from: null, box_keys: [],
  created_at: "2026-11-18T10:00:00Z", started_at: "2026-11-18T10:00:00Z", exited_at: null, state: "running",
  reason: null, silent_s: null, heartbeat_stale: false, step: null, question: null, pr_url: null, tmux: null,
  run_record: false, task_deleted: false,
};

describe("HeadChatHeader in the shell", () => {
  it("shows the drawer icon instead of the back chevron", () => {
    render(
      <MobileNavProvider>
        <HeadChatHeader head={RUN} onBack={vi.fn()} />
      </MobileNavProvider>,
    );
    expect(screen.getByTestId("drawer-button").className).toContain("md:hidden");
    expect(screen.queryByTestId("head-chat-back")).toBeNull();
  });

  it("keeps the back chevron outside the shell", () => {
    render(<HeadChatHeader head={RUN} onBack={vi.fn()} />);
    expect(screen.getByTestId("head-chat-back")).toBeInTheDocument();
    expect(screen.queryByTestId("drawer-button")).toBeNull();
  });
});
