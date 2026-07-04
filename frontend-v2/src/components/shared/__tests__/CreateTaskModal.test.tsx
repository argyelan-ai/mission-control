/**
 * CreateTaskModal — Repo-Default (Mark, 04.07.).
 *
 * Der Ad-hoc-Task (kein Projekt gewählt) muss standardmässig OHNE separates
 * Repo starten — "Eigenes Repo" ist Opt-in, nicht vorausgewählt. Der Default
 * muss auch nach mehrfachem Öffnen/Schliessen des Modals stabil bleiben.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateTaskModal } from "../CreateTaskModal";
import { api } from "@/lib/api";

function renderWithQuery(ui: React.ReactElement) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("CreateTaskModal — Repo-Default", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    // Generic fetch stub for any unmocked call.
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
  });

  it("defaults to 'kein eigenes Repo' with no project preselected", async () => {
    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);

    await userEvent.click(screen.getByRole("button", { name: "Neuer Auftrag" }));

    const checkbox = (await screen.findByRole("checkbox", { name: /Eigenes Repo/ })) as HTMLInputElement;
    expect(checkbox.checked).toBe(false);
  });

  it("keeps the ad-hoc default after cancelling and reopening the modal", async () => {
    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);

    await userEvent.click(screen.getByRole("button", { name: "Neuer Auftrag" }));
    const firstCheckbox = (await screen.findByRole("checkbox", { name: /Eigenes Repo/ })) as HTMLInputElement;
    expect(firstCheckbox.checked).toBe(false);

    // Flip it on, then close without submitting.
    await userEvent.click(firstCheckbox);
    expect(firstCheckbox.checked).toBe(true);
    await userEvent.click(screen.getByRole("button", { name: "Schließen" }));

    // Reopen — the form must have reset to the ad-hoc default, not remember the toggle.
    await userEvent.click(screen.getByRole("button", { name: "Neuer Auftrag" }));
    const secondCheckbox = (await screen.findByRole("checkbox", { name: /Eigenes Repo/ })) as HTMLInputElement;
    expect(secondCheckbox.checked).toBe(false);
  });

  it("renders the Projekt section directly after title/description, before Zuweisung", async () => {
    renderWithQuery(<CreateTaskModal activeBoardId="board-1" agents={[]} />);

    await userEvent.click(screen.getByRole("button", { name: "Neuer Auftrag" }));
    await screen.findByRole("checkbox", { name: /Eigenes Repo/ });

    const headings = screen.getAllByText(/^(Projekt|Zuweisung|Ausführung)$/).map((el) => el.textContent);
    const projektIdx = headings.indexOf("Projekt");
    const zuweisungIdx = headings.indexOf("Zuweisung");
    expect(projektIdx).toBeGreaterThanOrEqual(0);
    expect(projektIdx).toBeLessThan(zuweisungIdx);
  });
});
