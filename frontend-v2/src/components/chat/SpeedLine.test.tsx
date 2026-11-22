/**
 * The speed line under the composer (variant C, 2026-11-18):
 * dot · box · model · live tok/s — with data, without data, and only the
 * numbers MC really measures (GET /hosts/{id}/pulse `now_tps`).
 */
import { describe, expect, it, vi } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { NextIntlClientProvider } from "next-intl";
import en from "../../../messages/en.json";

const apiMock = vi.hoisted(() => ({
  runtimes: [] as unknown[],
  pulse: { points: [], now_tps: 41.4, idle_seconds: 0, available: true } as unknown,
  pulseCalls: 0,
}));
vi.mock("@/lib/api", () => ({
  api: {
    runtimes: { listRows: vi.fn(async () => ({ runtimes: apiMock.runtimes })) },
    hosts: {
      pulse: vi.fn(async () => {
        apiMock.pulseCalls += 1;
        return apiMock.pulse;
      }),
    },
  },
}));

import { SpeedLine, SpeedLineView, boxLabel, lastTps, liveTps, shortModelName } from "./SpeedLine";

const intl = (ui: React.ReactNode) => (
  <NextIntlClientProvider locale="en" messages={en}>
    {ui}
  </NextIntlClientProvider>
);

const RUNTIME = {
  id: "rt-1",
  model_identifier: "qwen3.8-27b",
  host: { id: "h-1", slug: "box-a", display_name: "Box A" },
  member_hosts: [
    { host_id: "h-1", slug: "box-a", display_name: "Box A", role: "head", node_rank: 0 },
    { host_id: "h-2", slug: "box-b", display_name: "Box B", role: "worker", node_rank: 1 },
  ],
};

describe("boxLabel / liveTps", () => {
  it("names the head box and its duo members once each", () => {
    expect(boxLabel(RUNTIME as never)).toBe("Box A + Box B");
    expect(boxLabel({ host: null, member_hosts: [] } as never)).toBeNull();
  });

  it("gives tok/s only while streaming and only when the box measures it", () => {
    const pulse = { points: [], now_tps: 41.4, idle_seconds: 0, available: true };
    expect(liveTps(pulse, true)).toBe(41);
    expect(liveTps(pulse, false)).toBeNull();
    expect(liveTps({ ...pulse, available: false }, true)).toBeNull();
    expect(liveTps({ ...pulse, now_tps: 0 }, true)).toBeNull();
    expect(liveTps(undefined, true)).toBeNull();
  });

  it("at rest: the newest point the box measured above zero", () => {
    const pulse = { points: [{ t: 1, tps: 38.6 }, { t: 2, tps: 41.2 }, { t: 3, tps: 0 }], now_tps: 0, idle_seconds: 40, available: true };
    expect(lastTps(pulse)).toBe(41);
    expect(lastTps({ ...pulse, points: [] })).toBeNull();
    expect(lastTps({ ...pulse, available: false })).toBeNull();
  });
});

describe("SpeedLineView", () => {
  // Operator 22.11.2026: on a 390 px phone the chip cuts the model
  // ("DeepSeek-V…"), so this line names it whole again — without the
  // provider path that the user test of 21.11. objected to.
  it("with data: box · whole model and the live tok/s", () => {
    render(intl(<SpeedLineView box="Box A + Box B" model="mc-openai/DeepSeek-V4.1-Flash-1017" tps={41} live />));
    expect(screen.getByTestId("speed-line-label")).toHaveTextContent(/^Box A \+ Box B · DeepSeek-V4\.1-Flash-1017$/);
    expect(screen.getByTestId("speed-line")).not.toHaveTextContent("mc-openai/");
    expect(screen.getByTestId("speed-line-tps")).toHaveTextContent("41 tok/s");
  });

  it("the model name is never cut: the label wraps instead of truncating", () => {
    render(intl(<SpeedLineView box="Box A" model="DeepSeek-V4.1-Flash-1017" tps={41} live />));
    expect(screen.getByTestId("speed-line-label")).not.toHaveClass("truncate");
  });

  it("at rest: the last measured tok/s, quiet, not marked live", () => {
    render(intl(<SpeedLineView box="Box A" model="qwen3.8-27b" tps={38} />));
    expect(screen.getByTestId("speed-line")).toHaveTextContent("Box A · qwen3.8-27b");
    expect(screen.getByTestId("speed-line-tps")).toHaveTextContent("38 tok/s");
    expect(screen.getByTestId("speed-line-tps")).not.toHaveAttribute("data-live");
  });

  it("a box that never measured: '– tok/s' only while the chat streams, nothing at rest", () => {
    const { unmount } = render(intl(<SpeedLineView box="Box A" model="qwen3.8-27b" tps={null} streaming />));
    expect(screen.getByTestId("speed-line-tps")).toHaveTextContent("– tok/s");
    unmount();
    render(intl(<SpeedLineView box="Box A" model="qwen3.8-27b" tps={null} />));
    expect(screen.queryByTestId("speed-line-tps")).toBeNull();
    expect(screen.getByTestId("speed-line")).toHaveTextContent(/^Box A · qwen3\.8-27b$/);
  });

  it("a cloud model without a box: no line — its chip is not cut, the line would only repeat it", () => {
    render(intl(<SpeedLineView box={null} model="claude-opus-5" tps={null} />));
    expect(screen.queryByTestId("speed-line")).toBeNull();
  });

  it("a cloud model with the pair picker: the line is the door, named by the short model", () => {
    render(intl(<SpeedLineView box={null} model="custom:anthropic/claude-opus-5" tps={null} onPick={() => {}} />));
    expect(screen.getByTestId("speed-line")).toHaveTextContent(/^claude-opus-5$/);
  });

  it("without data: no line at all (K3)", () => {
    render(intl(<SpeedLineView box={null} model={null} tps={null} />));
    expect(screen.queryByTestId("speed-line")).toBeNull();
  });
});

describe("SpeedLine (loaded)", () => {
  const withQuery = (ui: React.ReactNode) => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    return render(intl(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>));
  };

  it("reads box and model from the runtime row and tok/s from the box's pulse", async () => {
    apiMock.runtimes = [RUNTIME];
    withQuery(<SpeedLine runtimeId="rt-1" model={null} streaming />);
    await waitFor(() => expect(screen.getByTestId("speed-line-tps")).toHaveTextContent("41 tok/s"));
    expect(screen.getByTestId("speed-line")).toHaveTextContent("Box A + Box B · qwen3.8-27b");
  });

  it("the transcript's model wins over the runtime row's", async () => {
    apiMock.runtimes = [RUNTIME];
    withQuery(<SpeedLine runtimeId="rt-1" model="qwen3.8-27b-nvfp4" streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("speed-line")).toHaveTextContent("Box A + Box B · qwen3.8-27b-nvfp4"));
  });

  it("at rest it still shows the box's last measured speed", async () => {
    apiMock.runtimes = [RUNTIME];
    apiMock.pulse = { points: [{ t: 1, tps: 39.7 }], now_tps: 0, idle_seconds: 300, available: true };
    withQuery(<SpeedLine runtimeId="rt-1" model={null} streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("speed-line-tps")).toHaveTextContent("40 tok/s"));
    expect(screen.getByTestId("speed-line-tps")).not.toHaveAttribute("data-live");
    apiMock.pulse = { points: [], now_tps: 41.4, idle_seconds: 0, available: true };
  });

  it("steps aside outside a query client instead of throwing", () => {
    render(intl(<SpeedLine runtimeId="rt-1" model="m" streaming />));
    expect(screen.queryByTestId("speed-line")).toBeNull();
  });
});

describe("shortModelName (user test 21.11.)", () => {
  it("drops provider paths and the custom: prefix", () => {
    expect(shortModelName("mc-openai/DeepSeek-V4.1-Flash-1017")).toBe("DeepSeek-V4.1-Flash-1017");
    expect(shortModelName("custom:openai/qwen3.8-27b")).toBe("qwen3.8-27b");
    expect(shortModelName("claude-opus-5")).toBe("claude-opus-5");
  });

  it("keeps the model's identity: family name and version or build tag stay (critic 21.11.)", () => {
    // A numeric tail is a version as often as a build date; the family name
    // is what tells two "V3"s apart. The chip is the only place the model
    // is named, so nothing that tells models apart may go.
    expect(shortModelName("Mistral-Large-2411")).toBe("Mistral-Large-2411");
    expect(shortModelName("deepseek-r1-0528")).toBe("deepseek-r1-0528");
    expect(shortModelName("Kimi-K2-0905")).toBe("Kimi-K2-0905");
    expect(shortModelName("Qwen-V3")).toBe("Qwen-V3");
  });

  it("a label with spaces around a slash is a name, not a provider path", () => {
    expect(shortModelName("GPT-5.1 / codex")).toBe("GPT-5.1 / codex");
  });

  it("a catalogue display name wins", () => {
    expect(shortModelName("custom:anthropic/claude-opus-5", "Opus 5")).toBe("Opus 5");
  });

  it("nothing in, nothing out — never an empty string the caller's ?? would keep", () => {
    expect(shortModelName(null)).toBeNull();
    expect(shortModelName("  ")).toBeNull();
    expect(shortModelName("custom:")).toBeNull();
    expect(shortModelName("mc-openai/")).toBeNull();
  });
});
