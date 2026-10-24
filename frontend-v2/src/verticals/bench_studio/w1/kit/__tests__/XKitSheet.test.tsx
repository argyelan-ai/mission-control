import { beforeEach, describe, expect, it, vi } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { BenchTestDetail, XKit } from "../../types";
import {
  kitGreenFixture,
  kitRedFixture,
  testDoneGreenFixture,
  testDoneTruncatedFixture,
  testNeedsLookRedFixture,
  testRunningFixture,
} from "../../fixtures";

const push = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ push, replace: vi.fn(), back: vi.fn() }),
  usePathname: () => "/bench/r/x",
  useSearchParams: () => new URLSearchParams(),
}));

vi.mock("@/lib/api", () => ({
  api: { files: { contentUrl: (root: string, sub: string) => `/files/${root}/${sub}` } },
  BASE_URL: "",
  getToken: () => "test-token",
  request: vi.fn(),
}));

vi.mock("@/verticals/bench_studio/w1/api", async () => {
  const actual = await vi.importActual<typeof import("../../api")>("@/verticals/bench_studio/w1/api");
  return {
    ...actual,
    benchW1Api: {
      tests: { get: vi.fn(), rerun: vi.fn(), harvest: vi.fn(), stop: vi.fn() },
      kit: {
        get: vi.fn(),
        downloadToken: vi.fn(),
        fileUrl: (id: string, name: string, token: string) =>
          `/api/v1/bench/tests/${id}/kit/files/${name}?token=${token}`,
      },
    },
  };
});

vi.mock("@/verticals/bench_studio/api", () => ({
  benchApi: { sharedSubpath: (p: string) => p.replace(/^\/shared-deliverables\//, "") },
}));

vi.mock("@/lib/notify", () => ({
  notify: { success: vi.fn(), error: vi.fn(), info: vi.fn(), warning: vi.fn() },
}));

vi.mock("@/components/task/FilePreview", () => ({
  FilePreview: ({ fileUrl }: { fileUrl: string }) => <div data-testid="video-preview">{fileUrl}</div>,
}));

import { benchW1Api } from "@/verticals/bench_studio/w1/api";
import { benchResultHref } from "@/lib/benchLinks";
import { resetTruncationAckForTests } from "../../result/truncationAck";
import { XKitSheet } from "../XKitSheet";

function renderSheet(test: BenchTestDetail, kit: XKit, open = true) {
  vi.mocked(benchW1Api.tests.get).mockResolvedValue(test);
  vi.mocked(benchW1Api.kit.get).mockResolvedValue(kit);
  vi.mocked(benchW1Api.kit.downloadToken).mockResolvedValue({ token: "tok123", expires_in: 300 });
  const onClose = vi.fn();
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  render(
    <QueryClientProvider client={qc}>
      <XKitSheet testId={test.id} open={open} onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose };
}

const clipboard = { writeText: vi.fn().mockResolvedValue(undefined) };

beforeEach(() => {
  vi.clearAllMocks();
  resetTruncationAckForTests();
  Object.defineProperty(navigator, "clipboard", { value: clipboard, configurable: true });
});

describe("XKitSheet — closed", () => {
  it("renders nothing and fetches nothing while closed", () => {
    renderSheet(testDoneGreenFixture, kitGreenFixture, false);
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(benchW1Api.kit.get).not.toHaveBeenCalled();
  });
});

describe("XKitSheet — green", () => {
  it("shows both previews from real token links", async () => {
    renderSheet(testDoneGreenFixture, kitGreenFixture);
    const video = await screen.findByTestId("kit-video");
    await waitFor(() =>
      expect(video).toHaveAttribute(
        "src",
        `/api/v1/bench/tests/${testDoneGreenFixture.id}/kit/files/video.mp4?token=tok123#t=0.1`,
      ),
    );
    expect(screen.getByTestId("kit-card")).toHaveAttribute(
      "src",
      `/api/v1/bench/tests/${testDoneGreenFixture.id}/kit/files/card.png?token=tok123`,
    );
    expect(screen.getByText("MP4 · 1920×1080 · 24.5 s")).toBeInTheDocument();
    expect(screen.getByText("PNG · 1080×1350 · portrait")).toBeInTheDocument();
  });

  it("offers 'Download all' and single downloads as real links", async () => {
    renderSheet(testDoneGreenFixture, kitGreenFixture);
    const all = await screen.findByRole("link", { name: /Download all \(\.zip\)/ });
    expect(all).toHaveAttribute(
      "href",
      `/api/v1/bench/tests/${testDoneGreenFixture.id}/kit/files/kit.zip?token=tok123`,
    );
    expect(all).toHaveAttribute("download");
    expect(screen.getByRole("link", { name: /Save video/ })).toHaveAttribute(
      "href",
      expect.stringContaining("video.mp4?token=tok123"),
    );
    expect(screen.getByRole("link", { name: /Save image/ })).toHaveAttribute(
      "href",
      expect.stringContaining("card.png?token=tok123"),
    );
  });

  it("copies facts, settings and alt text exactly as the backend wrote them", async () => {
    const user = userEvent.setup();
    Object.defineProperty(navigator, "clipboard", { value: clipboard, configurable: true });
    renderSheet(testDoneGreenFixture, kitGreenFixture);
    const facts = await screen.findByTestId("copy-facts");
    await user.click(within(facts).getByRole("button", { name: "Copy" }));
    expect(clipboard.writeText).toHaveBeenCalledWith(kitGreenFixture.texts.facts.join("\n"));
    // One copy button per settings block: each block fits one X reply (S6).
    const blocks = within(screen.getByTestId("copy-settings")).getAllByTestId("settings-block");
    expect(blocks).toHaveLength(kitGreenFixture.texts.settings.length);
    for (const b of kitGreenFixture.texts.settings) expect(b.length).toBeLessThanOrEqual(280);
    await user.click(within(blocks[1]).getByRole("button", { name: "Copy block 2" }));
    expect(clipboard.writeText).toHaveBeenCalledWith(kitGreenFixture.texts.settings[1]);
    await user.click(within(screen.getByTestId("copy-alt")).getByRole("button", { name: "Copy" }));
    expect(clipboard.writeText).toHaveBeenCalledWith(kitGreenFixture.texts.alt_text);
    // No link while no episode is published (benchmark site offline).
    expect(screen.queryByTestId("copy-link")).toBeNull();
    expect(screen.getByTestId("kit-thread")).toHaveTextContent("How to post");
    expect(screen.getByTestId("kit-thread").textContent).not.toMatch(/link/);
  });

  it("phone: saving the video is the one primary, in the action bar; the zip is quiet", async () => {
    renderSheet(testDoneGreenFixture, kitGreenFixture);
    const bar = await screen.findByTestId("kit-actions");
    const save = await within(bar).findByTestId("kit-save-video");
    expect(save).toHaveAttribute("data-primary", "true");
    expect(save).toHaveTextContent("Save video");
    expect(within(bar).getByTestId("kit-zip")).not.toHaveAttribute("data-primary");
    expect(screen.getAllByText("Save video")).toHaveLength(1);
  });

  it("says privately when no handle is set (MC_BENCH_FOOTER missing)", async () => {
    renderSheet(testDoneGreenFixture, { ...kitGreenFixture, footer: "" });
    expect(await screen.findByTestId("kit-no-footer")).toHaveTextContent("No handle set");
  });

  it("offers the link only when the episode is published", async () => {
    renderSheet(testDoneGreenFixture, {
      ...kitGreenFixture,
      texts: { ...kitGreenFixture.texts, link: "https://episodes.example/ep-5" },
    });
    expect(await screen.findByTestId("copy-link")).toHaveTextContent("https://episodes.example/ep-5");
  });
});

describe("XKitSheet — red", () => {
  it("is preview only: reason, watermark, fix button, no downloads, no texts", async () => {
    const user = userEvent.setup();
    vi.mocked(benchW1Api.tests.harvest).mockResolvedValue(testNeedsLookRedFixture);
    renderSheet(testNeedsLookRedFixture, kitRedFixture);
    const note = await screen.findByTestId("kit-blocked");
    expect(note).toHaveTextContent("A headline number is missing: tokens out");
    expect(note).toHaveTextContent("Not ready to post");
    // No picture yet (no video, no card): no watermark over an empty box.
    expect(screen.queryByTestId("kit-watermark")).toBeNull();
    expect(screen.queryByRole("link", { name: /Download all/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Save/ })).toBeNull();
    expect(screen.queryByTestId("copy-facts")).toBeNull();
    expect(benchW1Api.kit.downloadToken).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Load numbers" }));
    await waitFor(() =>
      expect(benchW1Api.tests.harvest).toHaveBeenCalledWith(testNeedsLookRedFixture.id),
    );
  });
});

describe("XKitSheet — red with a recording", () => {
  it("stamps the preview 'Not ready to post'", async () => {
    renderSheet(
      { ...testNeedsLookRedFixture, video: { path: "/shared-deliverables/bench-x/grid-1.mp4", duration_s: 22, run_index: 1 } },
      kitRedFixture,
    );
    expect(await screen.findByTestId("kit-watermark")).toHaveTextContent("Not ready to post");
    expect(screen.getByTestId("video-preview")).toHaveTextContent("shared-deliverables/bench-x/grid-1.mp4");
  });
});

describe("XKitSheet — private truncation warning", () => {
  const truncatedKit: XKit = {
    ...kitGreenFixture,
    test_id: testDoneTruncatedFixture.id,
    gate: testDoneTruncatedFixture.gate,
    truncation: testDoneTruncatedFixture.truncation,
  };

  it("stops before the download until the operator decides", async () => {
    const user = userEvent.setup();
    renderSheet(testDoneTruncatedFixture, truncatedKit);
    expect(await screen.findByTestId("truncation-warning")).toHaveTextContent(
      "1 of 3 runs hit the token limit",
    );
    // Wait until the download token is there (previews use it) — only then
    // is "no download link" a real stop and not a race.
    await waitFor(() =>
      expect(screen.getByTestId("kit-video")).toHaveAttribute("src", expect.stringContaining("tok123")),
    );
    expect(screen.queryByRole("link", { name: /Download all/ })).toBeNull();
    expect(screen.queryByRole("link", { name: /Save video|Save image/ })).toBeNull();
    await user.click(screen.getByRole("button", { name: "Continue anyway" }));
    expect(await screen.findByRole("link", { name: /Download all/ })).toBeInTheDocument();
    expect(screen.queryByTestId("truncation-warning")).toBeNull();
  });

  it("never puts the truncation into the amber notes that go public", async () => {
    renderSheet(testDoneTruncatedFixture, truncatedKit);
    await screen.findByTestId("truncation-warning");
    expect(screen.queryByTestId("kit-notes")).toBeNull();
  });

  it("over the cap a paid re-run asks twice before it starts (D4)", async () => {
    const user = userEvent.setup();
    vi.mocked(benchW1Api.tests.rerun)
      .mockRejectedValueOnce(
        new Error('API 409: {"detail":{"code":"bench_cost_confirmation","step":1,"usd":2.5,"cap_usd":1.0}}'),
      )
      .mockRejectedValueOnce(
        new Error('API 409: {"detail":{"code":"bench_cost_confirmation","step":2,"usd":2.5,"cap_usd":1.0}}'),
      )
      .mockResolvedValueOnce({ ...testRunningFixture, id: "new-test" });
    renderSheet(testDoneTruncatedFixture, truncatedKit);
    await user.click(await screen.findByRole("button", { name: "Re-run with 32,768 tokens" }));
    // The price question keeps what the button does (N2).
    await user.click(await screen.findByRole("button", { name: "Re-run with 32,768 tokens · $2.50 — OK?" }));
    await user.click(await screen.findByRole("button", { name: "Over $1: really start for $2.50?" }));
    await waitFor(() =>
      expect(benchW1Api.tests.rerun).toHaveBeenLastCalledWith(testDoneTruncatedFixture.id, "larger", {
        cost_confirmed_usd: 2.5,
        confirm_over_cap: true,
      }),
    );
    await waitFor(() => expect(push).toHaveBeenCalledWith(benchResultHref("new-test")));
  });

  it("re-runs with a larger budget, closes and opens the new result", async () => {
    const user = userEvent.setup();
    vi.mocked(benchW1Api.tests.rerun).mockResolvedValue({ ...testRunningFixture, id: "new-test" });
    const { onClose } = renderSheet(testDoneTruncatedFixture, truncatedKit);
    await user.click(await screen.findByRole("button", { name: "Re-run with 32,768 tokens" }));
    await waitFor(() =>
      expect(benchW1Api.tests.rerun).toHaveBeenCalledWith(testDoneTruncatedFixture.id, "larger"),
    );
    await waitFor(() => expect(push).toHaveBeenCalledWith(benchResultHref("new-test")));
    expect(onClose).toHaveBeenCalled();
  });
});
