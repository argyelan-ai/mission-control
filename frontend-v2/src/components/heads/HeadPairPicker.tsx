"use client";

/**
 * Pair picker — harness × runtime for a head (docs/specs/head-launcher.md
 * §8.1). One component for "New task" and "Restart with …".
 *
 *   [ ● omp · GLM local · live                                ▾ ]
 *     ● omp · GLM local            costs no Claude quota · default
 *     ● Claude Code · GLM local    costs no Claude quota · test
 *     Show more (3)
 *       omp · Qwen local     The model is not running. Start it on Runtimes.
 *
 * Only startable pairs show by default; the blocked rest sits behind
 * "Show more", greyed, each with one plain sentence (text from i18n, the
 * backend only sends a reason CODE). The list opens inline (no portal) so it
 * never clips inside the modal's scroll area or the mobile bottom sheet.
 */

import { useId, useRef, useState } from "react";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { ChevronDown } from "lucide-react";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { pairKey, pairLabel, pairReasonKey, splitPairs, type HeadPair } from "@/lib/heads";

function PairDot({ pair }: { pair: HeadPair }) {
  const color = pair.startable && pair.live ? STATUS.online : pair.live ? STATUS.warning : STATUS.offline;
  return <span aria-hidden className="w-2 h-2 rounded-full shrink-0" style={{ background: color }} />;
}

export function usePairReason() {
  const t = useTranslations("heads");
  return (pair: HeadPair) =>
    t(pairReasonKey(pair.reason_code), { title: pair.busy_by?.title ?? "…" });
}

export function HeadPairPicker({
  pairs,
  selected,
  defaultKey,
  onSelect,
  disabled = false,
  showHints = true,
}: {
  pairs: HeadPair[];
  selected: HeadPair | null;
  /** Key of the backend's default pair — gets the "default" tag. */
  defaultKey?: string | null;
  onSelect: (pair: HeadPair) => void;
  disabled?: boolean;
  /** Explanation + engine hints under the field (off in compact places). */
  showHints?: boolean;
}) {
  const t = useTranslations("heads");
  const reasonText = usePairReason();
  const [open, setOpen] = useState(false);
  const [showMore, setShowMore] = useState(false);
  const listId = useId();
  const labelId = useId();
  const triggerId = useId();
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLUListElement>(null);

  const close = () => {
    setOpen(false);
    triggerRef.current?.focus();
  };

  // Keyboard: Esc closes only the list (never the surrounding modal),
  // ↑/↓ move between the startable options, Home/End jump.
  const onKeyDown = (e: React.KeyboardEvent) => {
    if (!open) {
      if (e.key === "ArrowDown" && e.target === triggerRef.current) {
        e.preventDefault();
        setOpen(true);
      }
      return;
    }
    if (e.key === "Escape") {
      e.preventDefault();
      e.stopPropagation();
      close();
      return;
    }
    if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) return;
    const options = Array.from(
      listRef.current?.querySelectorAll<HTMLButtonElement>('[role="option"]:not([disabled])') ?? [],
    );
    if (options.length === 0) return;
    e.preventDefault();
    const at = options.indexOf(document.activeElement as HTMLButtonElement);
    const next =
      e.key === "Home" ? 0
      : e.key === "End" ? options.length - 1
      : e.key === "ArrowDown" ? (at + 1) % options.length
      : at <= 0 ? options.length - 1 : at - 1;
    options[next].focus();
  };

  const selectedKey = selected ? pairKey(selected) : null;
  const { primary, more } = splitPairs(pairs, selectedKey);

  if (pairs.length === 0) {
    return (
      <p className="text-xs" style={{ color: C.textMuted }} data-testid="head-no-pairs">
        {t("noPairs")}
      </p>
    );
  }

  const meta = (p: HeadPair) => {
    const parts: string[] = [];
    if (p.locality === "local") parts.push(t("noQuotaShort"));
    if (defaultKey && pairKey(p) === defaultKey) parts.push(t("defaultTag"));
    if (p.status === "experimental") parts.push(t("experimentalTag"));
    return parts.join(" · ");
  };

  const row = (p: HeadPair) => {
    const key = pairKey(p);
    const isSelected = key === selectedKey;
    return (
      <li key={key} role="presentation">
        <button
          type="button"
          role="option"
          aria-selected={isSelected}
          aria-disabled={!p.startable}
          disabled={!p.startable}
          data-testid={`head-pair-option-${key}`}
          data-startable={p.startable ? "true" : "false"}
          onClick={() => {
            if (!p.startable) return;
            onSelect(p);
            close();
          }}
          className="w-full flex flex-col sm:flex-row sm:items-center gap-0.5 sm:gap-3 px-3 py-2 min-h-[44px] text-left rounded-md cursor-pointer transition-colors hover:bg-[var(--color-bg-hover)] disabled:cursor-not-allowed disabled:hover:bg-transparent"
          style={{
            background: isSelected ? C.accentSubtle : "transparent",
            opacity: p.startable ? 1 : 0.6,
          }}
        >
          <span className="flex items-center gap-2 min-w-0">
            <PairDot pair={p} />
            <span className="text-xs truncate" style={{ color: C.textPrimary }}>{pairLabel(p)}</span>
          </span>
          <span className="text-[11px] sm:ml-auto sm:text-right pl-4 sm:pl-0" style={{ color: C.textMuted }}>
            {p.startable ? meta(p) : reasonText(p)}
          </span>
        </button>
      </li>
    );
  };

  const selectedNote = selected
    ? selected.startable
      ? selected.live
        ? t("live")
        : t("notLive")
      : reasonText(selected)
    : "";

  return (
    <div className="space-y-1.5" data-testid="head-pair-picker" onKeyDown={onKeyDown}>
      <div className="flex flex-col sm:flex-row sm:items-center gap-1.5 sm:gap-3">
        <span id={labelId} className="label-sys shrink-0 sm:w-[88px]">{t("pair")}</span>
        <button
          type="button"
          disabled={disabled}
          aria-haspopup="listbox"
          aria-expanded={open}
          aria-controls={listId}
          id={triggerId}
          ref={triggerRef}
          // name = "Pair" + the current choice (select-button pattern)
          aria-labelledby={`${labelId} ${triggerId}`}
          data-testid="head-pair-trigger"
          onClick={() => setOpen((o) => !o)}
          className="flex-1 min-w-0 flex items-center gap-2 px-3 min-h-[44px] sm:min-h-[36px] rounded-md text-left cursor-pointer disabled:opacity-40 disabled:cursor-not-allowed"
          style={{ background: C.bgDeep, border: `1px solid ${C.border}` }}
        >
          {selected && <PairDot pair={selected} />}
          <span className="text-xs truncate" style={{ color: C.textPrimary }}>
            {selected ? pairLabel(selected) : "—"}
          </span>
          {selectedNote && (
            <span className="text-[11px] truncate" style={{ color: selected?.startable ? C.textMuted : STATUS_TEXT.warning }}>
              · {selectedNote}
            </span>
          )}
          <ChevronDown size={13} className="ml-auto shrink-0" style={{ color: C.textMuted, transform: open ? "rotate(180deg)" : "none" }} />
        </button>
      </div>

      {open && (
        <div className="sm:ml-[100px] rounded-lg p-1" style={{ background: C.bgSurface, border: `1px solid ${C.border}` }}>
          <ul id={listId} role="listbox" aria-labelledby={labelId} ref={listRef}>
            {primary.map(row)}
            {showMore && more.map(row)}
          </ul>
          {more.length > 0 && (
            <button
              type="button"
              onClick={() => setShowMore((s) => !s)}
              aria-expanded={showMore}
              data-testid="head-pair-show-more"
              className="w-full text-left px-3 min-h-[44px] sm:min-h-[32px] text-[11px] cursor-pointer hover:underline"
              style={{ color: C.textSecondary }}
            >
              {showMore ? t("showLess") : t("showMore", { count: more.length })}
            </button>
          )}
        </div>
      )}

      {showHints && selected && (
        <div className="sm:ml-[100px] space-y-1 text-[11px] leading-relaxed" style={{ color: C.textMuted }} data-testid="head-pair-hints">
          <p>
            {t("explain")} {selected.locality === "local" ? t("localNoQuota") : t("cloudCost")}
          </p>
          {selected.reason_code === "engine_not_ready" && (
            <p style={{ color: STATUS_TEXT.warning }} data-testid="head-engine-down">
              {t("engineDownHint")}{" "}
              <Link href="/runtimes" className="underline" style={{ color: C.textPrimary }}>
                {t("runtimesLink")}
              </Link>
            </p>
          )}
          {selected.startable && selected.engine_in_use && (
            <p style={{ color: STATUS_TEXT.warning }} data-testid="head-engine-in-use">{t("engineInUse")}</p>
          )}
        </div>
      )}
    </div>
  );
}
