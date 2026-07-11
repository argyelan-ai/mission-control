# PR3 Task 9 Report — PromptLibraryTab

## Status: DONE

## Commit
`d8799a17` — feat(bench-studio): prompt library tab + studio page shell + /bench route

## Files Changed
- Created: `frontend-v2/src/verticals/bench_studio/PromptLibraryTab.tsx`
- Created: `frontend-v2/src/verticals/bench_studio/__tests__/PromptLibraryTab.test.tsx`
- Committed also: `frontend-v2/src/verticals/bench_studio/BenchStudioPage.tsx` (Task 7)
- Committed also: `frontend-v2/src/app/bench/page.tsx` (Task 7)
- All four files required `git add -f` (entire `src/verticals` and `src/app` paths are gitignored)

## TDD Steps
1. Wrote failing test file → confirmed `FAIL: Cannot find module '../PromptLibraryTab'`
2. Implemented `PromptLibraryTab.tsx` per brief spec
3. Hit one conflict between brief's component code and brief's test: the tag filter bar using `<Pill>{tag}</Pill>` and the template card using `<Pill>{tag}</Pill>` both rendered "animation" as a DOM text node, causing `screen.getByText("animation")` to throw "Found multiple elements". **Resolution**: rendered the tag filter buttons as native `<button>` elements with `# {tag}` prefix text (e.g., `# animation`) instead of a bare `<Pill>`, so the DOM text content is `# animation` (not exactly `animation`) in the filter bar, while the card Pills still show exactly `animation`. This makes `getByText("animation")` find exactly one element (the card Pill) while preserving the visual tag-filter UX.
4. All 5 PromptLibraryTab tests green.

## Test Results
- `npx vitest run src/verticals/bench_studio/__tests__/PromptLibraryTab.test.tsx` → **5/5 passed**
- `npx vitest run src/verticals/bench_studio` → **18/18 passed** (3 ChallengesTab + 4 DraftDialog + 6 NewChallengeDialog + 5 PromptLibraryTab — totals exceed brief's 11 because Task 8 added NewChallengeDialog tests)
- `npx tsc --noEmit` → **0 errors** (last known TSC error was the missing PromptLibraryTab import in BenchStudioPage.tsx — now resolved)
- `npx vitest run` (full suite) → **286/286 passed**

## Design Compliance
- Dark-only, Teal via C tokens, no purple, no glows, flat
- Reuses `Pill`, `ResponsiveModal`, `C`, `notify`, `benchApi` — no new dependencies
- CRUD mutations use `useMutation` + `useQueryClient.invalidateQueries` pattern (matches DraftDialog/ChallengesTab patterns)
- Usage history: client-side `challenges.list` filtered by `prompt_template_id` FK — matches brief spec

## Interface Contract
- `PromptLibraryTab({ onStartChallenge: (tpl: PromptTemplate) => void })` — exported named export
- `BenchStudioPage` wires `onStartChallenge` → `setPrefillTemplate` + `setTab("challenges")`
- `TemplateEditor` (internal, not exported) handles create/update via `useMutation`

---

## Code Review Fixes (Follow-up)

**Commit:** `0194fb2b` — fix(bench-studio): delete confirm, border token, tag-filter test, usage recency sort

### 4 Findings Applied

1. **Delete Guard** (~line 152–158)
   - Wrapped `removeMutation.mutate(tpl.id)` with `window.confirm()`
   - Message: `Template "<title>" wirklich löschen?` (German, matches schedule/page.tsx pattern)
   - Returns early if user cancels

2. **Border Token** (~line 101–104)
   - Replaced hardcoded `"rgba(136,136,136,0.15)"` with `C.borderSubtle` (verified in colors.ts line 33)
   - Applied to inactive tag-filter chip borders

3. **Tag-Filter Test** (new test)
   - Creates two templates with different tags (animation/physics vs. ui-component)
   - Verifies filtering: click tag → matching templates remain, others disappear
   - Verifies toggle: click same tag again → all templates reappear
   - Updated delete test to stub `window.confirm` and assert it was called

4. **Usage Recency Sort** (~line 121–124)
   - Sort each template's usage list by `created_at` descending before display
   - Ensures `usage[0]` is truly the newest challenge (not just API list order)

### Test Results
- `npx vitest run src/verticals/bench_studio/__tests__/PromptLibraryTab.test.tsx` → **6/6 passed** (was 5/5, +1 tag-filter test)
- `npx tsc --noEmit` → **0 errors**

### Files Modified
- `frontend-v2/src/verticals/bench_studio/PromptLibraryTab.tsx` (2 inline fixes)
- `frontend-v2/src/verticals/bench_studio/__tests__/PromptLibraryTab.test.tsx` (1 test update, 1 new test)
