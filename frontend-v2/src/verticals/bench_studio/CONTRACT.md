# Bench W1 contract — frontend side

The full contract (routes, ownership, rules, done criteria) lives next to
the backend vertical: `backend/app/verticals/bench_studio/CONTRACT.md`.

Frontend pieces it freezes:

- `w1/types.ts` — mirror of `w1_schemas.py`
- `w1/api.ts` — `benchW1Api`, `benchW1Keys`, `LIVE_POLL_MS`, `parseCostConfirmation`
- `w1/fixtures.ts` — GENERATED from the backend fixtures (do not edit)
- component signatures: `w1/sheet/TestSheetHost.tsx` (B), `w1/live/LiveRun.tsx` (B),
  `w1/result/ResultPage.tsx` (C), `w1/kit/XKitSheet.tsx` (C), `w1/list/TestList.tsx` (D)
- `src/lib/benchLinks.ts` — the only door from core pages into the bench
- i18n: `benchShared` (lead), `benchSheet` (B), `benchResult` (C), `benchHome` (D)
