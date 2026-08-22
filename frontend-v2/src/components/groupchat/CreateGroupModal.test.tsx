/**
 * CreateGroupModal — vitest.
 *
 * Prüft das Versprechen des Dialogs: genau zwei Pflichtangaben (Ziel und
 * mindestens 2 Mitglieder), ein harter Deckel bei 6, und ein Fehlschlag beim
 * Anlegen wirft Marks Eingaben nicht weg.
 *
 * Die Labels sind englisch, weil src/test-setup.ts next-intl gegen
 * messages/en.json auflöst.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { CreateGroupModal } from "./CreateGroupModal";
import { api } from "@/lib/api";
import type { EligibleMember, GroupDetail } from "@/lib/groupTypes";

vi.mock("@/lib/api", () => ({
  api: {
    groups: {
      eligibleMembers: vi.fn(),
      create: vi.fn(),
    },
  },
}));

const NAMES = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf"];

const MEMBERS: EligibleMember[] = NAMES.map((name, i) => ({
  id: `agent-${i + 1}`,
  name,
  slug: name.toLowerCase(),
  emoji: "🤖",
}));

const CREATED = { id: "group-1", name: "Alpha vs Bravo" } as unknown as GroupDetail;

const eligibleMembers = vi.mocked(api.groups.eligibleMembers);
const create = vi.mocked(api.groups.create);

function setup() {
  const onClose = vi.fn();
  const onCreated = vi.fn();
  render(<CreateGroupModal open onClose={onClose} onCreated={onCreated} />);
  // `delay: null` tippt ohne Kunstpausen zwischen den Anschlägen. Mit der
  // Voreinstellung brauchte diese Datei ~50s und lief in der vollen Suite
  // (parallele Worker) ins 5s-Zeitlimit — die Prüfungen blieben dieselben,
  // nur das Warten war echt.
  return { user: userEvent.setup({ delay: null }), onClose, onCreated };
}

/** Wartet, bis die Kandidatenliste geladen ist. */
async function chips() {
  await screen.findByRole("checkbox", { name: "Alpha" });
}

const submitButton = () => screen.getByRole("button", { name: "Create group" });
const goalField = () => screen.getByLabelText("Goal");

beforeEach(() => {
  vi.clearAllMocks();
  eligibleMembers.mockResolvedValue(MEMBERS);
  create.mockResolvedValue(CREATED);
});

describe("CreateGroupModal", () => {
  it("keeps submit disabled while the goal is empty, even with enough members", async () => {
    const { user } = setup();
    await chips();

    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));
    await user.click(screen.getByRole("checkbox", { name: "Bravo" }));

    expect(submitButton()).toBeDisabled();
  });

  it("keeps submit disabled with a goal but only one member", async () => {
    const { user } = setup();
    await chips();

    await user.type(goalField(), "Compare DFlash2 and vLLM");
    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));

    expect(submitButton()).toBeDisabled();
  });

  it("enables submit once a goal and two members are there", async () => {
    const { user } = setup();
    await chips();

    await user.type(goalField(), "Compare DFlash2 and vLLM");
    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));
    await user.click(screen.getByRole("checkbox", { name: "Bravo" }));

    expect(submitButton()).toBeEnabled();
  });

  it("refuses a seventh member and keeps the counter at the cap", async () => {
    const { user } = setup();
    await chips();

    for (const name of NAMES) {
      await user.click(screen.getByRole("checkbox", { name }));
    }

    expect(screen.getByRole("checkbox", { name: "Golf" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getAllByRole("checkbox").filter((c) => c.getAttribute("aria-checked") === "true"))
      .toHaveLength(6);
    expect(screen.getByText("Members (6/6)")).toBeInTheDocument();
  });

  it("creates the group with goal and member ids, then reports it upwards", async () => {
    const { user, onCreated, onClose } = setup();
    await chips();

    await user.type(goalField(), "Compare DFlash2 and vLLM");
    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));
    await user.click(screen.getByRole("checkbox", { name: "Bravo" }));
    await user.click(submitButton());

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith(
      expect.objectContaining({
        goal: "Compare DFlash2 and vLLM",
        member_ids: ["agent-1", "agent-2"],
        lifecycle: "one_shot",
        max_rounds: 3,
      }),
    );
    await waitFor(() => expect(onCreated).toHaveBeenCalledWith(CREATED));
    expect(onClose).toHaveBeenCalled();
  });

  it("shows the failure line and keeps the typed goal when creating fails", async () => {
    create.mockRejectedValue(new Error("boom"));
    const { user, onClose, onCreated } = setup();
    await chips();

    await user.type(goalField(), "Compare DFlash2 and vLLM");
    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));
    await user.click(screen.getByRole("checkbox", { name: "Bravo" }));
    await user.click(submitButton());

    expect(await screen.findByText("Could not create the group")).toBeInTheDocument();
    expect(onClose).not.toHaveBeenCalled();
    expect(onCreated).not.toHaveBeenCalled();
    expect(goalField()).toHaveValue("Compare DFlash2 and vLLM");
  });

  it("hides the advanced fields until the disclosure is opened", async () => {
    const { user } = setup();
    await chips();

    const disclosure = screen.getByRole("button", { name: /Advanced/ });
    expect(disclosure).toHaveAttribute("aria-expanded", "false");
    expect(screen.queryByLabelText("Max. rounds")).not.toBeInTheDocument();

    await user.click(disclosure);

    expect(disclosure).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByLabelText("Max. rounds")).toBeInTheDocument();
  });

  it("sends the max_rounds value edited in the advanced section", async () => {
    const { user } = setup();
    await chips();

    await user.type(goalField(), "Compare DFlash2 and vLLM");
    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));
    await user.click(screen.getByRole("checkbox", { name: "Bravo" }));

    await user.click(screen.getByRole("button", { name: /Advanced/ }));
    const rounds = screen.getByLabelText("Max. rounds");
    await user.clear(rounds);
    await user.type(rounds, "5");

    await user.click(submitButton());

    await waitFor(() => expect(create).toHaveBeenCalledTimes(1));
    expect(create).toHaveBeenCalledWith(expect.objectContaining({ max_rounds: 5 }));
  });

  it("offers only the selected members as lead", async () => {
    const { user } = setup();
    await chips();

    await user.click(screen.getByRole("checkbox", { name: "Alpha" }));
    await user.click(screen.getByRole("checkbox", { name: "Charlie" }));
    await user.click(screen.getByRole("button", { name: /Advanced/ }));

    const lead = screen.getByLabelText("Lead — judges and writes the result");
    const options = Array.from(lead.querySelectorAll("option")).map((o) => o.textContent);
    expect(options).toEqual(["—", "Alpha", "Charlie"]);
  });

  it("focuses the goal field when the dialog opens", async () => {
    setup();
    await chips();

    expect(goalField()).toHaveFocus();
  });
});
