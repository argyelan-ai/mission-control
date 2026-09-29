# UI craft — how a visible change gets built and accepted

The one workflow for anything visible in `frontend-v2/`: layout, a new block,
a new page, spacing, type, colour — and for reviewing a UI pull request.
**DESIGN.md is the only rule source** (colours, shapes and the composition rules
K1–K14). This file is the procedure around it. Other taste skills or style
guides (landing-page checklists, "premium SaaS" prompts) are not used for MC.

Why it exists: every earlier UI pull request was correct on its own, yet the task
detail header grew to seven layers, because each one added a block, none took one
away and nobody looked at the whole picture before merging. Tests prove behaviour;
they do not prove that a screen is calm. The picture does.

## 1 · Which path

| Kind | Example | Required |
|---|---|---|
| **Fix** | text, one token for another, a spacing value for another scale value | steps 4–5, one phone picture in the run record |
| **Structure** | new block, new page, header rebuilt, things reordered | all steps; no merge before the operator saw the phone picture (K14) |

## 2 · Steps

| # | Step | Output | Gate |
|---|---|---|---|
| 0 | **Read** DESIGN.md (the composition table first) | — | — |
| 1 | **Reference** — look at how a strong product solves the same job (e.g. an issue page on a phone) | one sentence: what we take, what we do not. Screenshots of other products stay out of the repo | — |
| 2 | **Inventory** — list every fact the region shows, with its zone (K12) and "does the operator need this on more than half of the visits?" | table in the run record | K4 budget holds, otherwise cut first |
| 3 | **Variants** (only when the direction is open) | 2–3 variants **rendered** at 390 px (dark and light) — never ASCII sketches | the operator picks |
| 4 | **Build** | only scale values (ratchet green), `data-region` on each region, strings through i18n (K10) | `npm run test:run` |
| 5 | **Measure** | `npm run design:budget` on the branch's own dev server (`npm run dev`, port 3001 — `http://localhost` is the deployed main): budget JSON, 390 + 1440 pictures, 10 px blur picture; plus the UI probe (`docs/ui-probe.md`) for changed menus and dialogs | findings fixed or explained |
| 6 | **Critique** (structure only) | a *different* agent than the builder scores the rubric below from the pictures; at most 2 rounds of 1–3 findings each | ≥ 15 / 18 and no axis at 0 |
| 7 | **Acceptance** | before/after page for the operator (pictures, no code). A new judgement from the operator is proposed as a rule with an ID, never added silently | **operator** |

Pictures of live data never go into this public repository — not in commits and
not in pull-request bodies. They go into the run record or the private vault.

## 3 · Rubric (step 6)

Nine axes, 0–2 points each. **Pass = at least 15 of 18 and no axis at 0.**
The reviewer scores *before* and *after* separately and unlabelled first, then
compares; names the strongest spot in each region of the blur picture; answers
"what is this, and does the operator have to act?" from the first screen only;
gives every point with rule ID + region + what it sees; and ends with 1–3
concrete changes plus what must **stay**. It never edits DESIGN.md and never merges.

| Axis | Rules | 2 | 1 | 0 |
|---|---|---|---|---|
| Focus | K1 | one strong spot per region, the title leads | a secondary element competes | several equal spots, or a control louder than the title |
| 3 seconds | K4, K12 | what + state + next step clear without scrolling | clear after reading more than one line | state only after scrolling |
| Amount | K3, K4 | budget holds, nothing twice, nothing empty | one fact over budget | over budget, or repeated/empty values |
| Type | K5, K7 | ≤ 3 sizes in the header, no capitals there, no "LABEL: value" | one breach | label carpet |
| Edges | K9 | one shared left edge | two clean edges | a staircase of edges |
| Surfaces | K8 | groups by space | one unneeded box | card in card |
| Colour & words | K6, K10 | colour only for state, once; one word per state | one extra tint or word | signal twice, mixed languages, contrast < 4.5:1 |
| Craft | K3, K11 | 44 px targets, nothing cut off, tabular numbers, one primary button, light as good as dark | one small fault | clipping, overflow or several primary buttons |
| Character | K13 | clearly MC | calm but generic | could be any SaaS page |

## 4 · Anti-slop list for app screens

1. Eyebrow line above the title (`TASK · ID · PROJECT`) → project into the context bar, ID into the properties (K7)
2. Uppercase label before every value (`STATUS`, `AGENT`, `TIME`) → a sentence, or icon + value (K7, K12)
3. Mono as costume for labels, durations or prose → mono only for machine values (K7)
4. Numbered or abbreviated navigation (`01 HOME`, `SESS`, `RUNT`) → icon + whole word
5. Tile grid with 1 px joints for facts → a list with one edge (K8, K9)
6. Card in card in card, or the whole page as a card on a phone → space instead of boxes (K8)
7. Everything equally loud; nothing stands out in the blur picture (K1)
8. Tiny 8–10 px text in dim grey (K5, K6)
9. Free pixel values `6/10/14 px`, `text-[10.5px]` (K5, ratchet)
10. The same fact two or three times (PR, time, runtime) (K3)
11. Empty or meaningless cells ("PR —", "not tracked") (K3)
12. The same signal twice: red surface + red button + red word (K6)
13. A switch with three lines of explanation in the header → ⋯ menu (K4)
14. A control louder than the title (tab select in 16 px mono capitals) (K1, K12)
15. Two words or two languages for one state ("Blocked" next to "Blockiert") (K10)
16. Every button the same outlined box — no primary action (K11)
17. General slop: purple/cyan gradients, glow, glass, emoji (DESIGN.md forbids them already)

## 5 · Guards and tools

| Guard | What it catches | Where |
|---|---|---|
| `npm run design:ratchet` (runs inside `npm run test:run`, so in CI) | new free font sizes, off-scale spacing, inline colours, free tracking, font weights stored as `--font-*` | CI, red on more **and** on fewer (then lock the win in with `-- --update` in the same PR) |
| `npm run design:budget` | sizes, weights, text colours, capitals, left edges, nested surfaces, repeats, cut-off text of one region (K3–K9) | local / head worktree, report-only until calibrated (`--strict` exits 1) |
| UI probe (`npm run probe`) | menus and dialogs off-screen, covered, empty, small targets | local |
| impeccable (`shape`, `critique`, `distill`, `quieter`, browser detector) | a toolbox inside steps 2–6 — never a gate, never a second rule source | optional, when installed |

The machine counts; it does not judge. A green budget is not a merge approval for
a structure change (K14).
