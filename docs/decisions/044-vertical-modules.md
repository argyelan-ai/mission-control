# ADR-044 — Vertical-Module: strippbare Feature-Bundles

**Status:** Accepted
**Datum:** 2026-07-02
**Scope:** Backend/Architektur | Frontend/Struktur | Release

## Kontext

MC enthält Features, die nicht in den Public-Release gehören (News-Studio:
News-Aggregation, Shorts/Storyboards, Newsletter, Content-Pipeline —
persönliches Content-Business des Operators, ~16k LOC). Zusätzlich soll MC
langfristig modular wachsen: Features einzeln aktivieren, strippen oder
später veröffentlichen können ("Stufe 2: modularer Monolith").

## Entscheidung

**Modul-Vertrag für Verticals:**

1. **Backend:** `app/verticals/<name>/` (routers/ + services/) mit
   `register(app)`-Entrypoint. `app/verticals/__init__.py` macht
   pkgutil-Discovery: vorhandene Unterpakete werden registriert, fehlende
   ignoriert — Ordner löschen = Feature weg, App bootet unverändert.
2. **Kopplung nur über Hooks:** `app/verticals/hooks.py` (Core, wird nie
   gestrippt) hält Registries (`task_done_hooks`, `tools_md_sections`).
   Core-Code ruft Hook-Listen; Verticals befüllen sie in `register()`.
   **Core importiert NIE direkt aus einem Vertical-Paket** (Tests dürfen
   konditional via try/ImportError).
3. **Models + Migrationen bleiben im Core.** Schema ist über beide
   Varianten identisch; gestrippte Installationen haben brachliegende
   Tabellen. Das hält die Alembic-Kette linear und macht Upgrades in beide
   Richtungen trivial.
4. **Frontend:** `src/verticals/<name>/` (Komponenten + vertical-eigene
   `types.ts`/`api.ts`). Core-Navigation gated über Flags in
   `src/lib/verticals.ts`. Regel: Core importiert nichts aus
   `src/verticals/` (ausser den Flag-File-Konsumenten).
5. **Release:** `release/internal-paths.txt` strippt Vertical-Verzeichnisse;
   `scripts/release-public.sh` flippt die Frontend-Flags.

**Erstes Vertical:** `news_studio` (7 Router + agent-Callback + 10 Services
backend; news/shorts-Komponenten + 47 Types + 5 API-Namespaces frontend).

## Alternativen

- **Separates Plugin-Repo pro Feature (Stufe 3):** Verworfen (vorerst) —
  getrennte Migrations-Ketten, Frontend-Build ohne dynamisches Nachladen,
  Versionierungs-Matrix; Overkill für Self-Hosted-Ein-Operator-Betrieb.
- **Nur Release-Strip ohne Modulgrenze:** Verworfen — ohne erzwungene
  Grenze (Hooks, Import-Verbot) wächst Kopplung zurück und der Strip
  bricht irgendwann den Build.

## Konsequenzen

### Positiv
- Public-Release ohne News-Studio: Backend bootet (349 statt 416 OpenAPI-
  Pfade, 0 Reste), Next-Production-Build grün, Suiten grün in BEIDEN
  Varianten (Drift-Guards konditional).
- Blaupause für alle künftigen Features: neues Vertical = Ordner + register().

### Negativ
- Brachliegende Tabellen in gestrippten Installationen (bewusst, s.o.).
- Hook-Registry ist ein indirekter Codepfad — Debugging braucht das Wissen,
  dass `register()` beim App-Aufbau läuft.

## Verifikation (2026-07-02)
Boot-Diff mit/ohne Paket, Export-Boot, Next-Build des Exports, Backend
2518 grün, Frontend 93 grün, tsc-Härtetest ohne Vertical-Verzeichnisse.
