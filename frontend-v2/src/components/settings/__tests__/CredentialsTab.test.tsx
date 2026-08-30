import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CredentialsTab } from "../CredentialsTab";
import { api } from "@/lib/api";
import { notify } from "@/lib/notify";
import type { Credential } from "@/lib/types";

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn() },
}));

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

const makeCredential = (over: Partial<Credential> = {}): Credential => ({
  id: "cred-1",
  name: "GX10 SSH key",
  credential_type: "ssh_key",
  data_masked: { username: "mcfleet", public_key: "ssh-ed25519 AAAA", private_key_pem: "[hidden]" },
  url: null,
  notes: null,
  created_at: "2026-08-30T00:00:00Z",
  updated_at: "2026-08-30T00:00:00Z",
  ...over,
});

describe("CredentialsTab — ssh_key protection (review finding #2, 30.08.2026)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it("hides the Edit action for ssh_key credentials", async () => {
    vi.spyOn(api.credentials, "list").mockResolvedValue([makeCredential()]);

    renderWithQuery(<CredentialsTab />);

    expect(await screen.findByText("GX10 SSH key")).toBeInTheDocument();
    expect(screen.queryByLabelText("Edit credential")).toBeNull();
    // Delete stays available — revoking a compromised key is legitimate.
    expect(screen.getByLabelText("Delete credential")).toBeInTheDocument();
  });

  it("still shows Edit for ordinary credential types", async () => {
    vi.spyOn(api.credentials, "list").mockResolvedValue([
      { ...makeCredential(), credential_type: "login", data_masked: { username: "mark", password: "****abcd" } },
    ]);

    renderWithQuery(<CredentialsTab />);

    expect(await screen.findByLabelText("Edit credential")).toBeInTheDocument();
  });

  it("shows a safe, non-crashing summary line for ssh_key rows", async () => {
    vi.spyOn(api.credentials, "list").mockResolvedValue([makeCredential()]);

    renderWithQuery(<CredentialsTab />);

    expect(await screen.findByText(/mcfleet/)).toBeInTheDocument();
    expect(screen.queryByText(/\[hidden\]/)).toBeNull();
  });
});
