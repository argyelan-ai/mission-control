"use client";

/**
 * WakeBoxButton — "Wake" for a power-managed box, keyed by its HOST.
 *
 * Round-5 re-check (L1): the box's power_managed flag lives on the host row.
 * A box whose runtime rows lack it (or that has none) slept with no way to
 * wake it — waking existed only per runtime. This button goes to
 * POST /hosts/{id}/wake, the same Wake-on-LAN path, and is used by every
 * place that shows a sleeping box (sleeping card, free card, cockpit).
 *
 * After a sent wake the button stays off for the time a box needs to boot
 * and the line says so — the card itself keeps saying "asleep" until the
 * box's own check answers, so a second click would only send a second wake.
 * The "sent" moment lives in the query cache (one key per host), so the card
 * and the cockpit of the same box agree — no "Wake" in one while the other
 * says "Waking…".
 * A viewer only looks: waking a box is an operator action (403 otherwise).
 */

import { useEffect } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useTranslations } from "next-intl";
import { Loader2, Power } from "lucide-react";
import { api } from "@/lib/api";
import { C, STATUS_TEXT } from "@/lib/colors";
import { useAppStore } from "@/lib/store";
import type { Host } from "@/lib/types";

/** How long a sent wake keeps the button off: a box needs ~1–2 min to boot. */
export const WAKE_SENT_MS = 180_000;

export function WakeBoxButton({ host }: { host: Host }) {
  const t = useTranslations("runtimes.wakeBox");
  const tStage = useTranslations("runtimes.stage");
  const tRuntimes = useTranslations("runtimes");
  const role = useAppStore((s) => s.currentUser?.role);
  const queryClient = useQueryClient();
  // Outside the ["hosts"] prefix: invalidating host data must not touch it.
  const sentKey = ["wake-sent", host.id];
  const { data: sentAt } = useQuery<number | null>({
    queryKey: sentKey,
    queryFn: () => null,
    enabled: false,
    initialData: null,
    staleTime: Infinity,
  });
  const setSentAt = (v: number | null) => queryClient.setQueryData(sentKey, v);

  useEffect(() => {
    if (sentAt == null) return;
    const id = setTimeout(() => setSentAt(null), Math.max(0, WAKE_SENT_MS - (Date.now() - sentAt)));
    return () => clearTimeout(id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sentAt]);

  const wake = useMutation({
    mutationFn: () => api.hosts.wake(host.id),
    onSuccess: () => {
      setSentAt(Date.now());
      // The backend forgot the remembered "asleep": the next check asks the box.
      queryClient.invalidateQueries({ queryKey: ["hosts", host.id, "metrics"] });
      queryClient.invalidateQueries({ queryKey: ["runtimes"] });
    },
  });

  if (!host.power_managed || role === "viewer") return null;

  const sent = sentAt != null;
  return (
    <div className="flex items-center gap-2 min-w-0">
      <button
        type="button"
        onClick={() => wake.mutate()}
        disabled={wake.isPending || sent}
        data-testid="wake-box"
        className="flex items-center gap-1.5 text-xs px-3.5 min-h-11 rounded-md cursor-pointer shrink-0 disabled:opacity-50"
        style={{ border: `1px solid ${C.borderActive}`, color: C.textSecondary }}
      >
        {wake.isPending ? <Loader2 size={12} className="animate-spin" /> : <Power size={12} />}
        {sent ? t("waking") : tStage("wake")}
      </button>
      {sent && (
        <span className="text-xs min-w-0" style={{ color: C.textMuted }} data-testid="wake-sent">
          {t("sent")}
        </span>
      )}
      {wake.isError && !sent && (
        <span className="text-xs min-w-0" style={{ color: STATUS_TEXT.error }} data-testid="wake-failed" role="alert">
          {tRuntimes("wakeFailed")}
        </span>
      )}
    </div>
  );
}
