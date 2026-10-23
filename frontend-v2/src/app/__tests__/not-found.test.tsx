import React from "react";
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";

// Round-3 user test t4 S9: every wrong URL showed the framework's English
// "404 | This page could not be found." without the app's menu or tab bar —
// on the phone only the browser's back button led out.

const shell = vi.fn();
vi.mock("@/components/layout/AppShell", () => ({
  default: ({ children }: { children: React.ReactNode }) => {
    shell();
    return <div data-testid="app-shell">{children}</div>;
  },
}));

import NotFound from "../not-found";

describe("app/not-found", () => {
  it("renders inside the app frame with a way home", () => {
    render(<NotFound />);
    expect(shell).toHaveBeenCalled();
    expect(screen.getByTestId("app-shell")).toContainElement(screen.getByText("Page not found"));
    expect(screen.getByRole("link", { name: /Go to home/ })).toHaveAttribute("href", "/");
    expect(screen.queryByText(/This page could not be found/)).not.toBeInTheDocument();
  });

  // Round-4 r2 N4: the 404 had no heading — a screen reader found no h1.
  it("names the page in an h1", () => {
    render(<NotFound />);
    expect(screen.getByRole("heading", { level: 1, name: "Page not found" })).toBeInTheDocument();
  });
});
