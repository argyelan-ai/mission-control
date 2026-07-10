import { describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReviewStep } from "../steps/ReviewStep";
import { initialWizardState } from "../types";

const createMock = vi.fn(async (_data: Record<string, unknown>) => ({ id: "new-1", token: "tok-xyz" }));
const healthMock = vi.fn(async () => ({ provision_status: "provisioned", runtime: "cli-bridge", ready: true, checks: [{ label: "provisioned", ok: true, detail: "ok" }] }));

vi.mock("@/lib/api", () => ({
  api: {
    agents: {
      create: (data: Record<string, unknown>) => createMock(data),
      provision: vi.fn(),
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
});
