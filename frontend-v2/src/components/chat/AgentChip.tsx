"use client";

/**
 * AgentChip — who a new chat goes to (concept "Neuer Chat", P4).
 *
 * "New chat" opens an empty draft at once; this quiet chip above the
 * composer says who answers: "With Sparky ▾". Its menu (a bottom sheet on the
 * phone, a popover on the desktop) has two sections:
 *
 *   New chat       alive agents that can hold a further chat — a tap makes
 *                  that agent the draft's agent; the chat starts on send.
 *   Only one chat  alive agents with just their own chat, dimmed — a tap
 *                  opens that chat ("opens your chat with Boss").
 *
 * and "New group" at the bottom. Dead and archived agents, head runs and the
 * group archive are not here (they live in the chat list and settings).
 *
 * While new chats are off (record flag off, or the viewer is no admin) the
 * chip says so before anything is typed: "New chats are off — your chat with
 * Sparky opens."
 */
import { useEffect, useRef, useState } from "react";
import { useTranslations } from "next-intl";
import { Check, ChevronDown, Users, X } from "lucide-react";
import { C } from "@/lib/colors";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";
import { PopoverPortal, anchoredBoxStyle, useAnchoredPosition } from "@/lib/anchoredPopover";
import { usePhoneViewport } from "@/lib/chatHistory";
import type { NewChatAgent, NewChatChoices } from "@/lib/chatList";

export interface AgentChipProps {
  /** The draft's agent; null while nobody alive can be offered. */
  agent: NewChatAgent | null;
  choices: NewChatChoices;
  /** New chats can be made (record on and the viewer is an admin). */
  newChatsOn: boolean;
  /** A "New chat" row: this agent answers the draft. */
  onPick: (agentId: string) => void;
  /** An "Only one chat" row: open that agent's own chat. */
  onOpenOwnChat: (agentId: string) => void;
  onNewGroup: () => void;
}

/** The chip's sentence: who answers, or honestly why it is the old chat. */
export function agentChipLabel(
  t: (key: string, values?: Record<string, string>) => string,
  agent: NewChatAgent | null,
  choices: NewChatChoices,
  newChatsOn: boolean,
): string {
  if (!agent) return t("agentChipNone");
  if (!newChatsOn) return t("newChatOff", { name: agent.name });
  if (choices.canStart.some((a) => a.id === agent.id)) return t("agentChipWith", { name: agent.name });
  return t("agentChipOwnChat", { name: agent.name });
}

export function AgentChip({ agent, choices, newChatsOn, onPick, onOpenOwnChat, onNewGroup }: AgentChipProps) {
  const t = useTranslations("cockpit");
  const phone = usePhoneViewport();
  const [open, setOpen] = useState(false);
  const chipRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const popoverOpen = open && !phone;
  const menuStyle = useAnchoredPosition(popoverOpen, chipRef, menuRef, { side: "top", align: "start", offset: 4 });

  // Desktop popover: a click elsewhere or Esc closes it (the sheet has its own).
  useEffect(() => {
    if (!popoverOpen) return;
    function onPointerDown(e: PointerEvent) {
      const target = e.target as Node;
      if (!chipRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [popoverOpen]);

  const label = agentChipLabel(t, agent, choices, newChatsOn);
  const close = () => setOpen(false);

  const menu = (
    <div data-testid="agent-chip-menu" className="flex flex-col p-2">
      {choices.canStart.length > 0 && (
        <section aria-label={t("agentChipSectionNew")} data-testid="agent-chip-section-new">
          <h3 className="label-sys px-2 pt-2 pb-1" style={{ color: C.textMuted }}>
            {t("agentChipSectionNew")}
          </h3>
          {choices.canStart.map((a) => {
            const current = a.id === agent?.id;
            return (
              <button
                key={a.id}
                type="button"
                role="menuitemradio"
                aria-checked={current}
                data-testid="agent-chip-row"
                data-agent={a.id}
                onClick={() => {
                  close();
                  onPick(a.id);
                }}
                className="w-full flex items-center gap-2 px-2 min-h-touch rounded-md text-sm text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
                style={{ color: C.textPrimary }}
              >
                <span className="flex-1 min-w-0 truncate">{a.name}</span>
                {current && <Check size={15} aria-hidden="true" style={{ color: C.accent }} />}
              </button>
            );
          })}
        </section>
      )}
      {choices.onlyOne.length > 0 && (
        <section aria-label={t("agentChipSectionOnlyOne")} data-testid="agent-chip-section-only-one" className="pt-2">
          <h3 className="label-sys px-2 pt-2 pb-1" style={{ color: C.textMuted }}>
            {t("agentChipSectionOnlyOne")}
          </h3>
          {choices.onlyOne.map((a) => (
            <button
              key={a.id}
              type="button"
              role="menuitem"
              data-testid="agent-chip-row-dimmed"
              data-agent={a.id}
              onClick={() => {
                close();
                onOpenOwnChat(a.id);
              }}
              className="w-full flex flex-col items-start justify-center px-2 py-2 min-h-touch rounded-md text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
            >
              <span className="text-sm truncate max-w-full" style={{ color: C.textSecondary }}>
                {a.name}
              </span>
              <span className="text-xs truncate max-w-full" style={{ color: C.textMuted }}>
                {t("agentChipOpensChat", { name: a.name })}
              </span>
            </button>
          ))}
        </section>
      )}
      {choices.canStart.length + choices.onlyOne.length === 0 && (
        <p className="px-2 py-2 text-sm" style={{ color: C.textMuted }}>
          {t("agentChipNone")}
        </p>
      )}
      <button
        type="button"
        role="menuitem"
        data-testid="agent-chip-new-group"
        onClick={() => {
          close();
          onNewGroup();
        }}
        className="w-full flex items-center gap-2 px-2 mt-2 min-h-touch rounded-md text-sm text-left cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textSecondary, borderTop: `1px solid ${C.borderSubtle}` }}
      >
        <Users size={15} aria-hidden="true" style={{ color: C.textMuted }} />
        {t("agentChipNewGroup")}
      </button>
    </div>
  );

  return (
    <div data-testid="agent-chip" className="flex min-w-0 px-1 pb-1">
      <button
        ref={chipRef}
        type="button"
        data-testid="agent-chip-button"
        data-mode={!agent ? "none" : !newChatsOn ? "off" : choices.canStart.some((a) => a.id === agent.id) ? "new" : "own"}
        aria-haspopup={phone ? "dialog" : "menu"}
        aria-expanded={open}
        aria-label={t("agentChipChange", { label })}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 min-w-0 max-w-full min-h-touch px-2 -mx-1 rounded-md text-sm cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)]"
        style={{ color: C.textSecondary }}
      >
        <span className="truncate">{label}</span>
        <ChevronDown size={14} aria-hidden="true" className="shrink-0" style={{ color: C.textMuted }} />
      </button>

      {phone ? (
        <ResponsiveModal open={open} onClose={close} aria-labelledby="agent-chip-title">
          <div className="flex flex-col min-h-0 pt-3 pb-2">
            <div className="flex items-center justify-between px-4">
              <h2 id="agent-chip-title" className="text-base font-semibold" style={{ color: C.textPrimary }}>
                {t("agentChipTitle")}
              </h2>
              <button
                type="button"
                onClick={close}
                aria-label={t("agentChipClose")}
                className="flex items-center justify-center w-11 h-11 -mr-2 rounded-md cursor-pointer"
                style={{ color: C.textMuted }}
              >
                <X size={17} aria-hidden="true" />
              </button>
            </div>
            <div className="overflow-y-auto min-h-0 px-2">{menu}</div>
          </div>
        </ResponsiveModal>
      ) : (
        popoverOpen && (
          <PopoverPortal>
            <div
              ref={menuRef}
              role="menu"
              aria-label={t("agentChipTitle")}
              className="w-72 rounded-lg overflow-y-auto z-50"
              style={{
                ...anchoredBoxStyle(menuStyle),
                backgroundColor: C.bgElevated,
                border: `1px solid ${C.border}`,
                boxShadow: "var(--shadow-elevated)",
              }}
            >
              {menu}
            </div>
          </PopoverPortal>
        )
      )}
    </div>
  );
}
