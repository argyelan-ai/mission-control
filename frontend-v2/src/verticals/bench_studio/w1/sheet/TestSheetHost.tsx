"use client";

// CONTRACT STUB (bench W1, 24.10.2026) — owned by package B (test sheet P3).
//
// Mounted once by BenchStudioPage. Reads the sheet request from the URL
// (lib/benchLinks.parseBenchTestParams: `/bench?test=1&runtime=…`), loads
// GET /bench/plan, shows the phone bottom sheet / desktop side panel, and on
// Start calls benchW1Api.tests.create, then navigates to
// benchResultHref(test.id). Closing removes the sheet params from the URL.
// Replaces NewChallengeDialog (no second test dialog).
//
// Signature is frozen: no props. The stub renders nothing.
export function TestSheetHost(): null {
  return null;
}
