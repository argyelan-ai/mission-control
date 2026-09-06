/** Ein Schlüssel für alle Leser dieser Box (Cockpit-Gruppen: AutostartGroup,
 *  RecipeGroup).
 *
 * Extracted from the now-removed `HostAutostartRow.tsx` (PR 6 "Schliff", v1
 * retired) — that component's own switch+subtitle UI is superseded by the
 * Cockpit's `AutostartGroup` (Spec §4.3), but the query key itself is still
 * shared by two cockpit groups, so it needed a home that isn't a component
 * file. */
export const hostAutostartKey = (hostId: string) => ["hosts", hostId, "autostart"] as const;
