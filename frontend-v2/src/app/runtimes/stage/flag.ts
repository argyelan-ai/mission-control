/**
 * Runtimes-Bühne v2 — Schalter (Spec §7, PR 4 "Stage").
 *
 * Lebt in einer eigenen Datei statt als Export aus `page.tsx`: Next.js' App
 * Router prüft ein `page.tsx`-Modul gegen einen festen Satz erlaubter Exports
 * (`default`, `metadata`, …) — ein zusätzlicher benannter Export dort schlägt
 * `tsc` mit "Property 'X' is incompatible with index signature" fehl.
 */
export const RUNTIMES_STAGE_V2 = process.env.NEXT_PUBLIC_RUNTIMES_STAGE === "v2";
