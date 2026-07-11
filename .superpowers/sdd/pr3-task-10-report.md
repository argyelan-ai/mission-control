# PR3 Task 10 Report — ADR + Documentation

**Datum:** 2026-07-11  
**Branch:** feat/bench-studio  
**Commit:** 1ae41f71

## Status: COMPLETE

## Was gemacht wurde

### 1. ADR-Nummer verifiziert

Laut progress.md und tatsaechlichem Zustand von `docs/decisions/`:
- ADR-066: belegt (grok harness)
- ADR-067: frei, aber in progress.md als "uebersprungen" vermerkt
- ADR-068: belegt (grok bridge v2)
- ADR-069: naechste freie Nummer → verwendet

### 2. `docs/decisions/069-benchmark-studio.md` erstellt

Vollstaendiges ADR auf Deutsch, im Stil von ADR-065. Abgedeckt:

- **Kontext:** Marks Ziel (visuelle LLM-Demos als Video auf X), 3 Architektur-Spannungen
- **Entscheidung:** 5 Bausteine ueber 3 PRs mit allen Implementierungsdetails
  - Baustein 1: `post_media()` (Kern)
  - Baustein 2: mc-playwright `/record`+`/compose` (Kern)
  - Baustein 3: `prompt_templates` (Kern, PR 2)
  - Baustein 4: `bench_studio` Vertical (orchestrator, drafts, routers, DB, Frontend)
  - Baustein 5: Inbox-Preview
  - Hook-Registry `x_post_resolved_hooks`
  - `task_done_hooks` entgated
  - Draft-Idempotenz 409-Guard + Pipeline-Reuse
  - Edited-Text-wins
- **Alternativen:** 5 verworfene Optionen mit Begruendung
- **Konsequenzen:** Positiv (4) + Negativ/Bekannte Schwaechen (5):
  - Crash-Recovery-Gap (rendering/composing bleiben haengen nach Backend-Crash)
  - Theoretisches Race-Window beim 409-Guard (kein DB-Unique-Constraint)
  - Expired-pending blockiert Re-Draft
  - X-Video-Limit-Konstanten nicht hinterlegt (Follow-up)
  - sharedSubpath-Duplikation
- **Referenzen:** alle relevanten Code-Dateien + ADR-033/044/065

### 3. `docs/decisions/README.md` aktualisiert

Neue Tabellenzeile nach ADR-068-Eintrag eingefuegt. Kompakter Einzeiler-Summary
der gesamten Entscheidung inkl. aller 5 Bausteine und Schwaechen.

### 4. `docs/ARCHITECTURE.md` aktualisiert

- **Letztes Update** Header: 2026-05-17 → 2026-07-11
- **Neuer Abschnitt `### 7. Vertical-Module + Content-Bausteine`** zwischen §6 (LLM
  Runtime Registry) und "Zentrale Flows":
  - Tabelle aktiver Verticals (news_studio, bench_studio)
  - Tabelle Hook-Registries (task_done_hooks, tools_md_sections, x_post_resolved_hooks)
  - Tabelle Kern-Bausteine Publishing (text-post, media-post, draft-api, hook)
  - mc-playwright Record+Compose (Volume-Aenderung ro→rw erwaehnt)
  - bench_studio Vertical (Backend, DB-Tabellen, Frontend, Prompt Library)
- **Aenderungshistorie-Eintrag** zuoberst (2026-07-11): vollstaendige Zusammenfassung
  der 3-PR-Serie (wie die bestehenden 2026-07-10 Eintraege)

### 5. `python3 tools/generate-code-map.py` ausgefuehrt

Code-Map regeneriert (2550 Zeilen, 109641 Zeichen) — keine Aenderung vs. HEAD
(bereits aktuell nach Task 9).

## Commit

```
1ae41f71  docs(adr): add ADR-069 — Benchmark Studio vertical + core building blocks
```

Alle 3 Dateien in einem Commit:
- `docs/decisions/069-benchmark-studio.md` (neu, 128 Zeilen)
- `docs/decisions/README.md` (+1 Zeile Index-Eintrag)
- `docs/ARCHITECTURE.md` (+79 Zeilen Abschnitt 7 + Changelog-Eintrag)

## Concerns / Hinweise

- **ADR-067 ist eine Luecke** — progress.md sagt "ADR 067 frei aber uebersprungen".
  Die Luecke ist damit bewusst und dokumentiert im Numbering-Note von ADR-069.
- **code-map.md hatte keine Aenderungen** — das Tool lief sauber durch, aber die
  generierte Datei ist bit-identisch mit HEAD (Code-Aenderungen aus Tasks 1-9 haben
  die Map bereits auf aktuellem Stand gehalten).
- **Nummerierung bei Merge:** falls zwischen jetzt und Merge ein weiterer PR 067
  beansprucht, muss dieses ADR auf 070 umnummeriert werden (Numbering-Note im ADR
  selbst dokumentiert den Prozess).

---

## Fix-Append: Rerender Gate + ADR-069 Corrections

**Datum:** 2026-07-11  
**Commit:** 722d42ab

### Fix 1 — Rerender Gate widened (TDD)

- **Test written first** (`test_rerender_allowed_from_composing`): challenge mit
  `status="composing"` → POST rerender → expected 200. Test rot (409) vor dem Fix.
- **Fix applied** in `backend/app/verticals/bench_studio/routers.py` line 206:
  gate erweitert von `("review", "drafted", "failed")` auf
  `("review", "drafted", "failed", "rendering", "composing")`.
- **Test gruen** nach Fix. Alle 13 Tests in `test_bench_router.py` bestanden.

### Fix 2 — ADR-069 Korrekturen (4 Sub-Fixes)

- **2a (Expired-pending):** Paragraph korrigiert — `x_post` ist nicht renewable;
  der Approval-Watchdog (`health_checks.py::_check_expired_approvals`) setzt abgelaufene
  `x_post`-Approvals automatisch auf `status="expired"` beim naechsten Sweep,
  was den 409-Guard aufloest. Block-Fenster = TTL bis naechster Sweep.
- **2b (Crash-Recovery-Gap):** Text aktualisiert — Gate erlaubt nun explizit
  `rendering`/`composing`, Operator-Reset via rerender auch im haengenden Zustand moeglich.
- **2c (Endpoint-Liste):** `(create, list, get, start, rerender)` → `(create, list, get,
  draft, rerender, retry)` — kein "start"-Endpoint, stattdessen draft + retry korrekt.
- **2d (sharedSubpath):** Richtung umgekehrt — das Vertical definiert einen eigenen
  schmalen Helper, statt den Core-Helper `mediaPathToFilesLocation` zu verwenden.

### Fix 3 — __init__.py Docstring

`ADR-044, ADR-066` → `ADR-044, ADR-069` in
`backend/app/verticals/bench_studio/__init__.py`.

### Test-Summary

13 passed, 0 failed (`tests/test_bench_router.py`, 2.09s).
