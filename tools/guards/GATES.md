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
- ein Job mit `continue-on-error: true` als Gate statt als Hinweis eingetragen ist,
- ein Job mit `if: false` als Gate eingetragen ist (er läuft nie),
- ein CI-Job ohne Merge-Pflicht als `Gate` statt `Gate (nicht Pflicht)` eingetragen ist,
- eine Lauf-ID im Beleg nicht existiert, zu einem anderen Workflow gehört oder als
  „Rot-Lauf" gar nicht rot war.

**Typen:** `Gate` = kann rot werden und stoppt etwas · `Gate (nicht Pflicht)` = CI-Job
kann rot werden, ist aber **kein Pflicht-Check** — die Merge-Queue wartet nicht darauf,
ein roter Lauf hält also nichts auf, solange niemand hinschaut · `Hinweis` = wird rot,
stoppt aber bewusst nichts (Grund steht in der Zeile) · `Helfer` = kein Gate, macht nur
etwas (Formatieren, Melden), muss sagen warum.

**Belege:** `Rot-Lauf <ID>` = ein GitHub-Lauf dieses Workflows, der wirklich `failure`
war (und, falls der Job darin lief, dessen Job selbst). `Lauf <ID>` = Lauf existiert in
diesem Workflow, war aber nicht rot (z. B. Hinweis, übersprungener Job). Im CI prüft
`gate_audit.py --verify-runs` jede ID gegen die GitHub-API (Regel R9).

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
| `ci:ci.yml#backend-postgres` | Gate (nicht Pflicht) | Migrationen auf echtem Postgres + `-m postgres`-Tests | Migration scheitert, Postgres-Test rot | Rot-Lauf 34875527024 | nein |
| `ci:ci.yml#omp-bridge` | Gate (nicht Pflicht) | Test-Suite der omp-Bridge | ein Bridge-Test rot | Rot-Lauf 38326193948 | nein |
| `ci:ci.yml#cdp-gateway` | Gate (nicht Pflicht) | Test-Suite des CDP-Gateways | ein Gateway-Test rot | Probe 19.10.2026: Kopie der Suite + `assert False` → pytest exit 1 (kein Rot-Lauf in 294 roten CI-Läufen) | nein |
| `ci:ci.yml#mc-cli` | Gate (nicht Pflicht) | Test-Suite von `mc` (stdlib) | ein mc-cli-Test rot | Rot-Lauf 35428791508 | nein |
| `ci:ci.yml#frontend-v2` | Gate | `tsc --noEmit` + vitest inkl. Design-Ratchet | Typfehler, roter vitest, neuer freier Pixel-/Farbwert | Rot-Lauf 39277948280 | ja |
| `ci:ci.yml#deploy-portability` | Gate | Frischer Boot über `install.sh --here`, Registrierung, Seed-Prüfungen, Host-uid | Installer/Boot/Migration scheitert, Seed nicht leer, falsches Image | Rot-Lauf 39020437296 | ja |
| `ci:ci.yml#docker` | Gate | Bau aller Images, Platten-Preflight-Tests, cdp-Healthcheck-Sabotage, playwright-mcp-Router | ein Build scheitert, Lib-/Healthcheck-Test rot | Rot-Lauf 34720913640; Healthcheck-Sabotage in `docker/cdp-browser/test_healthcheck.sh` | ja |
| `ci:kohaerenz.yml#kz-check` | Hinweis | `kz check` Roter Faden (Produktkarte, Regeln, PR-Abschnitte) | neue kz-Fundstelle, z. B. fehlender PR-Abschnitt „What the user sees" — **stoppt bewusst nichts** (siehe Workflow-Kommentar) | Lauf 39291053580 (2 neue Funde, Workflow trotzdem grün); 9 von 40 Läufen rot (Stand 19.10.) | nein |
| `ci:release.yml#images` | Gate | Release-Images bauen, nur als `sha-<commit>` pushen, beide Architekturen + Worker-Modul prüfen | Build scheitert, Architektur fehlt, `app/worker.py` fehlt | Rot-Lauf 39032675253 (mc-claude-agent) | — (Tag-Push, kein PR) |
| `ci:release.yml#promote` | Gate | signierte Digests auf `X.Y.Z` (+ `X.Y`/`latest` bei stabilen Tags) umtaggen, dann prüft die Update-Engine die Signatur wie auf einem Nutzer-Rechner | kein Release-Tag aus `release-image-tags.sh`, Umtaggen scheitert, Engine lehnt die Signatur ab oder `X.Y.Z` zeigt nicht auf den signierten Digest (nur stabile Tags; Vorabversionen werden übersprungen, siehe Kommentar im Workflow); läuft gar nicht, wenn `images` oder `sign` rot | Lauf 39032675253: `images` rot → `promote` übersprungen (damals noch ohne `sign`); Tag-Liste in `backend/tests/test_release_image_tags.py`, Ablauf in `backend/tests/test_mc_update_verify.py::test_promote_retags_the_signed_digest_and_proves_the_engine_accepts_it`. Engine-Prüfschritt noch nie gelaufen | — (Tag-Push, kein PR) |
| `ci:ci.yml#mc-update` | Gate (nicht Pflicht) | Update-Engine `scripts/mc_update.py`, Update-Tür der Bridge und `install.sh --update/--rollback/--resume` gegen eine Fake-Welt (lokales git-Upstream, Fake-Docker, echtes `backup.sh`), auf py3.9 und py3.12. **Wackelt:** `test_start_runs_detached_to_done` scheitert sporadisch (FileNotFoundError auf die `.exit`-Datei) | ein Engine-, Tür- oder install-Test rot | Probe 20.10.2026: `self.put_fleet_back()` in `resume_after_switch` entfernt → `test_resume_after_switch_puts_a_lost_fleet_file_back` rot (1 failed, 144 passed), zurück → grün. Kein echter Rot-Lauf belegt: die 8 geprüften roten Läufe seit 10.10. (z. B. Lauf 39342385365) sind alle derselbe Wackel-Test, kein gefangener Fehler | nein |
| `ci:ci.yml#mc-update-crash` | Gate (nicht Pflicht) | Crash-Matrix der Update-Engine: vor und nach jedem Journal-Schreiben getötet (exit 137), dann `resume`, py3.9 | ein Endzustand falsch: Versionen gemischt, Fleet-Datei oder `.env` kaputt, Lock belegt, Drain übrig | Probe 20.10.2026: `self.drain_clear()` am Ende von `resume_after_switch` entfernt → 22 von 208 Fällen rot, zurück → grün. Grenze: `put_fleet_back()` entfernt lässt die Matrix grün (das fängt nur `mc-update`). Kein Rot-Lauf in CI seit Einführung | nein |
| `ci:guards.yml#guards` | Gate (nicht Pflicht) | shellcheck der Guards, `run-all.sh`, `sabotage-probe.sh`, `lint.sh` auf `AGENTS.md`/`CLAUDE.md`/`docs`/`scripts`/`tools`/`.github`, Installer- und Push-Beweis-Tests. Workflow hat keinen `merge_group`-Trigger: als Pflicht-Check würde die Queue auf ihn warten | shellcheck-Warnung, ein `.bad` grün oder `.good` rot, Guard-Logik ungetestet, verbotene Form in einer Repo-Datei (z. B. `git push` ohne privacy-scan in einem Code-Block), roter Installer-/Push-Beweis-Test | Rot-Lauf 39296675533 (shellcheck SC2034 + 4× push-gate-`.bad` mit exit 0; Job hiess damals „Guards (fixtures + sabotage probe)") | nein |
| `ci:release.yml#sign` | Gate | Freigabe-Klick über das Environment `release` (Pflicht-Reviewer), dann jedes geprüfte Image per Digest mit cosign (keyless) signieren | Environment fehlt oder hat keinen Pflicht-Reviewer (`scripts/release_gate_check.py` exit 1), Freigabe abgelehnt, kein Digest, cosign scheitert; läuft gar nicht, wenn `images` rot | `backend/tests/test_mc_update_verify.py` (`test_gate_check_requires_reviewers`: ungeschütztes/fehlendes Environment → nicht frei; `test_gate_check_cli_exit_codes` → exit 1; `test_signing_job_checks_the_gate_first_and_signs_by_digest`). **Noch nie gelaufen** (kein Release-Tag seit Einführung). Stand 20.10.2026: das Repo hat **kein** Environment `release` (API: 0 Environments) — der nächste Tag-Release wird hier rot, bis der Operator es mit Reviewer anlegt | — (Tag-Push, kein PR) |
| `ci:gate-audit.yml#gate-audit` | Gate (nicht Pflicht) | dieses Register gegen den Baum prüfen + Hook-Tests | unregistriertes Gate, Gate ohne Rot-Fall/Probe, veraltete Zeile, Skript nur mit `exit 0`, Job mit `if: false`, Soft-Fail ohne Begründung, Gate ohne Merge-Pflicht nicht als solches markiert, Lauf-ID im Beleg falsch/nicht rot (R9), roter Hook-/Audit-Test | `tools/guards/tests/test_gate_audit.py` (Dummy-Gate mit `exit 0` → rot) | nein (Vorschlag an Operator) |
| `script:tools/guards/gate_audit.py` | Gate | das Prüfskript hinter `gate-audit` | jede der Regeln R1–R9 (siehe Kopf des Skripts) | `tools/guards/tests/test_gate_audit.py` | über `gate-audit` |
| `script:tools/hooks/test-gate.sh` | Gate | Claude-Code-Hook vor `git push` / `gh pr create`: im Branch geänderte Testdateien + die nach geänderten Quelldateien benannten Tests (`app/x.py` → `test_x*.py`) laufen lassen. **Frühe Rückmeldung, ersetzt CI nicht**: andere kaputte Tests, gelöschte Tests (nur gemeldet) und abgeschwächte Asserts sieht erst CI | einer dieser Tests ist rot, oder das Zeitbudget (270 s, unter dem Hook-Limit 300 s) ist aufgebraucht (exit 2 = Aufruf blockiert) | `tools/hooks/tests/test_test_gate.py` (kaputter Test → Block, `git -c … push` → Block, kaputte Quelle mit bestehendem Test → Block, Zeitbudget leer → Block); vorher Platzhalter mit `exit 0`: 11 von 17 Tests rot | lokal (Hook, nur Claude-Code-Sessions mit Eintrag) |
| `script:tools/hooks/secrets-check.sh` | Gate | Claude-Code-Hook vor `git commit` / `git push`: hinzugefügte Zeilen auf Zugangsdaten prüfen. Ausnahmen: Wert enthält `TEST`/`dummy`/`fake`/`example`, oder Zeile trägt `# secrets-check: allow - <Grund>` (ohne Grund zählt es nicht) | Token/Schlüssel in gestagten Zeilen (bei `commit -a` / `git add … && git commit` auch ungestagte und neue Dateien) bzw. ausgehenden Zeilen, `.env`/`*.pem`/`*.p12`/`id_rsa*` | `tools/hooks/tests/test_secrets_check.py` (u. a. `git add -A && git commit`, `commit -am`, `git -c … commit`, Marker ohne Grund); vorher Platzhalter mit `exit 0` | lokal (Hook, nur Claude-Code-Sessions mit Eintrag) |
| `script:tools/hooks/memory-commit.sh` | Helfer | Stop-Hook: committet geänderte Memory-Dateien des Operators | — kein Gate, sichert nur | — | — |
| `script:tools/hooks/auto-format.sh` | Helfer | Platzhalter (tut nichts) für einen Formatier-Hook nach Edit/Write | — kein Gate; Formatieren blockt nie | — | — |
| `script:tools/hooks/notify.sh` | Helfer | Platzhalter (tut nichts) für eine Benachrichtigung | — kein Gate; Melden blockt nie | — | — |
| `script:tools/guards/background-tests.sh` | Gate | Lint-Guard (auch im Hook): Testlauf mit `run_in_background` oder `&` ohne `wait`; `--worktree <dir>` prüft offene Änderungen | Fund in einer Datei/einem Befehl (exit 1); `--worktree` bei uncommitteten Änderungen | `fixtures/background-tests*.bad` über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün) | über `guards` (nicht Pflicht) |
| `script:tools/guards/bash-hook.sh` | Hinweis | Claude-Code-`PreToolUse`-Hook auf `Bash` (und omp-Hook über `docker/omp-bridge/guard-hook.mjs`): ruft push-gate, gate-exit, zsh-set-split, background-tests, tmp-previews auf, lässt vor einem Push den privacy-scan laufen, liest `git status` vor `checkout`/`reset --hard`/`clean -f`/`stash drop` | ein Fund der genannten Guards, Push ohne grünen Scan, Wegwerf-Befehl bei offenen Änderungen — **stoppt im Standard-Modus `shadow` nichts** (nur Log `~/.mc/guards/hook-log.jsonl`); blockt (exit 2) erst mit `GUARD_HOOK_MODE=block` bzw. `~/.mc/guards/mode`; fällt bei eigenem Fehler offen | `fixtures/bash-hook.*.bad` (u. a. `incident-2026-10-17`) im Fixture-Modus exit 1, über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün); `docker/omp-bridge/tests/test_guard_hook.py` | lokal (Hook; Modus und Eintrag beim Operator, ausserhalb des Repos) |
| `script:tools/guards/gate-exit.sh` | Gate | Lint-Guard (auch im Hook): ein Gate (preflight, privacy-scan, run-all …), dessen Exit-Code durch `\| tail`, `;` oder `\|\|` ohne Stopp verloren geht | Fund (exit 1) | `fixtures/gate-exit*.bad` (u. a. `incident-2026-10-18`, `rc-lie`) über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün) | über `guards` (nicht Pflicht) |
| `script:tools/guards/hookparse.py` | Helfer | gemeinsamer Parser der git-Aufrufe für `bash-hook.sh` und `post-push-hook.sh` — entscheidet selbst nichts. Fehlt er (z. B. im Agent-Image), fallen beide Hooks offen | — kein Gate; geprüft nur indirekt über die Hook-Fixtures | — | — |
| `script:tools/guards/install-hooks.py` | Helfer | trägt die beiden Hooks in Claude-Code-`settings.json` ein (Trockenlauf, `--apply` mit Backup). `--check` gibt exit 1 bei fehlendem Eintrag, läuft aber nirgends automatisch — darum kein Gate | — kein Gate; Tests in `tools/guards/tests/test_install_hooks.py` (laufen in `guards`) | — | — |
| `script:tools/guards/lib.sh` | Helfer | gemeinsame Funktionen der Lint-Guards (`hit`, `unquoted`, `lint_main`, `guard-allow`) | — kein Gate, wird nur eingebunden | — | — |
| `script:tools/guards/lint.sh` | Gate | ruft die fünf Lint-Guards nacheinander auf, der höchste Exit-Code gewinnt (Schritt „Lint the files agents follow" in `guards`) | ein Lint-Guard rot | Probe 20.10.2026: `lint.sh fixtures/push-gate.bad` → exit 1, `lint.sh fixtures/push-gate.good` → exit 0. Selbst nicht von `run-all.sh` geprüft (steht auf dessen Nicht-Guard-Liste); kein Rot-Lauf dieses Schritts | über `guards` (nicht Pflicht) |
| `script:tools/guards/post-deploy-probe.sh` | Gate | Nach-Deploy-Beweis: Container-Alter, Datei-Vergleich, richtiger Checkout, ein Watchdog, Bootstrap-Endpunkt 403; leerer Beweis zählt als rot | eine Prüfung rot oder ein Pflicht-Beweis leer | `fixtures/post-deploy-probe*.bad` (`empty`, `ref`, `stale`, `watchdog`, `workdir`) über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün) | nur wenn nach einem Deploy aufgerufen — im Repo ruft es nichts auf |
| `script:tools/guards/post-push-hook.sh` | Hinweis | Claude-Code-`PostToolUse`-Hook (und omp-Hook): nach jedem Push `proof-push.sh` gegen das Remote, das git wirklich benutzt hat | Remote-Branch ≠ `HEAD`, Pfad nicht auflösbar, Beweis nicht erfassbar — **stoppt nichts**: der Push ist schon passiert; im Modus `block` bekommt der Agent den Grund, im Standard `shadow` nur das Log | `fixtures/post-push-hook.*.bad` über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün); `tools/guards/tests/test_post_push_live.py` gegen ein echtes Bare-Remote | lokal (Hook; Modus und Eintrag beim Operator) |
| `script:tools/guards/proof-push.sh` | Gate | ein Push gilt erst, wenn der Remote-Branch-SHA gleich `HEAD` ist | SHA ungleich, fehlt oder ist kein voller SHA (exit 1) | `fixtures/proof-push*.bad` (`missing`) über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün); `tools/guards/tests/test_post_push_live.py` | nur wenn aufgerufen (von Hand oder über `post-push-hook.sh`) |
| `script:tools/guards/push-gate.sh` | Gate | Lint-Guard (auch im Hook): jeder Push als `privacy-scan && git -C <worktree> push`, Ausgabe nicht abgeschnitten, kein `--no-verify`/`core.hooksPath`, kein Force ohne Lease, kein Push über eine Variable | Fund (exit 1) | `fixtures/push-gate*.bad` (u. a. `incident-2026-09-01`, `-10-17`, `-10-18`) über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün); Rot-Lauf 39296675533 (vier `.bad` nicht erkannt → `run-all` rot) | über `guards` (nicht Pflicht) |
| `script:tools/guards/run-all.sh` | Gate | jedes `.bad` muss seinen Guard rot machen, jedes `.good` grün | ein `.bad` grün, ein `.good` rot, Guard ohne `.bad`+`.good`, Fixture ohne Guard | Rot-Lauf 39296675533 (4× push-gate-`.bad` exit 0); Probe 20.10.2026: Kopie mit Guard ohne `.good` → exit 1 | über `guards` (nicht Pflicht) |
| `script:tools/guards/run-gates.sh` | Gate | Werkzeug: Gates Zeile für Zeile, Stopp beim ersten roten; verweigert `;`, `\|\|` und einzelnes `&` ausserhalb von Anführungszeichen | ein Gate rot oder eine verweigerte Zeile (exit 1) | `fixtures/run-gates*.bad` (`chain`, `or`, `tail`, `background`) über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün) | nur wo aufgerufen — im Repo ruft es nichts auf |
| `script:tools/guards/sabotage-probe.sh` | Gate | kommentiert pro Guard den Block `# >>> guard-logic` aus und verlangt, dass dann mindestens ein `.bad` grün wird | Guard ohne Logik-Block, Logik auskommentiert und kein `.bad` kippt, Skript parst ohne Logik nicht | Probe 20.10.2026: Kopie mit Schein-Guard, der nach Dateiname entscheidet (`.bad` → 1) → `run-all.sh` grün, `sabotage-probe.sh` „PROBE FAILED", exit 1 | über `guards` (nicht Pflicht) |
| `script:tools/guards/tmp-previews.sh` | Gate | Lint-Guard (auch im Hook): Vorschauen, Deploy-Ordner, Übergaben unter `/tmp` (macOS räumt nach Tagen auf) — nur Schreibzugriffe | Schreibziel unter `/tmp` ausser dem Session-Scratchpad (exit 1) | `fixtures/tmp-previews*.bad` (`cp`, `option`, `redirect`, `worktree`) über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün) | über `guards` (nicht Pflicht) |
| `script:tools/guards/zsh-set-split.sh` | Gate | Lint-Guard (auch im Hook): `set -- $var` teilt in zsh nicht | Fund (exit 1) | `fixtures/zsh-set-split.bad` über `run-all.sh` (jedes `.bad` muss exit 1 geben) und `sabotage-probe.sh` (Logik-Block auskommentiert → mindestens ein `.bad` wird grün) | über `guards` (nicht Pflicht) |
| `script:scripts/privacy-scan.py` | Gate | Flottennamen gegen `docs/privacy-sweep-backlog.md` (läuft im `leak-gate`) | neue Fundstelle ausserhalb der Baseline | `backend/tests/test_privacy_scan.py` (neue Fundstelle → exit 1) | über `leak-gate` |
| `test:backend/tests/test_alembic_chain_integrity.py` | Gate | eine Alembic-Spitze, keine zerstörerische Migration ohne Freigabe (läuft im `backend`) | zweiter Head, Revisionskollision, Drop ohne Marker | eingebaute Gegenproben in der Datei (z. B. `test_calls_drop_column_detects_text_wrapped_raw_sql`, `test_ci_cannot_silently_skip_the_destructive_migration_guards`) | über `backend` |

## Ausserhalb des Repos (nicht von `gate-audit` geprüft)

- **Hook-Registrierung — wen die Hooks erreichen:** `test-gate.sh` und `secrets-check.sh`
  blocken nur in **Claude-Code-Sessions**, deren (private) Konfiguration sie als
  `PreToolUse`-Hook für `Bash` einträgt. Agenten ausserhalb von Claude Code (andere Harnesses wie omp,
  Docker-Agenten) sehen sie nie. Was wirklich **alle** erreicht, ist nur CI.
  Der Eintrag soll das Skript aus `origin/main` laufen lassen, nicht aus einem Checkout,
  auf dem gerade irgendein Feature-Branch liegt, und erst dann `git show` starten, wenn
  der Befehl überhaupt nach Push/Commit aussieht:
  `bash -c 'p=$(cat); case "$p" in *push*|*commit*|*"pr create"*) ;; *) exit 0;; esac; f=$(mktemp) && git -C <repo> show origin/main:tools/hooks/test-gate.sh > "$f" && printf "%s" "$p" | bash "$f"; rc=$?; rm -f "$f"; exit $rc'`
  (Hook-Timeout für `test-gate` 300 s — das Skript hält sein Budget von 270 s darunter.)
  Achtung: Damit läuft Code aus `main` bei jedem Push/Commit auf dem Rechner des
  Operators — wer nach `main` mergen kann, bestimmt, was dort läuft.
- **Lesson-Hooks `bash-hook.sh` / `post-push-hook.sh`:** eingetragen per
  `tools/guards/install-hooks.py` (Claude Code) bzw. `docker/omp-bridge/guard-hook.mjs`
  (omp). Ob sie eingetragen sind und ob der Modus `shadow` oder `block` ist
  (`~/.mc/guards/mode`), sieht `gate-audit` nicht — darum stehen sie oben als Hinweis.
- **git-pre-push-Hook des Operators** (Leak-Scan der ausgehenden Zeilen, `kz check --fast`):
  liegt ausserhalb des Repos; Rot-Fall und Probe dokumentiert der Operator dort.
