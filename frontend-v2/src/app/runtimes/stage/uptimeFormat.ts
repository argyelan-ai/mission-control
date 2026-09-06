/**
 * uptimeFormat — Minuten-genaue Laufzeit für die Bühnen-Ecke (Spec §2
 * "Laufzeit in Mono", PR #443/W3-Nachlese).
 *
 * HONESTY RULE (wie Stage.tsx's Kopfkommentar): nur aus einem echten
 * Zeitstempel (`RuntimeLiveStatus.serving_since` / `Runtime.serving_since`,
 * beide vom Server gesetzt/gelöscht — nie clientseitig abgeleitet). Fehlt der
 * Wert, ist er nicht parsbar, oder liegt er in der Zukunft (Client-/Server-
 * Uhr-Drift), liefert diese Funktion `null` — der Aufrufer fällt dann auf das
 * Zustandswort zurück ("serving"/"läuft"), nie auf eine erfundene Zahl.
 */
export interface UptimeParts {
  hours: number;
  minutes: number;
}

export function formatUptimeParts(
  sinceIso: string | null | undefined,
  now: number = Date.now()
): UptimeParts | null {
  if (!sinceIso) return null;
  const since = Date.parse(sinceIso);
  if (Number.isNaN(since)) return null;
  const deltaMs = now - since;
  if (deltaMs < 0) return null;
  const totalMinutes = Math.floor(deltaMs / 60_000);
  return { hours: Math.floor(totalMinutes / 60), minutes: totalMinutes % 60 };
}

/** Zwei Stellen, wie im Spec-Beispiel „down 0 h 03" — reine Kosmetik für die
 *  Monospace-Spalte, keine Rundung. */
export function pad2(n: number): string {
  return String(n).padStart(2, "0");
}
