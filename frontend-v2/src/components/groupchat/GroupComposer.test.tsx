/**
 * GroupComposer — Verhaltenstests.
 *
 * Die Labels sind ENGLISCH: src/test-setup.ts mockt next-intl gegen
 * messages/en.json. Der „alle"-Eintrag zeigt darum „@alle" (der Handle, den
 * das Backend kennt) plus das übersetzte Wort „all" daneben.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { GroupComposer } from "./GroupComposer";

const MEMBERS = [
  { slug: "sparky", name: "Sparky", emoji: "⚡" },
  { slug: "rex", name: "Rex", emoji: "🔍" },
  { slug: "mia", name: "Mia Reviewer", emoji: null },
];

function renderComposer(props: Partial<React.ComponentProps<typeof GroupComposer>> = {}) {
  const onSend = vi.fn();
  render(<GroupComposer members={MEMBERS} onSend={onSend} {...props} />);
  const textarea = screen.getByPlaceholderText(/Message the group/) as HTMLTextAreaElement;
  return { onSend, textarea };
}

describe("GroupComposer", () => {
  it("sends the typed text on Enter and clears the field", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer();
    await user.type(textarea, "wie steht es?{Enter}");
    expect(onSend).toHaveBeenCalledWith("wie steht es?");
    expect(textarea).toHaveValue("");
  });

  it("inserts a newline on Shift+Enter instead of sending", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer();
    await user.type(textarea, "zeile1{Shift>}{Enter}{/Shift}zeile2");
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("zeile1\nzeile2");
  });

  it("does not send whitespace-only text", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend } = renderComposer();
    const textarea = screen.getByPlaceholderText(/Message the group/);
    await user.type(textarea, "   {Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });

  it("opens the mention palette on @ with every member plus the all entry", async () => {
    const user = userEvent.setup({ delay: null });
    const { textarea } = renderComposer();
    await user.type(textarea, "@");
    const options = screen.getAllByRole("option");
    expect(options).toHaveLength(MEMBERS.length + 1);
    expect(options[0]).toHaveTextContent("all");
    expect(screen.getByTestId("mention-item-sparky")).toBeInTheDocument();
    expect(screen.getByTestId("mention-item-rex")).toBeInTheDocument();
    expect(screen.getByTestId("mention-item-mia")).toBeInTheDocument();
  });

  it("filters the palette by the prefix typed after the @", async () => {
    const user = userEvent.setup({ delay: null });
    const { textarea } = renderComposer();
    await user.type(textarea, "@RE");
    expect(screen.getAllByRole("option")).toHaveLength(1);
    expect(screen.getByTestId("mention-item-rex")).toBeInTheDocument();
  });

  it("inserts the highlighted handle on ArrowDown+Enter and closes the palette", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer();
    await user.type(textarea, "@");
    await user.keyboard("{ArrowDown}{Enter}");
    expect(textarea).toHaveValue("@sparky ");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(onSend).not.toHaveBeenCalled();
  });

  it("marks the highlighted option as selected while arrowing through", async () => {
    const user = userEvent.setup({ delay: null });
    const { textarea } = renderComposer();
    await user.type(textarea, "@");
    expect(screen.getAllByRole("option")[0]).toHaveAttribute("aria-selected", "true");
    await user.keyboard("{ArrowDown}{ArrowDown}");
    const options = screen.getAllByRole("option");
    expect(options[0]).toHaveAttribute("aria-selected", "false");
    expect(options[2]).toHaveAttribute("aria-selected", "true");
  });

  it("closes the palette on Escape and keeps the typed text", async () => {
    const user = userEvent.setup({ delay: null });
    const { textarea } = renderComposer();
    await user.type(textarea, "@sp");
    expect(screen.getByRole("listbox")).toBeInTheDocument();
    await user.keyboard("{Escape}");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
    expect(textarea).toHaveValue("@sp");
  });

  it("does not send while the mention palette is open", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer();
    await user.type(textarea, "hallo @re");
    await user.keyboard("{Enter}");
    expect(onSend).not.toHaveBeenCalled();
    expect(textarea).toHaveValue("hallo @rex ");
  });

  it("inserts the localized broadcast handle when the all entry is clicked", async () => {
    // MC läuft deutsch UND englisch. Eingefügt wird der Handle der aktiven
    // Sprache (hier englisch: "all"); das Backend kennt beide Formen
    // (group_service.BROADCAST_HANDLES). Eingefügtes und angezeigtes Wort
    // müssen identisch sein — sonst zeigt die Palette etwas anderes, als sie
    // schreibt.
    const user = userEvent.setup({ delay: null });
    const { textarea } = renderComposer();
    await user.type(textarea, "@");
    await user.click(screen.getByTestId("mention-item-all"));
    expect(textarea).toHaveValue("@all ");
    expect(screen.queryByRole("listbox")).not.toBeInTheDocument();
  });

  it("keeps the text around a mention that is completed mid-sentence", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer();
    await user.type(textarea, "bitte @mi{Tab}pruefen{Enter}");
    expect(textarea).toHaveValue("");
    expect(onSend).toHaveBeenCalledWith("bitte @mia pruefen");
  });

  it("shows the queued note only while a round is running", async () => {
    const { unmount } = render(<GroupComposer members={MEMBERS} onSend={vi.fn()} />);
    expect(screen.queryByText("Will reach the group with the next round.")).not.toBeInTheDocument();
    unmount();
    render(<GroupComposer members={MEMBERS} onSend={vi.fn()} roundRunning />);
    expect(screen.getByText("Will reach the group with the next round.")).toBeInTheDocument();
  });

  it("still allows sending while a round is running", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer({ roundRunning: true });
    await user.type(textarea, "dazwischen{Enter}");
    expect(onSend).toHaveBeenCalledWith("dazwischen");
  });

  it("disables the send button while empty and enables it once there is text", async () => {
    const user = userEvent.setup({ delay: null });
    const { textarea } = renderComposer();
    const button = screen.getByRole("button", { name: /Message the group/ });
    expect(button).toBeDisabled();
    await user.type(textarea, "hi");
    expect(button).toBeEnabled();
    await user.click(button);
    expect(textarea).toHaveValue("");
  });

  it("refuses to send while a send is already in flight", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer({ sending: true });
    await user.type(textarea, "nochmal{Enter}");
    expect(onSend).not.toHaveBeenCalled();
    expect(screen.getByRole("button", { name: /Message the group/ })).toBeDisabled();
  });

  it("does not send when the composer is disabled", async () => {
    const user = userEvent.setup({ delay: null });
    const { onSend, textarea } = renderComposer({ disabled: true });
    expect(textarea).toBeDisabled();
    await user.type(textarea, "hallo{Enter}");
    expect(onSend).not.toHaveBeenCalled();
  });
});
