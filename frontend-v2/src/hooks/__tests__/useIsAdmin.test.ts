import { describe, it, expect, vi } from "vitest";
import { renderHook } from "@testing-library/react";

const mockStore = vi.hoisted(() => ({
  state: { currentUser: null as { id: string; email: string; name: string; role: string } | null },
}));
vi.mock("@/lib/store", () => ({
  useAppStore: (selector: (s: typeof mockStore.state) => unknown) => selector(mockStore.state),
}));

import { useIsAdmin } from "../useIsAdmin";

const user = (role: string) => ({ id: "u", email: "u@mc.local", name: "U", role });

describe("useIsAdmin", () => {
  it.each([
    ["admin", true],
    ["operator", false],
    ["viewer", false],
    ["Admin", false], // roles are exact, lower-case strings
  ])("role %s → %s", (role, expected) => {
    mockStore.state.currentUser = user(role);
    expect(renderHook(() => useIsAdmin()).result.current).toBe(expected);
  });

  it("no user loaded yet → not admin (fail closed)", () => {
    mockStore.state.currentUser = null;
    expect(renderHook(() => useIsAdmin()).result.current).toBe(false);
  });
});
