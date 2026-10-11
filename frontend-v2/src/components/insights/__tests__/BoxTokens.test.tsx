/** Insights → local boxes: box total, via MC, external (direct API), cached apart. */
import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { mkBox, mkUsageWeek } from "@/lib/__tests__/usageFixtures";
import { BoxTokens } from "../BoxTokens";

const SINCE = "2026-10-11T08:15:00+00:00";

function week(box = mkBox()) {
  return mkUsageWeek({ totals: { ...mkUsageWeek().totals, ...box } });
}

describe("BoxTokens", () => {
  it("leads with the box total and splits it into via MC and external", () => {
    render(<BoxTokens week={week()} since={SINCE} />);
    expect(screen.getByTestId("box-total")).toHaveTextContent("1.2M");
    expect(screen.getByTestId("box-via-mc")).toHaveTextContent("Through MC");
    expect(screen.getByTestId("box-via-mc")).toHaveTextContent("480K");
    expect(screen.getByTestId("box-via-mc")).toHaveTextContent("40%");
    expect(screen.getByTestId("box-external")).toHaveTextContent("Direct API (not through MC)");
    expect(screen.getByTestId("box-external")).toHaveTextContent("720K");
    expect(screen.getByTestId("box-external")).toHaveTextContent("60%");
  });

  it("shows cached prompt tokens apart, not in the headline", () => {
    render(<BoxTokens week={week()} since={SINCE} />);
    expect(screen.getByTestId("box-cached")).toHaveTextContent("14.8M");
    expect(screen.getByTestId("box-cached")).toHaveTextContent("cached prompt tokens");
    expect(screen.getByTestId("box-total")).not.toHaveTextContent("14.8M");
  });

  it("says honestly since when it counts", () => {
    render(<BoxTokens week={week()} since={SINCE} />);
    expect(screen.getByTestId("box-since")).toHaveTextContent(
      "Counted from the boxes' own counters since October 11. Nothing before that.",
    );
  });

  it("notes when MC counted more than the box saw", () => {
    render(<BoxTokens week={week(mkBox(100, 500, 0, true))} since={SINCE} />);
    expect(screen.getByTestId("box-clamped")).toBeInTheDocument();
    expect(screen.getByTestId("box-external")).toHaveTextContent("0");
  });

  it("explains that counting has not started yet", () => {
    render(<BoxTokens week={mkUsageWeek()} since={null} />);
    expect(screen.getByText(/Not counting yet/)).toBeInTheDocument();
    expect(screen.queryByTestId("box-total")).toBeNull();
  });
});
