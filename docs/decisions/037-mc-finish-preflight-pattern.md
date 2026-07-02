# ADR-037 — `mc finish` Preflight + Idempotency Pattern

**Status:** Accepted
**Datum:** 2026-05-16
**Scope:** Agent CLI (mc-cli), Frontend/Agent-Workflow

## Kontext

Der `mc finish` Befehl bündelt zwei Schritte, die ein Agent am Ende eines
Tasks erledigen muss: einen `comment_type=reflection` Comment mit den vier
Pflicht-Sektionen (Was wurde gemacht / Was hat funktioniert / Was war
unklar / Lesson für Agent-Memory) **und** den Status-PATCH auf
`done`/`review`. Die vorherige Implementation tat das in genau dieser
Reihenfolge:

```
POST /agent/.../comments  (reflection)        → 201
PATCH /agent/.../tasks/{id} status=done       → 422 "Checklist-Item(s) noch offen"
process exit 1
```

Beim 2026-05-16 "DNA PDF Dokument"-Vorfall postete der Researcher in 53 s
**drei** reflection-Comments für denselben Task: jeder `mc finish`-Versuch
postete erfolgreich den Comment, dann scheiterte der PATCH mit 422 (offene
Checklist), Researcher sah Exit 1, retried, und produzierte einen weiteren
Comment. Auf dem dritten Versuch fiel er auf manuelles `python3
urllib.request` zurück — ohne `X-Dispatch-Attempt-Id`-Header — und löste
zusätzlich ein `task.missing_dispatch_attempt_id` Activity-Event aus.

Die Dupes verschmutzen den Reflection-Audit-Trail dauerhaft (board_memory
+ Agent-Lessons werden aus Reflections gefüllt) und maskieren den
eigentlichen Failure-Pfad ("offene Checklist") hinter einem nackten Exit 1.

## Entscheidung

`mc finish` bekommt einen **Preflight-Check** der jedes Backend-Gate, das
der PATCH danach prüfen würde, vorher per GET abklopft. Erst wenn alles
sauber ist, läuft die POST-PATCH-Sequenz. Plus eine
**Reflexions-Idempotenz** (5 min Window, gleicher Agent) damit ein
ehrlicher Retry nach generischem Backend-Fehler nicht erneut einen
Comment landen lässt.

```python
def _cmd_finish(args, client, cfg):
    _validate_reflection(args.message)             # 4 Pflichtfelder + length
    pre = _preflight_finish(client, cfg, target)
    if pre["skip_patch"]:                          # Task schon im Ziel-Status
        return 0
    if pre["should_post_comment"]:                 # keine recent reflection
        client.request("POST", ".../comments", body=…)
    try:
        return _patch_status(client, cfg, target)
    except Exception as exc:
        if pre["should_post_comment"]:
            print("# Reflexion gepostet, Status-PATCH fehlgeschlagen.\n"
                  "# Retry mit `mc done` / `mc review`", file=sys.stderr)
        raise
```

`_preflight_finish` checkt:

1. **Status-Transition möglich?** GET `/tasks/{id}/detail`. Nur
   `in_progress` → `done`/`review` und `review` → `done` sind valide.
   Andere Source-Status → fail-fast mit klarer Message.
2. **Idempotenz**: wenn aktueller Status == Ziel-Status → No-op, exit 0
   (kein Comment, kein PATCH).
3. **Checklist alle done/skipped?** GET `/tasks/{id}/checklist`. Pending
   Items → Fehlermeldung mit den ersten 3 Item-Titeln + "+N weitere".
4. **Recent self-reflection (5 min, gleicher Agent)?** GET
   `/tasks/{id}/comments`. Falls ja → `should_post_comment=False`, nur
   PATCH wird versucht. Anderer Agent oder älter als 5 min → POST fährt
   normal.

Plus eine lokale Validation:

5. **Literal `\n` Detection**: wenn der Reflection-Text das 2-Zeichen-
   Escape `\n` enthält **und** keine echten Newlines hat, ist das fast
   sicher der Bash-Quoting-Bug
   (`mc finish "## … \n## …"` ohne `$'…'`). Fail-fast mit Hilfsmeldung
   die `$'…'` und Heredoc-Syntax zeigt.

## Alternativen

- **Atomic Backend-Endpoint** (`POST /agent/.../tasks/{id}/finish` der
  intern Comment + Status macht) → Verworfen weil parallel im selben
  Repo eine andere Claude-Session aktiv am Backend (vault-Phasen)
  arbeitete; Backend-Add wäre kollidiert mit ihren Migrations + Routern.
  Plus: Pre-flight im CLI funktioniert für **bestehende** Backend-API
  ohne Server-Deploy.
- **PATCH zuerst, Comment hinterher** → Verworfen weil die `enforce_-
  reflection` Backend-Validation den PATCH ohne vorherige Reflection mit
  400 ablehnt.
- **Keine Idempotenz-Window** (jeder `mc finish`-Aufruf postet) →
  Verworfen weil genau das den DNA-PDF-Vorfall ausgelöst hat.
- **Status Pre-Check ohne Checklist Pre-Check** (rely auf Backend-422) →
  Verworfen weil das exakt das alte Verhalten ist.

## Konsequenzen

### Positiv

- Keine Dupe-Reflections aus Retry-Loops mehr — der Comment landet erst
  wenn der PATCH (nahezu sicher) durchgeht.
- Fehlermeldungen erklären jetzt was zu tun ist statt Exit 1: "1
  Checklist-Item(s) noch offen: 60ae6cdd (PDF erstellen). Erst alle mit
  `mc checklist done <id>` schliessen, dann `mc finish` erneut."
- Idempotenz: ein ehrlicher Retry nach generischem 5xx schluckt nicht
  noch einen Comment.
- Literal-`\n`-Detection fängt einen wiederkehrenden Bash-Quoting-Bug ab,
  bevor er als unleserlicher Comment in der Datenbank landet.
- Recovery-Hint bei post-POST-Fail: Agent weiss genau welchen retry-
  Befehl er starten soll (`mc done` vs `mc review`).
- Pattern ist generisch: jeder weitere CLI-Wrapper um eine multi-step
  Backend-Sequenz (z.B. ein hypothetisches `mc release` mit "Tag → Push →
  Notification") kann denselben Aufbau übernehmen.

### Negativ

- 2-3 zusätzliche GET-Requests pro `mc finish` (detail + checklist +
  comments). Bei der heutigen Last vernachlässigbar, aber jede Pre-flight-
  Phase kostet messbare Wallclock-Time bei einem Watchdog-Stress-Test.
- Race zwischen Preflight und PATCH bleibt theoretisch: wenn der Operator in der
  UI eine neue Checklist-Sache hinzufügt zwischen Pre-flight und PATCH,
  kommt 422 trotzdem. Recovery-Hint führt aber durch, kein Dupe.
- Children-Integrität (Backend-Rule `check_children_complete`) wird **nicht**
  pre-checked, weil kein agent-scoped `/tasks/{id}/children` endpoint
  existiert — Backend-422 bei Parent-Tasks bleibt, Recovery-Hint zeigt
  retry mit `mc done`.
- 5-Minuten Dedup-Window ist eine Heuristik. Wenn ein Agent (rare)
  innerhalb von 5 min eine LEGITIME zweite Reflection posten will (z.B.
  nach Re-Open via UI), wird sie übersprungen. Workaround: Window per CLI
  flag override-bar machen ist nicht implementiert.
- Constants (`_FINISH_ALLOWED_FROM`, Status-Set, MIN_CHARS,
  REQUIRED_FIELDS) duplizieren Backend-Werte — wenn Backend sie ändert,
  hier auch ändern. Public Contract.

## Referenzen

- Betroffene Dateien:
  - `scripts/mc-cli/mc_cli/commands.py:159-300` (validate, preflight,
    helpers, neuer `_cmd_finish`)
- Commit: `a9acb9e3` — fix(mc-cli): preflight + idempotency for
  `mc finish` — no more dupe reflections
- Verwandte ADRs: ADR-023 (Reflection als Audit-Hebel + Review-Policy),
  ADR-031 (Hermes Hardening — Poll Claim, ähnlicher Pattern für Worker)
- Tests: `scripts/mc-cli/tests/test_finish_preflight.py` (19 Cases),
  21/21 grün
- Live-verifiziert: 2026-05-16 12:21 UTC, Researcher Task "LLM Modelle
  für DGX Spark" (358 s, 1 Reflection sauber gepostet vs 3 beim DNA-PDF-
  Vorfall ohne Fix)
- Motivierender Vorfall: `db4905cb-a478-46f6-b3a2-ffe7ac14d248` ("DNA
  PDF Dokument"), 3 Reflections in 53 s, 1× `task.missing_dispatch_-
  attempt_id` Event
