import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReviewStep } from "../steps/ReviewStep";
import { initialWizardState } from "../types";

const createMock = vi.fn(async (_data: Record<string, unknown>) => ({ id: "new-1", token: "tok-xyz" }));
const healthMock = vi.fn(async () => ({ provision_status: "provisioned", runtime: "cli-bridge", ready: true, checks: [{ label: "provisioned", ok: true, detail: "ok" }] }));
const provisionMock = vi.fn(async (_id: string): Promise<{ status: string; token?: string }> => ({ status: "provisioning" }));

vi.mock("@/lib/api", () => ({
  api: {
    agents: {
      create: (data: Record<string, unknown>) => createMock(data),
      provision: (id: string) => provisionMock(id),
      healthCheck: () => healthMock(),
    },
  },
}));
vi.mock("@/lib/notify", () => ({ notify: { success: vi.fn(), error: vi.fn() } }));

function wrap(ui: React.ReactNode) {
  const qc = new QueryClient();
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ReviewStep", () => {
  it("creating an agent posts the assembled payload and shows the token", async () => {
    const state = { ...initialWizardState(null), name: "Nova", scopes: ["tasks:read"], harness: "openclaude" as const };
    wrap(<ReviewStep state={state} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} onCreated={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Agent erstellen/ }));
    await waitFor(() => expect(createMock).toHaveBeenCalled());
    const payload = createMock.mock.calls[0][0];
    expect(payload.name).toBe("Nova");
    expect(payload.scopes).toEqual(["tasks:read"]);
    expect(payload.harness).toBe("openclaude");
    await waitFor(() => expect(screen.getByText("tok-xyz")).toBeTruthy());
  });

  it("shows the grok login hint before creating a grok host agent", async () => {
    const state = { ...initialWizardState(null), name: "Grok", agentRuntime: "host" as const, harness: "grok" as const, runtimeId: "grok-cloud" };
    wrap(<ReviewStep state={state} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} onCreated={() => {}} />);
    expect(screen.getByText(/grok login --device-auth/)).toBeTruthy();
    // Harness surfaces in the review summary.
    expect(screen.getByText("grok")).toBeTruthy();
  });

  it("posts harness=grok for a grok host agent", async () => {
    const state = { ...initialWizardState(null), name: "Grok", agentRuntime: "host" as const, harness: "grok" as const, runtimeId: "grok-cloud" };
    wrap(<ReviewStep state={state} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} onCreated={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Agent erstellen/ }));
    await waitFor(() => expect(createMock).toHaveBeenCalled());
    // createMock is module-shared across tests → assert THIS test's call, not calls[0].
    const payload = createMock.mock.lastCall![0];
    expect(payload.harness).toBe("grok");
    expect(payload.agent_runtime).toBe("host");
    expect(payload.runtime_id).toBe("grok-cloud");
  });

  it("shows the rotated token from host provisioning, not the stale create-time token", async () => {
    provisionMock.mockResolvedValueOnce({ status: "provisioning", token: "rotated" });
    const state = { ...initialWizardState(null), name: "Nova Host", agentRuntime: "host" as const, harness: "openclaude" as const };
    wrap(<ReviewStep state={state} update={() => {}} boards={[]} goNext={() => {}} goBack={() => {}} onCreated={() => {}} />);
    fireEvent.click(screen.getByRole("button", { name: /Agent erstellen/ }));
    await waitFor(() => expect(provisionMock).toHaveBeenCalledWith("new-1"));
    await waitFor(() => expect(screen.getByText("rotated")).toBeTruthy());
    expect(screen.queryByText("tok-xyz")).toBeNull();
  });
});
