/**
 * Bench W1 P1b / W1.11 (package D): the switch confirmation carries
 * "Danach automatisch testen" — on by default, only for this one switch.
 * After a successful start the dialog schedules the test
 * (scheduleBenchAutoTest); a failure there never undoes the switch, it only
 * leaves a quiet hint. Off → nothing is scheduled.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { HostRecipeSwitcher } from "../HostRecipeSwitcher";
import { api } from "@/lib/api";
import type { HostRecipe } from "@/lib/types";

const schedule = vi.hoisted(() => vi.fn());
vi.mock("@/lib/benchLinks", async (orig) => ({
  ...(await orig<typeof import("@/lib/benchLinks")>()),
  scheduleBenchAutoTest: schedule,
}));

const RECIPE: HostRecipe = {
  slug: "glm-flash",
  display_name: "GLM-5.3-Flash-EXL3",
  engine: "vllm_docker",
  topology: { nodes: 1 },
  port: 8000,
  instance_runtime_id: null,
  running: false,
  startable: true,
  fit: "solo",
  reason: null,
  busy_hosts: [],
  candidate_workers: [],
};

function renderSwitcher() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <HostRecipeSwitcher hostId="spark" hostName="spark" servingName="DeepSeek-V4.1" />
    </QueryClientProvider>,
  );
}

async function pickRecipe() {
  renderSwitcher();
  const trigger = await screen.findByTestId("recipe-dropdown-trigger");
  await act(async () => trigger.click());
  const option = await screen.findByTestId("recipe-option-glm-flash");
  await act(async () => option.click());
  return screen.findByTestId("recipe-confirm");
}

describe("HostRecipeSwitcher — Danach automatisch testen", () => {
  beforeEach(() => {
    schedule.mockReset();
    vi.spyOn(api.hosts, "recipes").mockResolvedValue([RECIPE]);
  });
  afterEach(() => vi.restoreAllMocks());

  it("the confirmation shows the switch, on by default, with the sentence", async () => {
    await pickRecipe();
    const toggle = screen.getByRole("switch", { name: "Test automatically afterwards" });
    expect(toggle).toHaveAttribute("aria-checked", "true");
    expect(screen.getByTestId("recipe-autotest")).toHaveTextContent(
      "Standard test, 3 runs. Starts 15 min after loading. The result goes to the Inbox.",
    );
  });

  it("after a successful start the test is scheduled for this host + recipe", async () => {
    vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    schedule.mockResolvedValue({ test_id: "t-5", not_before: "2026-10-24T09:45:00Z" });
    await pickRecipe();
    await act(async () => screen.getByTestId("recipe-confirm-start").click());
    await waitFor(() => expect(schedule).toHaveBeenCalledWith("spark", "glm-flash"));
    expect(await screen.findByTestId("recipe-autotest-note")).toHaveTextContent(
      /^Test planned: starts at \d{1,2}:\d\d( [AP]M)? at the earliest\. The result goes to the Inbox\.$/,
    );
  });

  it("toggle off: nothing is scheduled", async () => {
    const start = vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    await pickRecipe();
    await act(async () => screen.getByRole("switch", { name: "Test automatically afterwards" }).click());
    expect(screen.getByRole("switch", { name: "Test automatically afterwards" })).toHaveAttribute("aria-checked", "false");
    expect(screen.getByTestId("recipe-autotest")).toHaveTextContent("No test starts after this switch.");
    await act(async () => screen.getByTestId("recipe-confirm-start").click());
    await waitFor(() => expect(start).toHaveBeenCalled());
    await new Promise((r) => setTimeout(r, 20));
    expect(schedule).not.toHaveBeenCalled();
    expect(screen.queryByTestId("recipe-autotest-note")).toBeNull();
  });

  it("a failed start schedules nothing", async () => {
    vi.spyOn(api.hosts, "startRecipe").mockRejectedValue(new Error('API 409: {"detail":"busy"}'));
    await pickRecipe();
    await act(async () => screen.getByTestId("recipe-confirm-start").click());
    await screen.findByTestId("recipe-start-error");
    expect(schedule).not.toHaveBeenCalled();
  });

  it("scheduling fails: the switch stays, only a quiet hint", async () => {
    vi.spyOn(api.hosts, "startRecipe").mockResolvedValue({ ok: true });
    schedule.mockRejectedValue(new Error("API 500: nope"));
    await pickRecipe();
    await act(async () => screen.getByTestId("recipe-confirm-start").click());
    const note = await screen.findByTestId("recipe-autotest-note");
    expect(note).toHaveTextContent("MC could not plan the test — the switch goes ahead anyway.");
    expect(note).not.toHaveAttribute("role", "alert");
    // the switch itself is still "starting …", no start error
    expect(screen.getByTestId("recipe-starting")).toBeInTheDocument();
    expect(screen.queryByTestId("recipe-start-error")).toBeNull();
  });
});
