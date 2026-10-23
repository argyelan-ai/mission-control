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
  name: "box-c SSH key",
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

    expect(await screen.findByText("box-c SSH key")).toBeInTheDocument();
    expect(screen.queryByLabelText("Edit credential")).toBeNull();
    // Delete stays available — revoking a compromised key is legitimate.
    expect(screen.getByLabelText("Delete credential")).toBeInTheDocument();
  });

  it("still shows Edit for ordinary credential types", async () => {
    vi.spyOn(api.credentials, "list").mockResolvedValue([
      { ...makeCredential(), credential_type: "login", data_masked: { username: "mark", password: "••••••••" } },
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

  it("summarises free text without a character count (the mask has a fixed length)", async () => {
    vi.spyOn(api.credentials, "list").mockResolvedValue([
      { ...makeCredential(), name: "notes", credential_type: "custom", data_masked: { content: "••••••••" } },
    ]);

    renderWithQuery(<CredentialsTab />);

    expect(await screen.findByText(/\(Free text\)/)).toBeInTheDocument();
    expect(screen.queryByText(/characters/)).toBeNull();
  });
});

// Round-4 K5 (r2 S4): on the phone Edit and Delete were 24×24 px side by
// side (a mistap hits Delete), the type mark was 9 px, and "Token" was
// tinted ochre — the warning colour used as a category.
describe("CredentialsTab — phone targets and a neutral type mark (round-4 K5)", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(api.credentials, "list").mockResolvedValue([
      { ...makeCredential(), id: "c-tok", name: "Example token", credential_type: "token", data_masked: { token: "••••••••" } },
      { ...makeCredential(), id: "c-log", name: "Example login", credential_type: "login", data_masked: { username: "demo", password: "••••••••" } },
    ]);
  });

  const isTouchSized = (el: HTMLElement) =>
    /\bmin-h-touch\b/.test(el.className) && /\bmin-w-touch\b/.test(el.className);

  it("gives Edit and Delete a 44 px hit area", async () => {
    renderWithQuery(<CredentialsTab />);
    await screen.findByText("Example token");
    for (const el of [...screen.getAllByLabelText("Edit credential"), ...screen.getAllByLabelText("Delete credential")]) {
      expect(isTouchSized(el as HTMLElement), el.outerHTML).toBe(true);
    }
  });

  it("keeps the delete confirmation, also at 44 px", async () => {
    const user = userEvent.setup();
    vi.spyOn(api.credentials, "delete").mockResolvedValue(undefined as never);
    renderWithQuery(<CredentialsTab />);
    await screen.findByText("Example token");
    await user.click(screen.getAllByLabelText("Delete credential")[0]);
    expect(api.credentials.delete).not.toHaveBeenCalled();
    const confirm = screen.getByRole("button", { name: /really/i });
    expect(/\bmin-h-touch\b/.test(confirm.className)).toBe(true);
  });

  it("shows the type mark on the type scale, in a neutral tone", async () => {
    renderWithQuery(<CredentialsTab />);
    await screen.findByText("Example token");
    for (const label of ["Token", "Login"]) {
      const mark = screen.getByText(label, { selector: "span" });
      expect(mark.className).not.toMatch(/text-\[(9|10)px\]/);
      expect(mark.className).toMatch(/\btext-xs\b/);
      expect(mark.getAttribute("style") ?? "").not.toMatch(/status-warning|--color-accent/);
    }
    const html = document.body.innerHTML;
    expect(html).not.toMatch(/status-warning/);
  });
});

it("names a host-onboarding key an SSH key, not free text (round-4 K5)", async () => {
  vi.restoreAllMocks();
  vi.spyOn(api.credentials, "list").mockResolvedValue([makeCredential()]);
  renderWithQuery(<CredentialsTab />);
  expect(await screen.findByText("SSH key", { selector: "span" })).toBeInTheDocument();
  expect(screen.queryByText("Free text", { selector: "span" })).toBeNull();
});
