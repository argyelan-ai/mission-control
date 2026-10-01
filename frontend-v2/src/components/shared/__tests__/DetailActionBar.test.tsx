/**
 * Detail screens on the phone (mobile nav V2): Back · main action · Reply ·
 * More at the bottom. The main action mirrors the screen's one primary
 * button instead of re-implementing it.
 */
import { afterEach, describe, expect, it, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { DetailActionBar } from "../DetailActionBar";
import { findMainAction, runMainAction } from "@/lib/taskDetail/mainAction";
import { PRIMARY_BTN } from "@/components/task/detail/nextStepStyle";
import { chatsTabHref, loadRecentChats, pushRecent, rememberChat } from "@/lib/recentChats";

// jsdom here has no working Storage — same in-memory stub as the other shell tests.
vi.hoisted(() => {
  const store: Record<string, string> = {};
  Object.defineProperty(globalThis, "localStorage", {
    value: {
      getItem: (k: string) => store[k] ?? null,
      setItem: (k: string, v: string) => { store[k] = v; },
      removeItem: (k: string) => { delete store[k]; },
      clear: () => { for (const k of Object.keys(store)) delete store[k]; },
    },
    configurable: true,
    writable: true,
  });
});

afterEach(() => {
  // @ts-expect-error — test cleanup of the keyboard stub
  delete window.visualViewport;
  localStorage.clear();
});

describe("DetailActionBar", () => {
  it("shows Back · main · Reply · More and wires each", async () => {
    const onBack = vi.fn();
    const onMain = vi.fn();
    const onReply = vi.fn();
    render(
      <DetailActionBar
        onBack={onBack}
        main={{ label: "Approve", onClick: onMain }}
        onReply={onReply}
        more={<button type="button">More</button>}
      />,
    );
    const bar = screen.getByRole("toolbar", { name: "Actions" });
    expect([...bar.querySelectorAll("button")].map((b) => b.textContent)).toEqual(["Back", "Approve", "Reply", "More"]);
    await userEvent.click(screen.getByRole("button", { name: "Back" }));
    await userEvent.click(screen.getByRole("button", { name: "Approve" }));
    await userEvent.click(screen.getByRole("button", { name: "Reply" }));
    expect(onBack).toHaveBeenCalledTimes(1);
    expect(onMain).toHaveBeenCalledTimes(1);
    expect(onReply).toHaveBeenCalledTimes(1);
  });

  it("is phone-only and hides while the keyboard is up", () => {
    const { container, unmount } = render(<DetailActionBar onBack={() => {}} />);
    expect(container.firstElementChild).toHaveClass("md:hidden");
    unmount();
    Object.defineProperty(window, "visualViewport", {
      value: { height: window.innerHeight - 320, offsetTop: 0, addEventListener: () => {}, removeEventListener: () => {} },
      configurable: true,
    });
    render(<DetailActionBar onBack={() => {}} />);
    expect(screen.queryByRole("toolbar")).toBeNull();
  });
});

describe("main action mirror", () => {
  it("finds the first primary button of the next step and its label", () => {
    const root = document.createElement("div");
    root.innerHTML = `<p>text</p><button class="${PRIMARY_BTN}"><svg></svg> Approve </button><button class="${PRIMARY_BTN}">Second</button>`;
    expect(findMainAction(root)?.label).toBe("Approve");
    expect(findMainAction(document.createElement("div"))).toBeNull();
  });

  it("an enabled primary runs in place", () => {
    const root = document.createElement("div");
    root.innerHTML = `<button class="${PRIMARY_BTN}">Open log</button>`;
    const clicked = vi.fn();
    root.querySelector("button")!.addEventListener("click", clicked);
    expect(runMainAction(findMainAction(root)!.el)).toBe("clicked");
    expect(clicked).toHaveBeenCalledTimes(1);
  });

  it("a disabled primary (approve needs a reason) focuses the reason field instead", () => {
    document.body.innerHTML = `<section><textarea id="why"></textarea><button class="${PRIMARY_BTN}" disabled>Approve</button></section>`;
    const clicked = vi.fn();
    const btn = document.querySelector("button")!;
    btn.addEventListener("click", clicked);
    expect(runMainAction(findMainAction(document.body)!.el)).toBe("focused");
    expect(clicked).not.toHaveBeenCalled();
    expect(document.activeElement?.id).toBe("why");
    document.body.innerHTML = "";
  });
});

describe("recent chats", () => {
  it("keeps the newest first without duplicates, capped", () => {
    let list = pushRecent([], { kind: "agent", id: "a" });
    list = pushRecent(list, { kind: "group", id: "g" });
    list = pushRecent(list, { kind: "agent", id: "a" });
    expect(list).toEqual([{ kind: "agent", id: "a" }, { kind: "group", id: "g" }]);
    for (let i = 0; i < 10; i++) list = pushRecent(list, { kind: "agent", id: `x${i}` });
    expect(list).toHaveLength(5);
  });

  it("remembers across loads and falls back to the sessions page's last agent", () => {
    localStorage.setItem("mc-sessions-last-agent", "legacy");
    expect(loadRecentChats()).toEqual([{ kind: "agent", id: "legacy" }]);
    rememberChat({ kind: "group", id: "g1" });
    expect(loadRecentChats()[0]).toEqual({ kind: "group", id: "g1" });
  });

  it("ignores broken storage content", () => {
    localStorage.setItem("mc-recent-chats", "{not json");
    expect(loadRecentChats()).toEqual([]);
    localStorage.setItem("mc-recent-chats", JSON.stringify([{ kind: "x", id: 1 }]));
    expect(loadRecentChats()).toEqual([]);
  });

  it("Chats tab: last chat from elsewhere, the list on the chats page", () => {
    const recent = [{ kind: "group" as const, id: "g 1" }];
    expect(chatsTabHref("/", recent)).toBe("/sessions?group=g%201");
    expect(chatsTabHref("/sessions", recent)).toBe("/sessions");
    expect(chatsTabHref("/tasks", [])).toBe("/sessions");
  });
});
