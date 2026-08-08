/**
 * useContainerSize — Task #13 (memory graph iOS-PWA render bug).
 *
 * The hook exists specifically to NEVER read window.innerWidth/innerHeight
 * (both lie in iOS PWA standalone mode — see the hook's doc comment and the
 * ios-pwa-lvh-viewport memory finding: window.innerHeight reports ~793px on
 * a device whose real viewport is 852px). It must derive size purely from
 * the DOM container it's attached to.
 *
 * Coverage:
 *   1. mount measurement comes from getBoundingClientRect() of the
 *      container, even when window.innerWidth/innerHeight report a
 *      different (wrong) value — proves the hook is container-derived, not
 *      window-derived.
 *   2. a ResizeObserver-reported container resize updates the size.
 *   3. unmount disconnects the ResizeObserver (no leaked observers).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { render, screen, act, cleanup } from "@testing-library/react";
import { useContainerSize } from "../useContainerSize";

type ROCallback = (entries: ResizeObserverEntry[], observer: ResizeObserver) => void;

class MockResizeObserver {
  static instances: MockResizeObserver[] = [];
  callback: ROCallback;
  observedCount = 0;
  disconnected = false;

  constructor(callback: ROCallback) {
    this.callback = callback;
    MockResizeObserver.instances.push(this);
  }

  observe() {
    this.observedCount += 1;
  }

  unobserve() {
    this.observedCount -= 1;
  }

  disconnect() {
    this.disconnected = true;
  }

  trigger(rect: { width: number; height: number }) {
    this.callback(
      [{ contentRect: rect } as unknown as ResizeObserverEntry],
      this as unknown as ResizeObserver,
    );
  }
}

function Harness() {
  const { ref, size } = useContainerSize<HTMLDivElement>();
  return (
    <div ref={ref} data-testid="container">
      <span data-testid="size">{size ? `${size.width}x${size.height}` : "null"}</span>
    </div>
  );
}

function mockRect(width: number, height: number) {
  return vi.spyOn(HTMLElement.prototype, "getBoundingClientRect").mockReturnValue({
    width,
    height,
    top: 0,
    left: 0,
    right: width,
    bottom: height,
    x: 0,
    y: 0,
    toJSON() {
      return {};
    },
  });
}

describe("useContainerSize", () => {
  let originalRO: typeof ResizeObserver;
  let originalInnerWidth: number;
  let originalInnerHeight: number;

  beforeEach(() => {
    MockResizeObserver.instances = [];
    originalRO = globalThis.ResizeObserver;
    globalThis.ResizeObserver = MockResizeObserver as unknown as typeof ResizeObserver;
    originalInnerWidth = window.innerWidth;
    originalInnerHeight = window.innerHeight;
  });

  afterEach(() => {
    cleanup();
    globalThis.ResizeObserver = originalRO;
    Object.defineProperty(window, "innerWidth", { value: originalInnerWidth, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: originalInnerHeight, configurable: true });
    vi.restoreAllMocks();
  });

  it("measures the container on mount, ignoring window.innerWidth/innerHeight entirely", () => {
    // iOS PWA standalone sentinel: window reports the real measured-wrong
    // number (793) from the memory finding; the container reports the TRUE
    // viewport (852). If the hook ever fell back to window.*, this would
    // read 793 instead of 852.
    Object.defineProperty(window, "innerWidth", { value: 793, configurable: true });
    Object.defineProperty(window, "innerHeight", { value: 793, configurable: true });
    const rectSpy = mockRect(390, 852);

    render(<Harness />);

    expect(screen.getByTestId("size").textContent).toBe("390x852");
    rectSpy.mockRestore();
  });

  it("updates size when ResizeObserver reports a container resize", () => {
    const rectSpy = mockRect(300, 500);
    render(<Harness />);
    expect(screen.getByTestId("size").textContent).toBe("300x500");

    const observer = MockResizeObserver.instances[0];
    expect(observer).toBeDefined();
    act(() => {
      observer.trigger({ width: 320, height: 700 });
    });

    expect(screen.getByTestId("size").textContent).toBe("320x700");
    rectSpy.mockRestore();
  });

  it("disconnects the ResizeObserver on unmount", () => {
    const rectSpy = mockRect(300, 500);
    const { unmount } = render(<Harness />);
    const observer = MockResizeObserver.instances[0];

    unmount();

    expect(observer.disconnected).toBe(true);
    rectSpy.mockRestore();
  });
});
