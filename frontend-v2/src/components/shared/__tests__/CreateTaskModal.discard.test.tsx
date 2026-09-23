/**
 * CreateTaskModal — Esc / close must not silently throw a typed draft away.
 *
 * Before: every Esc anywhere in the modal (even one meant to close the
 * project dropdown), every backdrop click, X and Cancel called resetForm().
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { CreateTaskModal } from "../CreateTaskModal";
import { api } from "@/lib/api";

function renderModal() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CreateTaskModal activeBoardId="board-1" agents={[]} />
    </QueryClientProvider>
  );
}

const formDialog = () => screen.queryByRole("dialog", { name: "New task" });
const discardDialog = () => screen.queryByRole("dialog", { name: "Discard draft?" });

async function openAndType(title?: string) {
  await userEvent.click(screen.getByRole("button", { name: "New task" }));
  const input = await screen.findByRole("textbox", { name: /Titel/ });
  if (title) await userEvent.type(input, title);
  return input;
}

describe("CreateTaskModal — discard draft", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response("[]", { status: 200, headers: { "Content-Type": "application/json" } })
    );
    vi.spyOn(api.projects, "list").mockResolvedValue([]);
    vi.spyOn(api.credentials, "list").mockResolvedValue([]);
    vi.spyOn(api.repos, "list").mockResolvedValue([]);
  });

  it("an empty form still closes on Esc right away", async () => {
    renderModal();
    await openAndType();
    await userEvent.keyboard("{Escape}");
    await waitFor(() => expect(formDialog()).not.toBeInTheDocument());
    expect(discardDialog()).not.toBeInTheDocument();
  });

  it("Esc with a typed title asks before discarding; Keep editing keeps the text", async () => {
    renderModal();
    const input = await openAndType("Write the release notes");
    await userEvent.keyboard("{Escape}");

    const dialog = await screen.findByRole("dialog", { name: "Discard draft?" });
    expect(formDialog()).toBeInTheDocument();
    await userEvent.click(within(dialog).getByRole("button", { name: "Keep editing" }));

    await waitFor(() => expect(discardDialog()).not.toBeInTheDocument());
    expect(formDialog()).toBeInTheDocument();
    expect(input).toHaveValue("Write the release notes");
  });

  it("Discard closes the form and clears it", async () => {
    renderModal();
    await openAndType("Write the release notes");
    await userEvent.click(screen.getAllByRole("button", { name: "Cancel" })[0]);
    const dialog = await screen.findByRole("dialog", { name: "Discard draft?" });
    await userEvent.click(within(dialog).getByRole("button", { name: "Discard" }));
    await waitFor(() => expect(formDialog()).not.toBeInTheDocument());

    const input = await openAndType();
    expect(input).toHaveValue("");
  });

  it("Esc inside the open project dropdown closes only the dropdown", async () => {
    renderModal();
    await openAndType("Write the release notes");
    await userEvent.click(screen.getByRole("button", { name: /Project \(optional\)/ }));
    const search = await screen.findByPlaceholderText("Search projects...");
    search.focus();
    await userEvent.keyboard("{Escape}");

    // Let any exit animation finish — a closing modal lingers for ~220 ms.
    await new Promise((r) => setTimeout(r, 400));
    expect(discardDialog()).not.toBeInTheDocument();
    expect(formDialog()).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: /Titel/ })).toHaveValue("Write the release notes");
    await waitFor(() => expect(screen.queryByPlaceholderText("Search projects...")).not.toBeInTheDocument());
  });
});
