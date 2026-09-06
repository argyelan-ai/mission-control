"use client";

/**
 * AutostartGroup — Cockpit-Gruppe „Autostart" (Spec §4.3). Schalter (Host-
 * Autostart, `PUT /hosts/{id}/autostart`) + Modellname (Mono), darunter EINE
 * Zeile mit dem letzten Versuch.
 *
 * Abweichung von der Spec-Formulierung ("Zeitleiste der letzten 3 Start-
 * Ereignisse"), dokumentiert im Team-Auftrag: `ActivityEvent` (routers/
 * activity.py, lib/types.ts) trägt kein `host_id`-Feld — es gibt keinen
 * Filter, mit dem sich "die letzten 3 Ereignisse dieser Box" bilden liessen,
 * ohne eine Korrelation zu raten, die HONESTY RULE verbietet. Die Fusszeile
 * fällt darum auf `HostAutostartStatus.last_attempt_at`/`last_result` zurück
 * (eine Zeile) — exakt der Fallback, den der Auftrag für diesen Fall nennt.
 * Der Log-Pfad aus dem Mockup entfällt ganz: kein Backend-Feld trägt ihn.
 *
 * Team-Lead-Fund 06.09.2026 (Live-Sichtprüfung): `last_result` ist ein ganzer
 * Meldungssatz vom Backend ("Gestartet — DeepSeek … Gewichte laden dauert …
 * Logs: ~/.cache/…") — genau der Meldungstext, den die Spec für die Karte
 * verbietet und der auch im Cockpit zu lang ist. Die Zeile zeigt jetzt NUR
 * Zeit + ein Ergebnis-Wort (ok/failed), das aus `last_result` per
 * Schlüsselwort erkannt wird — kein Fliesstext, kein Log-Pfad. Lässt sich
 * kein sauberes Wort erkennen, bleibt nur die Zeit stehen.
 *
 * Wiederverwendet dieselbe Query/Mutation/No-Optimistic-Update-Regel wie
 * `HostAutostartRow.tsx` (Karten-Fusszeile) — ein Datenweg, zwei Ansichten.
 */

import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Loader2, Power } from "lucide-react";
import { useTranslations } from "next-intl";
import { humanApiError } from "@/components/shared/HostRecipeSwitcher";
import { api } from "@/lib/api";
import { C, STATUS, STATUS_TEXT } from "@/lib/colors";
import { hostAutostartKey } from "../../HostAutostartRow";

/** Mono, locale-unabhängig — "09/05 07:47" statt eines lokalisierten Satzes
 *  mit Komma/AM-PM (der Meldungstext, den die Spec verbietet). */
function compactWhen(iso: string): string {
  const d = new Date(iso);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getMonth() + 1)}/${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

const FAIL_RE = /fail|error|fehler|gescheitert|abgebrochen|exit \d/i;
const OK_RE = /\bok\b|started|gestartet|ready|bereit|success|erfolgreich/i;

/** Ein Wort aus dem Meldungssatz erkennen, nie ihn zeigen. `null` = kein
 *  sauberer Status erkennbar — dann bleibt nur die Zeit stehen. */
function resultWord(result: string): "ok" | "failed" | null {
  if (FAIL_RE.test(result)) return "failed";
  if (OK_RE.test(result)) return "ok";
  return null;
}

export function AutostartGroup({ hostId, isAdmin }: { hostId: string; isAdmin: boolean }) {
  const t = useTranslations("runtimes.hostAutostart");
  const tCockpit = useTranslations("runtimes.cockpit");
  const queryClient = useQueryClient();
  const [error, setError] = useState<string | null>(null);

  const statusQuery = useQuery({
    queryKey: hostAutostartKey(hostId),
    queryFn: () => api.hosts.autostart(hostId),
    staleTime: 15_000,
    retry: false,
  });
  const status = statusQuery.data ?? null;

  const mutation = useMutation({
    mutationFn: (enabled: boolean) =>
      api.hosts.setAutostart(hostId, {
        enabled,
        ...(status?.recipe_slug ? { recipe_slug: status.recipe_slug } : {}),
      }),
    onMutate: () => setError(null),
    onSuccess: (next) => {
      queryClient.setQueryData(hostAutostartKey(hostId), next);
      queryClient.invalidateQueries({ queryKey: ["hosts"], exact: true });
    },
    onError: (err: Error) => setError(t("saveFailed", { message: humanApiError(err) })),
  });

  if (status?.via_head) {
    return (
      <div data-testid="autostart-group-via-head" className="flex flex-col gap-1">
        <span
          className="inline-flex items-center gap-1 rounded-sm px-1.5 py-0.5 text-[10px] font-medium self-start"
          style={{ color: C.textSecondary, border: `1px solid ${C.borderActive}` }}
        >
          <Power size={10} aria-hidden />
          {t("viaHead", { slug: status.via_head.slug })}
        </span>
        <span className="text-[11px]" style={{ color: C.textMuted }}>{t("viaHeadHint")}</span>
      </div>
    );
  }

  const loading = statusQuery.isLoading;
  const unknown = !loading && (status == null || statusQuery.isError);
  const enabled = status?.enabled === true;
  const busy = loading || mutation.isPending;
  const recipeName = status?.recipe_display_name ?? status?.recipe_slug ?? null;

  const attemptWhen = status?.last_attempt_at ? compactWhen(status.last_attempt_at) : null;
  const attemptWord = status?.last_result ? resultWord(status.last_result) : null;

  return (
    <div data-testid="autostart-group" className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-2">
        <button
          type="button"
          role="switch"
          aria-checked={unknown ? false : enabled}
          aria-label={t("ariaLabel")}
          data-testid="cockpit-autostart-switch"
          data-state={unknown ? "unknown" : enabled ? "on" : "off"}
          title={!isAdmin ? t("adminOnly") : undefined}
          disabled={unknown || busy || !isAdmin}
          onClick={() => mutation.mutate(!enabled)}
          className="inline-flex items-center gap-1.5 rounded-sm px-2 py-1 min-h-11 sm:min-h-7 text-[11px] font-medium leading-none"
          style={{
            border: `1px solid ${!unknown && enabled ? `${STATUS.online}40` : C.borderActive}`,
            color: unknown ? C.textDim : enabled ? STATUS_TEXT.online : C.textMuted,
            cursor: unknown || busy || !isAdmin ? "not-allowed" : "pointer",
            opacity: busy ? 0.6 : 1,
          }}
        >
          {busy ? <Loader2 size={10} className="animate-spin" aria-hidden /> : <Power size={10} aria-hidden />}
          {loading ? t("loading") : unknown ? t("statusUnknown") : enabled ? t("statusOn") : t("statusOff")}
        </button>
        <span className="font-mono text-[12px] truncate" style={{ color: C.textSecondary }} data-testid="cockpit-autostart-model">
          {recipeName ?? tCockpit("autostartNoRecipe")}
        </span>
      </div>

      {attemptWhen && (
        <p className="font-mono text-[11px]" style={{ color: C.textDim }} data-testid="cockpit-autostart-last-attempt">
          {attemptWord
            ? tCockpit("lastAttempt", { when: attemptWhen, status: tCockpit(attemptWord === "ok" ? "resultOk" : "resultFailed") })
            : tCockpit("lastAttemptTimeOnly", { when: attemptWhen })}
        </p>
      )}

      {error && (
        <p className="text-[11px]" style={{ color: STATUS_TEXT.error }} role="alert" data-testid="cockpit-autostart-error">
          {error}
        </p>
      )}
    </div>
  );
}
