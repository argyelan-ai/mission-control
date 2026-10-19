# Gates — was wirklich blockt

Ein **Gate** ist alles, was eine Änderung rot machen und aufhalten soll: ein CI-Job,
ein Claude-Code-Hook, ein Prüfskript. Ehrlich ist ein Gate nur, wenn

1. klar ist, **bei welchem Fall es rot wird** (Spalte „blockt bei"), und
2. eine **Sabotage-Probe** oder ein **echter roter Lauf** zeigt, dass dieser Fall
   wirklich feuert (Spalte „Sabotage-Probe / Rot-Beleg").

Der CI-Job `gate-audit` (`.github/workflows/gate-audit.yml`, Skript
`tools/guards/gate_audit.py`) liest diese Tabelle und wird rot, wenn

- ein Workflow-Job oder ein Skript unter `tools/hooks/` / `tools/guards/` hier fehlt,
- eine Zeile vom Typ Gate/Hinweis keinen Rot-Fall oder keine Probe hat
  („offen", „keine", „—" zählen als fehlend),
- eine Zeile auf etwas zeigt, das es nicht mehr gibt,
- ein als Gate eingetragenes Skript gar nicht rot werden kann (z. B. nur `exit 0`),
- ein Workflow weich scheitert (`continue-on-error: true`, `|| true`, `exit 0`)
  ohne Kommentar `# gate-audit: soft - <Grund>` in der Zeile oder im Kommentarblock direkt darüber,
- ein Job mit `continue-on-error: true` als Gate statt als Hinweis eingetragen ist.

**Typen:** `Gate` = kann rot werden und stoppt etwas · `Hinweis` = wird rot, stoppt
aber bewusst nichts (Grund steht in der Zeile) · `Helfer` = kein Gate, macht nur
etwas (Formatieren, Melden), muss sagen warum.

**Merge-Pflicht** = steht in der Ruleset-Liste „required status checks" von `main`
(Merge-Queue wartet darauf). Stand 19.10.2026: Backend Tests, Frontend Checks,
Leak gate, Fresh-boot E2E, Docker Build Check. Ein roter Job ohne Merge-Pflicht ist
für die Queue unsichtbar — das ist eine Entscheidung des Operators, keine Agentenarbeit.

**Neues Gate?** Zeile hier ergänzen, im selben PR. Ohne Zeile wird `gate-audit` rot.

<!-- gate-audit:table — eine Zeile pro Gate, Spalten nicht umbenennen -->

| ID | Art | Was | blockt bei | Sabotage-Probe / Rot-Beleg | Merge-Pflicht |
|---|---|---|---|---|---|
| `ci:ci.yml#leak-gate` | Gate | gitleaks über den Baum, verbotene Dateien, privacy-scan gegen Baseline | `.env`/`*.pem`/`id_rsa*` im Baum, gitleaks-Fund, neue Flottennamen-Fundstelle | Rot-Lauf 39019506576 | ja |
| `ci:ci.yml#backend` | Gate | `pytest -x` der Backend-Suite (SQLite) inkl. Migrations-Wächter | ein Backend-Test rot | Rot-Lauf 39135305159 | ja |
| `ci:ci.yml#backend-postgres` | Gate | Migrationen auf echtem Postgres + `-m postgres`-Tests | Migration scheitert, Postgres-Test rot | Rot-Lauf 34875527024 | nein |
| `ci:ci.yml#omp-bridge` | Gate | Test-Suite der omp-Bridge | ein Bridge-Test rot | Rot-Lauf 38326193948 | nein |
| `ci:ci.yml#cdp-gateway` | Gate | Test-Suite des CDP-Gateways | ein Gateway-Test rot | Probe 19.10.2026: Kopie der Suite + `assert False` → pytest exit 1 (kein Rot-Lauf in 294 roten CI-Läufen) | nein |
| `ci:ci.yml#mc-cli` | Gate | Test-Suite von `mc` (stdlib) | ein mc-cli-Test rot | Rot-Lauf 35428791508 | nein |
| `ci:ci.yml#frontend-v2` | Gate | `tsc --noEmit` + vitest inkl. Design-Ratchet | Typfehler, roter vitest, neuer freier Pixel-/Farbwert | Rot-Lauf 39277948280 | ja |
| `ci:ci.yml#deploy-portability` | Gate | Frischer Boot über `install.sh --here`, Registrierung, Seed-Prüfungen, Host-uid | Installer/Boot/Migration scheitert, Seed nicht leer, falsches Image | Rot-Lauf 39020437296 | ja |
| `ci:ci.yml#docker` | Gate | Bau aller Images, Platten-Preflight-Tests, cdp-Healthcheck-Sabotage, playwright-mcp-Router | ein Build scheitert, Lib-/Healthcheck-Test rot | Rot-Lauf 39291053424; Healthcheck-Sabotage in `docker/cdp-browser/test_healthcheck.sh` | ja |
| `ci:kohaerenz.yml#kz-check` | Hinweis | `kz check` Roter Faden (Produktkarte, Regeln, PR-Abschnitte) | neue kz-Fundstelle, z. B. fehlender PR-Abschnitt „What the user sees" — **stoppt bewusst nichts** (siehe Workflow-Kommentar) | Rot-Lauf 39291053580 (2 neue Funde, Workflow trotzdem grün); 9 von 40 Läufen rot (Stand 19.10.) | nein |
| `ci:release.yml#images` | Gate | Release-Images bauen, nur als `sha-<commit>` pushen, beide Architekturen + Worker-Modul prüfen | Build scheitert, Architektur fehlt, `app/worker.py` fehlt | Rot-Lauf 39032675253 (mc-claude-agent) | — (Tag-Push, kein PR) |
| `ci:release.yml#promote` | Gate | geprüfte Images auf `X.Y.Z`/`latest` umtaggen | kein Release-Tag aus `release-image-tags.sh`, Umtaggen scheitert; läuft gar nicht, wenn `images` rot | Rot-Lauf 39032675253: `images` rot → `promote` übersprungen; Tag-Liste getestet in `backend/tests/test_release_image_tags.py` | — (Tag-Push, kein PR) |
| `ci:gate-audit.yml#gate-audit` | Gate | dieses Register gegen den Baum prüfen + Hook-Tests | unregistriertes Gate, Gate ohne Rot-Fall/Probe, veraltete Zeile, Skript nur mit `exit 0`, Soft-Fail ohne Begründung, roter Hook-/Audit-Test | `tools/guards/tests/test_gate_audit.py` (Dummy-Gate mit `exit 0` → rot) | nein (Vorschlag an Operator) |
| `script:tools/guards/gate_audit.py` | Gate | das Prüfskript hinter `gate-audit` | jede der Regeln R1–R7 (siehe Kopf des Skripts) | `tools/guards/tests/test_gate_audit.py` | über `gate-audit` |
| `script:tools/hooks/test-gate.sh` | Gate | Claude-Code-Hook vor `git push` / `gh pr create`: im Branch geänderte Testdateien laufen lassen | ein im Branch neuer/geänderter Test ist rot (exit 2 = Aufruf blockiert) | `tools/hooks/tests/test_test_gate.py` (kaputter Test → Block); vorher Platzhalter mit `exit 0`: 11 von 17 Tests rot | lokal (Hook) |
| `script:tools/hooks/secrets-check.sh` | Gate | Claude-Code-Hook vor `git commit` / `git push`: hinzugefügte Zeilen auf Zugangsdaten prüfen | Token/Schlüssel in gestagten bzw. ausgehenden Zeilen, gestagte `.env`/`*.pem`/`*.p12`/`id_rsa*` | `tools/hooks/tests/test_secrets_check.py`; vorher Platzhalter mit `exit 0` | lokal (Hook) |
| `script:tools/hooks/memory-commit.sh` | Helfer | Stop-Hook: committet geänderte Memory-Dateien des Operators | — kein Gate, sichert nur | — | — |
| `script:tools/hooks/auto-format.sh` | Helfer | Platzhalter (tut nichts) für einen Formatier-Hook nach Edit/Write | — kein Gate; Formatieren blockt nie | — | — |
| `script:tools/hooks/notify.sh` | Helfer | Platzhalter (tut nichts) für eine Benachrichtigung | — kein Gate; Melden blockt nie | — | — |
| `script:scripts/privacy-scan.py` | Gate | Flottennamen gegen `docs/privacy-sweep-backlog.md` (läuft im `leak-gate`) | neue Fundstelle ausserhalb der Baseline | `backend/tests/test_privacy_scan.py` (neue Fundstelle → exit 1) | über `leak-gate` |
| `test:backend/tests/test_alembic_chain_integrity.py` | Gate | eine Alembic-Spitze, keine zerstörerische Migration ohne Freigabe (läuft im `backend`) | zweiter Head, Revisionskollision, Drop ohne Marker | eingebaute Gegenproben in der Datei (z. B. `test_calls_drop_column_detects_text_wrapped_raw_sql`, `test_ci_cannot_silently_skip_the_destructive_migration_guards`) | über `backend` |

## Ausserhalb des Repos (nicht von `gate-audit` geprüft)

- **Hook-Registrierung:** `test-gate.sh` und `secrets-check.sh` wirken nur, wenn sie in
  der (privaten) Claude-Code-Konfiguration als `PreToolUse`-Hook für `Bash` eingetragen
  sind. Ein Gate im Repo ohne Eintrag blockt nichts. Empfehlung: den Hook immer aus
  `origin/main` laufen lassen (`bash <(git -C <repo> show origin/main:tools/hooks/test-gate.sh)`),
  nicht aus einem Checkout, auf dem gerade irgendein Feature-Branch liegt.
- **git-pre-push-Hook des Operators** (Leak-Scan der ausgehenden Zeilen, `kz check --fast`):
  liegt ausserhalb des Repos; Rot-Fall und Probe dokumentiert der Operator dort.
