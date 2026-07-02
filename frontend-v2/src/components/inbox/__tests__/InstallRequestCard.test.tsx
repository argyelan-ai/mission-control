import { describe, it, expect } from "vitest";
import { render, screen } from "@testing-library/react";
import { InstallRequestCard } from "../InstallRequestCard";
import type { Approval } from "@/lib/types";

const mkApproval = (overrides: Partial<Approval> = {}): Approval => ({
  id: "a1",
  board_id: "b1",
  agent_id: "requester",
  action_type: "install_skill",
  description: "Install web-performance",
  status: "pending",
  created_at: new Date().toISOString(),
  resolved_at: null,
  resolver_note: null,
  failure_reason: null,
  expires_at: null,
  confidence: null,
  autonomy_level: "L2",
  task_id: null,
  payload: {
    name: "web-performance",
    source: "github:anthropic/skill-web-performance",
    target_agent_id: "t1",
    target_agent_slug: "spark",
    requester_agent_id: "r1",
    requester_agent_slug: "boss-host",
    reason: "Agent failed 3 perf-debug tasks",
    proposed_config: null,
  },
  ...overrides,
} as Approval);

describe("InstallRequestCard", () => {
  it("renders target agent and source", () => {
    render(<InstallRequestCard approval={mkApproval()} onResolve={() => {}} />);
    expect(screen.getByText(/web-performance/i)).toBeInTheDocument();
    expect(screen.getByText(/spark/i)).toBeInTheDocument();
    expect(screen.getByText(/github:anthropic/i)).toBeInTheDocument();
    expect(screen.getByText(/3 perf-debug/i)).toBeInTheDocument();
  });

  it("shows install label for install_skill action", () => {
    render(<InstallRequestCard approval={mkApproval()} onResolve={() => {}} />);
    const btns = screen.getAllByRole("button");
    const approveBtn = btns.find(b => /install/i.test(b.textContent || ""));
    expect(approveBtn).toBeDefined();
  });

  it("shows uninstall label for uninstall_plugin action", () => {
    render(<InstallRequestCard
      approval={mkApproval({ action_type: "uninstall_plugin" as const })}
      onResolve={() => {}}
    />);
    const btns = screen.getAllByRole("button");
    const approveBtn = btns.find(b => /uninstall/i.test(b.textContent || ""));
    expect(approveBtn).toBeDefined();
  });
});
