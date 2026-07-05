import React from "react";
import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { act, render, screen, fireEvent } from "@testing-library/react";
import ToastRenderer from "../ToastRenderer";
import { notify } from "@/lib/notify";
import { useNotificationStore } from "@/lib/store";

// AnimatePresence normally holds a node in the DOM until its exit animation
// finishes via requestAnimationFrame — which fake timers can't drive, so
// dismissed toasts would never unmount in these tests. Replace with plain
// elements that unmount immediately, like the rest of the app sees at rest.
vi.mock("framer-motion", () => ({
  motion: new Proxy(
    {},
    {
      get:
        (_target, tag: string) =>
        ({ children, layout: _layout, initial: _initial, animate: _animate, exit: _exit, transition: _transition, ...rest }: Record<string, unknown>) =>
          React.createElement(tag, rest, children as React.ReactNode),
    }
  ),
  AnimatePresence: ({ children }: { children?: React.ReactNode }) => <>{children}</>,
}));

describe("ToastRenderer", () => {
  beforeEach(() => {
    useNotificationStore.getState().clearNotifications();
    vi.useFakeTimers();
  });

  afterEach(() => {
    act(() => {
      vi.runOnlyPendingTimers();
    });
    vi.useRealTimers();
  });

  it("notify.success renders a toast with the message", () => {
    render(<ToastRenderer />);

    act(() => {
      notify.success("Repo imported");
    });

    expect(screen.getByText("Repo imported")).toBeInTheDocument();
  });

  it("auto-dismisses a success toast after ~5s", () => {
    render(<ToastRenderer />);

    act(() => {
      notify.success("Saved");
    });
    expect(screen.getByText("Saved")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5000);
    });

    expect(screen.queryByText("Saved")).not.toBeInTheDocument();
  });

  it("error toast survives 5s, is manually closable via the X button", () => {
    render(<ToastRenderer />);

    act(() => {
      notify.error("Sync failed");
    });
    expect(screen.getByText("Sync failed")).toBeInTheDocument();

    act(() => {
      vi.advanceTimersByTime(5000);
    });
    // Still visible — errors get the longer ~8s timeout.
    expect(screen.getByText("Sync failed")).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /dismiss notification/i }));

    expect(screen.queryByText("Sync failed")).not.toBeInTheDocument();
  });

  it("error toast auto-dismisses after ~8s if not closed manually", () => {
    render(<ToastRenderer />);

    act(() => {
      notify.error("Delete failed");
    });

    act(() => {
      vi.advanceTimersByTime(8000);
    });

    expect(screen.queryByText("Delete failed")).not.toBeInTheDocument();
  });

  it("stacks at most 4 visible toasts", () => {
    render(<ToastRenderer />);

    act(() => {
      notify.success("one");
      notify.success("two");
      notify.success("three");
      notify.success("four");
      notify.success("five");
    });

    // "one" was pushed out of the visible window by the 5-toast burst.
    expect(screen.queryByText("one")).not.toBeInTheDocument();
    expect(screen.getByText("five")).toBeInTheDocument();
  });
});
