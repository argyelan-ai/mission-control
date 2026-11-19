"use client";

/**
 * The directory ("Telefonbuch", ADR-100 §4, stage 2d): who can be talked to
 * across harnesses, opened as a sheet from the chat header ("3 reachable").
 * No new page, no new menu entry (PRINCIPLES §8 rule 1).
 *
 * "Alive" and "listening" are two words on purpose and come from the one
 * presence source (§6 rule 2): a paused agent keeps sending signs of life,
 * so one green dot would say it listens when it does not. This sheet only
 * shows what the backend said — it decides neither word.
 */
import { useLocale, useTranslations } from "next-intl";
import { X } from "lucide-react";
import { C, STATUS_TEXT } from "@/lib/colors";
import { timeAgo } from "@/lib/utils";
import { isReachable, reachableCount, type Directory, type DirectoryRow } from "@/lib/comms";
import { Pill } from "@/components/shared/Pill";
import { ResponsiveModal } from "@/components/shared/ResponsiveModal";

function AliveChip({ row }: { row: DirectoryRow }) {
  const t = useTranslations("comms");
  if (row.alive === "yes") return <Pill color={STATUS_TEXT.online} casing="sentence">{t("alive")}</Pill>;
  if (row.alive === "no") return <Pill color={C.textMuted} casing="sentence">{t("notAlive")}</Pill>;
  return <Pill color={C.textMuted} casing="sentence">{t("aliveUnknown")}</Pill>;
}

function ListeningChip({ row }: { row: DirectoryRow }) {
  const t = useTranslations("comms");
  // null = not reported yet: no chip rather than a guess (K3).
  if (row.listening === true) return <Pill color={STATUS_TEXT.online} casing="sentence">{t("listening")}</Pill>;
  if (row.listening === false) return <Pill color={STATUS_TEXT.warning} casing="sentence">{t("notListening")}</Pill>;
  return null;
}

/** Second line: what the address does not already say. A session's address
 *  is harness@machine:folder, so its harness and machine are in it. */
function identityExtras(row: DirectoryRow, guest: string): string[] {
  const out: string[] = [];
  if (row.harness && !row.address.startsWith(`${row.harness}@`)) out.push(row.harness);
  if (row.machine && !row.address.includes(`@${row.machine}`)) out.push(row.machine);
  if (row.kind === "session") out.push(guest);
  return out;
}

function Row({ row }: { row: DirectoryRow }) {
  const t = useTranslations("comms");
  const locale = useLocale();
  const extras = identityExtras(row, t("guest"));
  const doing: string[] = [];
  if (row.listening === false) doing.push(t("takesNoMessages"));
  else {
    doing.push(row.busy ? t("working") : t("idle"));
    if (row.task) doing.push(row.task);
    if (row.accept === "hold") doing.push(t("holdsMessages"));
    if (row.accept === "refuse") doing.push(t("refusesMessages"));
  }
  doing.push(row.last_seen ? t("seen", { ago: timeAgo(row.last_seen, locale) }) : t("neverSeen"));

  return (
    <li
      data-testid="directory-row"
      data-reachable={isReachable(row) ? "true" : "false"}
      className="py-3 flex flex-col gap-1"
      style={{ borderTop: `1px solid ${C.borderSubtle}` }}
    >
      <div className="flex items-start gap-3">
        <span className="flex-1 min-w-0 truncate text-sm font-semibold" style={{ color: C.textPrimary }}>
          {row.label}
        </span>
        <span className="flex items-center gap-2 shrink-0">
          <AliveChip row={row} />
          <ListeningChip row={row} />
        </span>
      </div>
      <div className="flex items-baseline gap-2 min-w-0 text-xs" style={{ color: C.textMuted }}>
        <span className="font-mono truncate min-w-0">{row.address}</span>
        {extras.length > 0 && <span className="shrink-0">· {extras.join(" · ")}</span>}
      </div>
      <p className="m-0 text-xs break-words" style={{ color: C.textSecondary }}>
        {doing.join(" · ")}
      </p>
    </li>
  );
}

export function DirectorySheet({
  open,
  onClose,
  directory,
  error = false,
}: {
  open: boolean;
  onClose: () => void;
  directory: Directory | undefined;
  error?: boolean;
}) {
  const t = useTranslations("comms");
  const rows = directory?.sessions ?? [];
  // Reachable first, then by address — the backend's own order inside each.
  const sorted = [...rows].sort((a, b) => Number(isReachable(b)) - Number(isReachable(a)));

  return (
    <ResponsiveModal open={open} onClose={onClose} aria-labelledby="directory-title">
      <div data-testid="directory-sheet" className="flex flex-col px-4 pt-4 pb-4 sm:px-6 overflow-y-auto">
        <div className="flex items-center gap-3 pb-3">
          <h2 id="directory-title" className="m-0 flex-1 text-base font-semibold" style={{ color: C.textPrimary }}>
            {t("directoryTitle")}
            {rows.length > 0 && (
              <span className="ml-2 text-sm font-normal tabular-nums" style={{ color: C.textMuted }}>
                {t("directoryOf", { reachable: reachableCount(directory), total: rows.length })}
              </span>
            )}
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label={t("close")}
            className="-mr-2 flex items-center justify-center w-11 h-11 rounded-full cursor-pointer bg-transparent border-0"
            style={{ color: C.textSecondary }}
          >
            <X size={18} aria-hidden />
          </button>
        </div>

        {directory?.warming_up && (
          <p className="m-0 pb-3 text-xs" style={{ color: C.textMuted }} data-testid="directory-warming">
            {t("warmingUp")}
          </p>
        )}

        {error && rows.length === 0 ? (
          <p className="m-0 py-6 text-sm" style={{ color: C.textMuted }}>
            {t("directoryLoadFailed")}
          </p>
        ) : rows.length === 0 ? (
          <div className="py-6 flex flex-col gap-1" data-testid="directory-empty">
            <p className="m-0 text-sm font-medium" style={{ color: C.textSecondary }}>
              {t("directoryEmpty")}
            </p>
            <p className="m-0 text-sm" style={{ color: C.textMuted }}>
              {t("directoryEmptyHint")}
            </p>
          </div>
        ) : (
          <ul className="m-0 p-0 list-none">
            {sorted.map((row) => (
              <Row key={row.address} row={row} />
            ))}
          </ul>
        )}
      </div>
    </ResponsiveModal>
  );
}

/**
 * "3 reachable" in the chat header — opens the directory. Hidden while the
 * directory is unknown (loading, or this user may not read it): the header
 * never shows an empty or invented number (K3).
 */
export function ReachableButton({
  directory,
  onOpen,
  className,
}: {
  directory: Directory | undefined;
  onOpen: () => void;
  className?: string;
}) {
  const t = useTranslations("comms");
  if (!directory) return null;
  const count = reachableCount(directory);
  return (
    <button
      type="button"
      onClick={onOpen}
      data-testid="reachable-button"
      aria-label={t("reachableAria", { count })}
      className={`flex items-center gap-2 bg-transparent border-0 p-0 cursor-pointer text-xs ${className ?? ""}`}
      style={{ color: C.textMuted }}
    >
      <span
        aria-hidden
        className="inline-block w-1.5 h-1.5 rounded-full shrink-0"
        style={{ background: count > 0 ? C.online : C.textDim }}
      />
      <span className="tabular-nums">{t("reachable", { count })}</span>
    </button>
  );
}
