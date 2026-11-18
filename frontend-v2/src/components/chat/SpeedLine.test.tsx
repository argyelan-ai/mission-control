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

import { SpeedLine, SpeedLineView, boxLabel, liveTps } from "./SpeedLine";

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
});

describe("SpeedLineView", () => {
  it("with data: box · model and the live tok/s", () => {
    render(intl(<SpeedLineView box="Box A + Box B" model="qwen3.8-27b" tps={41} />));
    const line = screen.getByTestId("speed-line");
    expect(line).toHaveTextContent("Box A + Box B · qwen3.8-27b");
    expect(screen.getByTestId("speed-line-tps")).toHaveTextContent("41 tok/s");
  });

  it("not streaming: box · model, no number", () => {
    render(intl(<SpeedLineView box="Box A" model="qwen3.8-27b" tps={null} />));
    expect(screen.getByTestId("speed-line")).toHaveTextContent("Box A · qwen3.8-27b");
    expect(screen.queryByTestId("speed-line-tps")).toBeNull();
  });

  it("a cloud model without a box: only the model", () => {
    render(intl(<SpeedLineView box={null} model="claude-opus-5" tps={null} />));
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

  it("asks the box for its pulse only while the chat streams", async () => {
    apiMock.runtimes = [RUNTIME];
    apiMock.pulseCalls = 0;
    withQuery(<SpeedLine runtimeId="rt-1" model={null} streaming={false} />);
    await waitFor(() => expect(screen.getByTestId("speed-line")).toBeInTheDocument());
    expect(apiMock.pulseCalls).toBe(0);
  });

  it("steps aside outside a query client instead of throwing", () => {
    render(intl(<SpeedLine runtimeId="rt-1" model="m" streaming />));
    expect(screen.queryByTestId("speed-line")).toBeNull();
  });
});
