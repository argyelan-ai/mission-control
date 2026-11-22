/** /login: one <main> and an h1 in every state, including the first paint. */
import { describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { api } from "@/lib/api";

vi.mock("next/navigation", () => ({ useRouter: () => ({ replace: vi.fn(), push: vi.fn() }) }));

import LoginPage from "../page";

describe("login landmarks", () => {
  it("while it still asks whether setup is needed: main + h1, marked busy", () => {
    vi.spyOn(api.auth, "setupRequired").mockReturnValue(new Promise(() => {}));
    render(<LoginPage />);
    const main = screen.getByRole("main");
    expect(main).toHaveAttribute("aria-busy", "true");
    expect(screen.getByRole("heading", { level: 1 })).toBeInTheDocument();
  });

  it("the form screen is inside main and carries an h1", async () => {
    vi.spyOn(api.auth, "setupRequired").mockResolvedValue({ setup_required: false } as never);
    render(<LoginPage />);
    const main = await screen.findByRole("main");
    await screen.findByRole("button", { name: /sign in/i });
    expect(main).not.toHaveAttribute("aria-busy");
    expect(screen.getAllByRole("heading", { level: 1 }).length).toBeGreaterThan(0);
  });
});
