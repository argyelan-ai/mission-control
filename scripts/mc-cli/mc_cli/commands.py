"""Command registry + handlers for `mc` CLI.

Each command mirrors an agent-scoped backend endpoint. The registry is also
introspected by `backend/tests/test_mc_cli_endpoints.py` — add a SPEC entry
whenever a new agent-scoped endpoint must be reachable from agents.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from typing import Callable

from .client import Client
from .config import Config
from .errors import UsageError


@dataclass(frozen=True)
class CommandSpec:
    """Metadata for one `mc <sub>` command.

    `endpoints` is the set of backend routes this command can hit (some
    commands fan out across multiple — e.g. checklist add/done/list).
    """
    name: str
    help: str
    endpoints: tuple[str, ...]
    scope: str  # primary scope required on the backend
    handler: Callable[[argparse.Namespace, Client, Config], int]
    add_args: Callable[[argparse.ArgumentParser], None] = field(
        default=lambda p: None
    )


# ── Helpers ───────────────────────────────────────────────────────────────

def _emit(data) -> None:
    """Print JSON result to stdout (or a one-line summary if small)."""
    if data is None:
        return
    if isinstance(data, dict) and "id" in data:
        print(data.get("id"))
        return
    print(json.dumps(data, indent=2, default=str))


def _patch_status(client: Client, cfg: Config, status: str, **extra) -> int:
    board_id, task_id = cfg.require_task_context()
    body = {"status": status, **{k: v for k, v in extra.items() if v is not None}}
    resp = client.request("PATCH", f"/api/v1/agent/boards/{board_id}/tasks/{task_id}", body=body)
    _emit(resp)
    return 0


# ── Status commands (ack / done / review / blocked / failed) ──────────────

def _cmd_ack(args, client, cfg):
    """Task-ACK — idempotent.

    Wenn poll.sh den Task bereits via /me/poll geclaimt hat, ist der Status
    schon `in_progress` und der Agent kriegt "Ungueltiger Status-Uebergang:
    In Progress -> In Progress". Fuer den Agent ist das ein verwirrender
    False-Negative — er HAT ge-ACK'd (poll.sh setzt ack_at automatisch),
    die CLI sagt aber Fehler. Darum: 400 "In Progress -> In Progress" als
    Erfolg behandeln.
    """
    try:
        return _patch_status(client, cfg, "in_progress")
    except Exception as e:
        msg = str(e)
        if "In Progress" in msg and "In Progress" in msg.replace("In Progress", "", 1):
            # Idempotent-Success: Task war schon in_progress.
            _, task_id = cfg.require_task_context()
            print(task_id)
            return 0
        raise


def _force_close_open_checklist(client: Client, cfg: Config) -> int:
    """Schliesst alle offenen Checklist-Items (status not in done|skipped).

    Wird von `mc done --force` und `mc finish --force` genutzt wenn der Agent
    bewusst beschliesst, Items en bloc als done zu markieren — z.B. weil die
    Inhalte de facto erledigt sind aber er vergessen hat `mc checklist done <id>`
    aufzurufen. Reversibel: Items koennen via Backend wieder geoeffnet werden.

    Returns: count of items closed.
    """
    board_id, task_id = cfg.require_task_context()
    base = f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist"
    items = client.request("GET", base) or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("checklist") or []
    pending = [
        i for i in items
        if (i.get("status") or "").lower() not in ("done", "skipped")
    ]
    for item in pending:
        item_id = item.get("id")
        if not item_id:
            continue
        client.request("PATCH", f"{base}/{item_id}", body={"status": "done"})
    return len(pending)


def _cmd_done(args, client, cfg):
    if getattr(args, "force", False):
        closed = _force_close_open_checklist(client, cfg)
        if closed:
            import sys as _sys
            print(f"# --force: {closed} offene Checklist-Item(s) auf done gesetzt", file=_sys.stderr)
    return _patch_status(client, cfg, "done")


def _add_done_args(p):
    _add_optional_task_id(p)
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Offene Checklist-Items automatisch auf done setzen bevor der Task "
            "geschlossen wird. Ohne --force blockt das Backend mit 422 bis alle "
            "Items per `mc checklist done <id>` geschlossen sind."
        ),
    )


def _cmd_patch(args, client, cfg):
    """mc patch --status <status> — Generischer Status-Setter (Alias für die jeweiligen Commands).

    Akzeptiert alle Status-Werte: done, review, in_progress, blocked, failed.
    Fuer blocked/failed sind die dedizierten Commands (mc blocked, mc failed) bevorzugt.
    """
    valid_statuses = ("done", "review", "in_progress", "blocked", "failed")
    if args.status not in valid_statuses:
        raise UsageError(f"--status muss einer von: {', '.join(valid_statuses)} sein")
    return _patch_status(client, cfg, args.status)


def _add_patch_args(p):
    _add_optional_task_id(p)
    p.add_argument("--status", required=True, help="Neuer Status: done | review | in_progress | blocked | failed")


def _cmd_task_get(args, client, cfg):
    """mc task-get — Aktuellen Task-Status abrufen."""
    board_id, task_id = cfg.require_task_context()
    resp = client.request("GET", f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/detail")
    _emit(resp)
    return 0


def _add_vault_search_args(p):
    p.add_argument("query", help="Suchbegriff (FTS5 — Tokens werden gequotet, Dashes/Digits OK)")
    p.add_argument("--type", default=None, help="Filter: knowledge | decision | lesson | reference | concept | journal | deliverable")
    p.add_argument("--agent", default=None, help="Filter: agent slug (z.B. 'researcher')")
    p.add_argument("--limit", type=int, default=20, help="Max Treffer (default 20, le=50)")


def _cmd_vault_search(args, client, cfg):
    """mc vault-search — FTS5-Suche über Vault Notes + Deliverable-Wrappers."""
    query = {"q": args.query, "limit": args.limit}
    if args.type:
        query["type"] = args.type
    if args.agent:
        query["agent"] = args.agent
    resp = client.request("GET", "/api/v1/agent/vault/search", query=query)
    _emit(resp)
    return 0


def _add_vault_related_args(p):
    p.add_argument("task_id", help="Task-UUID (z.B. aus mc me .current_task.id)")


def _cmd_vault_related(args, client, cfg):
    """mc vault-related — alle Notes + Wrappers + Lessons zu einem Task."""
    resp = client.request("GET", f"/api/v1/agent/vault/related/{args.task_id}")
    _emit(resp)
    return 0


def _add_vault_write_args(p):
    p.add_argument("title", help="Note-Titel (3-80 Zeichen)")
    p.add_argument(
        "--content",
        default=None,
        help="Markdown body. Wenn nicht gesetzt: stdin wird gelesen.",
    )
    p.add_argument(
        "--type",
        default="lesson",
        help="knowledge | decision | lesson | reference | concept | journal (default lesson)",
    )
    p.add_argument(
        "--tags",
        default=None,
        help="Komma-getrennte Tags (z.B. 'auth,security,oauth')",
    )
    p.add_argument(
        "--target",
        default=None,
        help=(
            "Optional Zielpfad (z.B. 'global/decisions/foo.md'). "
            "Default: agents/<slug>/<type>s/<title-slug>.md"
        ),
    )
    p.add_argument(
        "--task-id",
        dest="task_id",
        default=None,
        help=(
            "Phase E Task-Klammer. UUID des aktiven Tasks — verlinkt die "
            "Note mit allen anderen Notes/Files desselben Tasks. "
            "Tipp: $(mc me | jq -r .current_task.id) im Subagent-Modus."
        ),
    )
    p.add_argument(
        "--related",
        default=None,
        help=(
            "Komma-getrennte [[note-slug]] Wikilinks (max 8). Empfohlen: "
            "vorher 'mc vault-search' und 2-4 thematisch nächste Hits verlinken."
        ),
    )
    p.add_argument(
        "--idempotency-key",
        dest="idempotency_key",
        default=None,
        help="Optional — verhindert Duplikat-Writes bei Timeout-Retry.",
    )


def _cmd_vault_write(args, client, cfg):
    """mc vault-write — Note via Inbox-Envelope-API schreiben.

    Für Notes die ausserhalb des eigenen Agent-Ordners landen sollen
    (`global/`, `projects/{slug}/`) — direkte FS-Writes dorthin lehnt der
    Watcher ab. Eigene Lessons unter `$AGENT_VAULT_PATH/lessons/` kann
    der Agent direkt mit `cat > ...` schreiben.
    """
    import sys
    content = args.content
    if content is None:
        if sys.stdin.isatty():
            print(
                "Fehler: kein --content gesetzt und kein stdin. "
                "Beispiel: echo '# Body' | mc vault-write 'Titel' --type lesson",
                file=sys.stderr,
            )
            return 2
        content = sys.stdin.read()

    body: dict = {
        "title": args.title,
        "content": content,
        "type": args.type,
    }
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if args.target:
        body["target"] = args.target
    if args.task_id:
        body["task_id"] = args.task_id
    if args.related:
        body["related_notes"] = [r.strip() for r in args.related.split(",") if r.strip()]
    if args.idempotency_key:
        body["idempotency_key"] = args.idempotency_key

    resp = client.request("POST", "/api/v1/agent/vault/note", body=body)
    _emit(resp)
    return 0


def _cmd_review(args, client, cfg):
    return _patch_status(client, cfg, "review")


# ── Reviewer verdicts (approve / reject) ──────────────────────────────────
#
# Thin wrappers over POST /boards/{board_id}/tasks/{task_id}/review
# (backend agent_task_status.agent_review_decision). Give a reviewer agent an
# explicit verb for the two everyday verdicts instead of forcing a raw status
# PATCH: `mc approve` (decision=approve) and `mc reject` (decision=request_changes).
# The backend body requires a non-empty `comment`, so approve supplies a default
# when no --feedback is given; reject hard-requires --feedback locally so the
# author always gets an actionable reason.

def _review_decision(client, cfg, *, decision: str, comment: str):
    board_id, task_id = cfg.require_task_context()
    resp = client.request(
        "POST",
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/review",
        body={"decision": decision, "comment": comment},
    )
    _emit(resp)
    return 0


def _cmd_approve(args, client, cfg):
    """mc approve [--feedback ...] — Review approven (decision=approve)."""
    comment = (getattr(args, "feedback", None) or "").strip() or "Approved."
    return _review_decision(client, cfg, decision="approve", comment=comment)


def _add_approve_args(p):
    _add_optional_task_id(p)
    p.add_argument(
        "--feedback",
        default=None,
        help="Optionale Begruendung/Notiz zum Approve (wird als review-comment gespeichert).",
    )


def _cmd_reject(args, client, cfg):
    """mc reject --feedback ... — Changes anfordern (decision=request_changes)."""
    feedback = (getattr(args, "feedback", None) or "").strip()
    if not feedback:
        raise UsageError(
            "mc reject: --feedback ist Pflicht — der Author braucht einen "
            "konkreten Grund, was geaendert werden soll."
        )
    return _review_decision(client, cfg, decision="request_changes", comment=feedback)


def _add_reject_args(p):
    _add_optional_task_id(p)
    p.add_argument(
        "--feedback",
        required=True,
        help="Pflicht — was muss der Author aendern? Wird als review-comment gespeichert.",
    )


def _cmd_blocked(args, client, cfg):
    if not args.question and not args.description:
        raise UsageError("--question oder --description ist Pflicht bei `mc blocked`.")
    return _patch_status(
        client, cfg, "blocked",
        blocker_type=args.blocker_type,
        blocker_description=args.description,
        blocker_question=args.question,
    )


def _cmd_failed(args, client, cfg):
    board_id, task_id = cfg.require_task_context()
    # Post a comment first so the failure reason is persisted, then flip status.
    client.request(
        "POST",
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        body={"content": args.reason, "comment_type": "blocker"},
    )
    return _patch_status(client, cfg, "failed")


# ── Finish (Reflection + Status atomic) ───────────────────────────────────
#
# Sparky 2026-05-12 ('Race-Test'): nach 'mc done --reason ...' (argparse error)
# hat er Python-urllib gebaut, 3x 409 (kein X-Dispatch-Attempt-Id-Header) + 2x
# 400 (Pflicht-Reflexion fehlt) — 4:34 Min Kampf. Hauptgrund: er kannte
# `mc comment reflection` nicht und der Dispatch-Prompt erwaehnte die
# Pflicht-Reflexion nicht.
#
# `mc finish` macht beides atomar: Reflexion + Status-Change. Validiert
# lokal die 4 Pflichtfelder und Min-Length BEVOR irgendein HTTP-Call passiert,
# damit Agents nicht erst auf 400-Antworten warten muessen.

# Canonical reflection contract — sourced from the SINGLE in-container source of
# truth (mc_cli/reflection.py), which itself is drift-guarded against
# backend/app/constants.py REFLECTION_REQUIRED_FIELDS. Re-exported here as
# module-level names so existing callers/tests (e.g. monkeypatching
# commands.REFLECTION_MIN_CHARS) keep working.
from . import reflection as _reflection  # noqa: E402
REFLECTION_REQUIRED_FIELDS = _reflection.REFLECTION_REQUIRED_FIELDS
REFLECTION_MIN_CHARS = _reflection.REFLECTION_MIN_CHARS


def _validate_reflection(text: str) -> None:
    """Local validation matching backend/app/services/work_context.enforce_reflection.

    Forgiving (B1): the header check is tolerant via mc_cli.reflection —
    #/##/### level, case-insensitive, optional trailing colon, ü↔ue, and
    English aliases ("What was done", ...) all count. Strict canonical input is
    unaffected. This only additively rescues trivial local-model variance that
    the backend gate (existence + length, no header check) accepts anyway.

    Raises UsageError mit klarem Hinweis bevor der HTTP-Call passiert.
    """
    if not text or not text.strip():
        raise UsageError(
            "Reflexions-Text ist leer. Erwartet: alle 4 Pflichtfelder als `## <Feld>` Headers, "
            f"mind. {REFLECTION_MIN_CHARS} Zeichen total."
        )
    # Detect the literal `\n` shell-escape pitfall FIRST: if the text was passed
    # via `mc finish "## … \n## …"` (no $'…' quoting) bash hands us the two-char
    # sequence `\\n` instead of an actual newline. Backend stores it 1:1 and the
    # comment renders as one unbroken line. Must run BEFORE the field check —
    # with everything on one line the tolerant header matcher can't recognise
    # the headers, so it would otherwise mis-report "fields missing". Heuristic:
    # backslash-n appears AND no real newlines split the required headers.
    has_real_newlines = "\n" in text
    has_literal_escape = "\\n" in text
    if has_literal_escape and not has_real_newlines:
        raise UsageError(
            "Reflexion enthaelt literal `\\n` statt echter Newlines (Shell-Escape-Bug). "
            "Bash interpretiert `\"...\\n...\"` nicht — nutze stattdessen:\n"
            "  $'## Was wurde gemacht\\n...\\n## Was hat funktioniert\\n...'\n"
            "oder ein Heredoc:\n"
            "  mc finish \"$(cat <<'EOF'\n"
            "  ## Was wurde gemacht\n  ...\n  EOF\n  )\""
        )
    missing = _reflection.missing_fields(text)
    if missing:
        raise UsageError(
            f"Reflexion unvollstaendig — fehlende Pflichtfelder: {', '.join(missing)}. "
            f"Erwartet: alle 4 Headers '## <Feld>' im Text."
        )
    if len(text) < REFLECTION_MIN_CHARS:
        raise UsageError(
            f"Reflexion zu kurz ({len(text)} Zeichen, mind. {REFLECTION_MIN_CHARS}). "
            f"Inhalt pro Feld ausfuehrlicher beschreiben."
        )


# Status-Mengen aus backend/app/routers/tasks.py VALID_TRANSITIONS — wenn
# Backend-Werte sich aendern, hier auch aendern. _cmd_finish nutzt das fuer
# die preflight-Validierung damit Agents nicht erst auf 4xx-Antworten warten.
_FINISH_ALLOWED_FROM = frozenset({"in_progress", "review"})

# Wenn die letzte reflection vom selben Agent <= dieses Fenster zurueckliegt,
# behandeln wir den `mc finish` Aufruf als idempotenten Retry (kein zweiter
# Comment) — typisches Symptom des 2026-05-16 DNA-PDF Vorfalls, in dem der
# Researcher 3x reflection postete weil `mc finish` hinter einem PATCH-422
# scheiterte und der Agent es jeweils neu probierte.
_REFLECTION_DEDUP_WINDOW_S = 300


def _agent_base(cfg) -> tuple[str, str, str]:
    """Convenience: (board_id, task_id, agent-scoped task base path)."""
    board_id, task_id = cfg.require_task_context()
    return board_id, task_id, f"/api/v1/agent/boards/{board_id}/tasks/{task_id}"


def _preflight_finish(client: Client, cfg, target_status: str) -> dict:
    """Pruefe alles was der Backend-PATCH danach pruefen wuerde — VOR dem
    POST damit ein erfolgloser Versuch keinen Junk-Comment hinterlaesst.

    Returns:
        dict with `should_post_comment` (bool) und `task` (current task data).
        - should_post_comment=False bedeutet: Task ist schon im Zielstatus
          ODER eine recent reflection vom selben Agent existiert. PATCH wird
          trotzdem versucht, falls Status noch nicht final.

    Raises:
        UsageError mit klarem Hinweis bei Pre-Fail (offene Checklist, falscher
        Status, offene Children). Backend-Antworten werden als Quelle zitiert.
    """
    _, _, base = _agent_base(cfg)

    # 1. Task-Status checken — fail wenn Transition unmöglich ist.
    task = client.request("GET", f"{base}/detail")
    current = task.get("status")

    # Idempotenz: wenn Task bereits im Ziel-Status, NICHT erneut posten.
    # Spart Junk-Reflections wenn der Agent `mc finish` zweimal aufruft.
    if current == target_status:
        return {"should_post_comment": False, "task": task, "skip_patch": True}

    if current not in _FINISH_ALLOWED_FROM:
        raise UsageError(
            f"Task-Status ist '{current}' — `mc finish` erwartet 'in_progress' oder 'review'. "
            f"Vermutlich wurde der Task bereits abgeschlossen, gestoppt oder wartet auf Approval."
        )

    # 2. Checklist-Items: jede Open-Item blockt PATCH mit 422.
    items = client.request("GET", f"{base}/checklist") or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("checklist") or []
    pending = [
        i for i in items
        if (i.get("status") or "").lower() not in ("done", "skipped")
    ]
    if pending:
        preview = ", ".join(
            f"{(i.get('id') or '')[:8]} ({i.get('title') or i.get('label') or '?'})"
            for i in pending[:3]
        )
        more = f" + {len(pending) - 3} weitere" if len(pending) > 3 else ""
        raise UsageError(
            f"{len(pending)} Checklist-Item(s) noch offen: {preview}{more}. "
            f"Erst alle mit `mc checklist done <id>` schliessen, dann `mc finish` erneut."
        )

    # 3. Children-Integritaet: Backend's check_children_complete() wuerde mit
    # 400 ablehnen. Wir koennen kein billiges agent-side endpoint dafuer auf-
    # rufen (kein /tasks/{id}/children agent-scoped) — das laesst sich nicht
    # ohne Backend-Aenderung pruefen. Behandlung passiert dann beim PATCH-
    # Fail-Pfad in _cmd_finish (klare Message statt Generic Exit 1).

    # 4. Idempotenz: kürzliche reflection vom gleichen Agent → skip POST.
    # Verhindert dupe-comments wenn der Agent in einem Retry-Loop landet.
    try:
        comments = client.request("GET", f"{base}/comments") or []
        if isinstance(comments, dict):
            comments = comments.get("comments") or []
    except Exception:
        comments = []
    own_recent_reflection = _has_recent_self_reflection(
        comments, agent_id=task.get("assigned_agent_id"),
        window_s=_REFLECTION_DEDUP_WINDOW_S,
    )
    return {
        "should_post_comment": not own_recent_reflection,
        "task": task,
        "skip_patch": False,
        "recent_reflection": own_recent_reflection,
    }


def _has_recent_self_reflection(comments, agent_id, window_s) -> bool:
    """Return True wenn der gleiche Agent in den letzten window_s Sekunden
    bereits eine reflection gepostet hat. Wird genutzt um `mc finish`-Retries
    nicht in Junk-Comments zu verwandeln.

    Bewusst tolerant: ein nicht-parseable Timestamp gilt als 'old' (False),
    fehlender agent_id-Match disqualifiziert. Wir wollen lieber gelegentlich
    ein Duplicate als ein false-positive-skip der einen ehrlichen Retry
    schluckt.
    """
    if not agent_id:
        return False
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    for c in comments:
        if c.get("comment_type") != "reflection":
            continue
        if (c.get("author_type") or "").lower() != "agent":
            continue
        if str(c.get("author_agent_id") or "") != str(agent_id):
            continue
        ts_raw = c.get("created_at")
        if not ts_raw:
            continue
        try:
            ts = _dt.datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
        except (TypeError, ValueError):
            continue
        if (now - ts).total_seconds() <= window_s:
            return True
    return False


def _cmd_finish(args, client, cfg):
    """Reflexion posten + Status auf done (oder review mit --review).

    Anders als die alte Implementierung machen wir jetzt EXPLIZIT pre-flight
    checks (Status, Checklist) BEVOR der POST passiert — eine 422 vom Backend
    fuehrte vorher dazu, dass die Reflexion gepostet, der Status nicht ge-
    aendert wurde, der Agent Exit 1 sah, retried und einen DUPLICATE Comment
    produzierte (siehe 2026-05-16 DNA-PDF Vorfall: 3 reflections in 53 s).

    Idempotenz: wenn eine reflection vom gleichen Agent in den letzten 5 min
    existiert, ueberspringen wir POST und versuchen nur PATCH — der Retry-
    Pfad fuegt keine neuen Comments hinzu.

    PATCH-Fail nach erfolgreichem POST: klare Hinweismeldung (statt nackter
    HTTP-Stacktrace), damit der Agent weiss, dass er mit `mc done` /
    `mc review` separat re-tryen kann.
    """
    _validate_reflection(args.message)
    # Normalize recognised headers to canonical German (idempotent on canonical
    # input) so the POSTed reflection — and thus the memory pipeline's lesson
    # extraction — always sees the canonical `## <German>` headers even when the
    # model wrote English/###/ü variants (B1).
    reflection_content = _reflection.normalize_reflection(args.message)
    target_status = "review" if args.review else "done"
    # --force: erst alle offenen Checklist-Items schliessen, dann normaler
    # Preflight (Status + Children). _preflight_finish wuerde sonst mit
    # UsageError "Checklist-Item(s) noch offen" abbrechen.
    if getattr(args, "force", False):
        closed = _force_close_open_checklist(client, cfg)
        if closed:
            print(f"# --force: {closed} offene Checklist-Item(s) auf done gesetzt", file=sys.stderr)
    pre = _preflight_finish(client, cfg, target_status)

    if pre.get("skip_patch"):
        # Task ist schon im Ziel-Status — beides skipped, klares Signal.
        print(f"# Task ist bereits in Status '{target_status}', nichts zu tun")
        return 0

    if pre["should_post_comment"]:
        board_id, task_id = cfg.require_task_context()
        client.request(
            "POST",
            f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
            body={"comment_type": "reflection", "content": reflection_content},
        )
    else:
        # Eine recent reflection vom gleichen Agent gibt es schon — wir laufen
        # vermutlich im Retry-Pfad. Kein zweiter Comment, nur Status-PATCH.
        print(
            "# recent reflection vom selben Agent gefunden "
            f"(< {_REFLECTION_DEDUP_WINDOW_S}s) — skip POST, nur Status-PATCH"
        )

    try:
        return _patch_status(client, cfg, target_status)
    except Exception as exc:
        # Comment ist ggf. schon im Audit-Trail. Klare Message zum recovery
        # statt nacktem HTTP-Stacktrace, damit der Agent weiss was zu tun ist.
        if pre["should_post_comment"]:
            print(
                f"# Reflexion wurde gepostet, aber Status-PATCH fehlgeschlagen: {exc}\n"
                f"# Retry NUR den Status (kein neuer Comment) mit:\n"
                f"#   mc {'review' if args.review else 'done'}",
                file=sys.stderr,
            )
        raise


def _add_finish_args(p):
    _add_optional_task_id(p)
    p.add_argument(
        "message",
        help=(
            "Reflexions-Text mit 4 Pflicht-Headers: '## Was wurde gemacht\\n...\\n"
            "## Was hat funktioniert\\n...\\n## Was war unklar\\n...\\n"
            "## Lesson fuer Agent-Memory\\n...'"
        ),
    )
    p.add_argument(
        "--review",
        action="store_true",
        help="Status auf 'review' statt 'done' setzen (Code/API/Security-Tasks).",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help=(
            "Offene Checklist-Items automatisch auf done setzen bevor `mc finish` "
            "den Task schliesst. Ohne --force bricht der Pre-Flight mit UsageError ab "
            "wenn Items offen sind."
        ),
    )


def _add_optional_task_id(p):
    """Optional positional task-id für status commands. poll.sh injiziert
    TASK_ID per env, aber `mc ack <task-id>` ist ein verbreitetes
    Bedien-Muster (Boss live-bug 2026-04-25: 'unrecognized arguments').
    Wenn uebergeben: __main__ ueberschreibt cfg.task_id mit diesem Wert.
    """
    p.add_argument(
        "task_id", nargs="?", default=None,
        help="Optional: Task-UUID (default: TASK_ID env-var von poll.sh)",
    )


def _add_blocked_args(p):
    _add_optional_task_id(p)
    p.add_argument("--question", help="Konkrete Frage an den Operator")
    p.add_argument("--description", help="Was ist das Problem?")
    p.add_argument(
        "--blocker-type",
        dest="blocker_type",
        choices=[
            "missing_info", "technical_problem", "decision_needed",
            "permission_needed", "dependency_blocked", "other",
        ],
        default="other",
    )


def _add_failed_args(p):
    _add_optional_task_id(p)
    p.add_argument("--reason", required=True, help="Kurze Fehlerursache")


# ── Comments ──────────────────────────────────────────────────────────────

# Delivered comment_types — gehen ueber /me/poll an den assigned Worker (siehe
# backend/app/comment_types.py DELIVERABLE_SYSTEM_TYPES). Routine-Types
# (message/progress/checkpoint) bleiben Audit-only.
DELIVERED_COMMENT_TYPES = frozenset({
    "blocker", "feedback", "handoff", "resolution",
    "subtask_completed", "install_completed", "install_failed",
})

COMMENT_TYPES = [
    # delivered → assigned Worker wacht via /me/poll auf
    "handoff", "blocker", "feedback", "resolution",
    # silent / audit-only
    "message", "progress", "evidence", "next", "reflection", "report_back",
]


def _cmd_comment(args, client, cfg):
    board_id, task_id = cfg.require_task_context()
    # Guard against the 2026-05-17 Researcher-Bug: agents sometimes wrap their
    # content in {"content": "..."} JSON because they imagine the CLI needs an
    # envelope. The CLI takes plain text. Detect + refuse early with a useful
    # hint so the agent retries with the real text on the next turn.
    stripped = args.message.strip()
    if stripped.startswith("{") and stripped.endswith("}") and '"content"' in stripped:
        try:
            import json as _json
            payload = _json.loads(stripped)
            if isinstance(payload, dict) and "content" in payload:
                raise UsageError(
                    "mc comment: content sieht aus wie JSON-Envelope "
                    '({"content": "..."}). Schick den Markdown direkt: '
                    '`mc comment ' + args.type + ' "<text>"`. Nicht json.dumps drumrum.'
                )
        except _json.JSONDecodeError:
            pass  # nicht valid JSON → durchlassen, kein false-positive
    resp = client.request(
        "POST",
        f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
        body={"comment_type": args.type, "content": args.message},
    )
    # Bug 9 (2026-05-13): Backend liefert `delivery_hint` mit wenn ein
    # `message`-Comment auf einem fremden assigned Task gepostet wurde
    # (silent-fail-Warnung). _emit gibt bei id-Responses nur die id auf stdout
    # aus → wir muessen die Hint hier separat auf stderr loggen, sonst
    # uebersieht der Agent sie.
    if isinstance(resp, dict) and resp.get("delivery_hint"):
        import sys as _sys
        print(f"⚠ delivery_hint: {resp['delivery_hint']}", file=_sys.stderr)
    _emit(resp)
    return 0


def _add_comment_args(p):
    type_help = (
        "Comment-Typ. DELIVERED (Worker wacht auf): handoff, blocker, "
        "feedback, resolution. AUDIT-only (silent): message, progress, "
        "evidence, next, reflection, report_back."
    )
    p.add_argument("type", choices=COMMENT_TYPES, help=type_help)
    p.add_argument("message", help="Inhalt")


# ── Checklist ─────────────────────────────────────────────────────────────

def _resolve_checklist_item_id(client: Client, base: str, item_id: str) -> str:
    """Resolve a full or prefix UUID for a checklist item.

    Verifies the item exists in the current task's checklist before returning
    (catches the 2026-05-17 case where the model hallucinates a UUID — instead
    of a generic backend-404 the agent gets a useful list of real open IDs).
    Raises UsageError on zero or ambiguous matches.
    """
    items = client.request("GET", base) or []
    if isinstance(items, dict):
        items = items.get("items") or items.get("checklist") or []
    matches = [i for i in items if str(i.get("id", "")).startswith(item_id)]
    if not matches:
        # Provide a list of OPEN items so the agent can self-recover.
        open_items = [
            f"{str(i.get('id', ''))[:8]}…  {i.get('title') or i.get('label') or '?'}"
            for i in items
            if (i.get("status") or "").lower() not in ("done", "skipped")
        ]
        listing = ("\n  - " + "\n  - ".join(open_items[:8])) if open_items else " (keine offenen Items)"
        raise UsageError(
            f"Kein Checklist-Item mit ID '{item_id}' gefunden. "
            f"Offene Items:{listing}\n"
            f"Tipp: Nutze `mc checklist list` für alle IDs, dann "
            f"`mc checklist done <id-prefix>` mit den ersten 8 Hex-Stellen."
        )
    if len(matches) > 1:
        ids = ", ".join(str(m["id"])[:12] for m in matches)
        raise UsageError(f"Präfix '{item_id}' ist nicht eindeutig — passt auf: {ids}")
    return str(matches[0]["id"])


def _cmd_checklist(args, client, cfg):
    board_id, task_id = cfg.require_task_context()
    base = f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/checklist"

    # Default to "list" wenn kein subcommand angegeben — verhindert das
    # 2026-05-17 Pattern wo der Agent `mc checklist` ohne Args ruft als
    # Recovery-Versuch und argparse Exit 2 wirft (kryptisch).
    action = args.action or "list"

    if action == "add":
        body = {"items": [{"title": args.title, "sort_order": args.order}]}
        resp = client.request("POST", base, body=body)
        _emit(resp)
    elif action == "done":
        item_id = _resolve_checklist_item_id(client, base, args.item_id)
        resp = client.request("PATCH", f"{base}/{item_id}", body={"status": "done"})
        _emit(resp)
    elif action == "skip":
        # 2026-07-08: an agent can hit a checklist item it physically cannot
        # do (a live Vercel deploy needing npm/node = a Deployer's job, not
        # an omp agent's). Before this the only options were `done` (a lie)
        # or leaving `mc finish` blocked forever. Reuses the existing
        # `skipped` status — `_preflight_finish` already treats it as
        # non-blocking, no backend change needed.
        item_id = _resolve_checklist_item_id(client, base, args.item_id)
        resp = client.request("PATCH", f"{base}/{item_id}", body={"status": "skipped"})
        _emit(resp)
        if getattr(args, "reason", None):
            board_id, task_id = cfg.require_task_context()
            client.request(
                "POST",
                f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/comments",
                body={
                    "comment_type": "progress",
                    "content": f"Checklist-Item {item_id} skipped: {args.reason}",
                },
            )
    elif action == "list":
        resp = client.request("GET", base)
        _emit(resp)
    else:
        raise UsageError(f"Unbekannte checklist action: {action}")
    return 0


def _add_checklist_args(p):
    sub = p.add_subparsers(dest="action", required=False)
    p_add = sub.add_parser("add", help="Item hinzufügen")
    p_add.add_argument("title")
    p_add.add_argument("--order", type=int, default=0)
    p_done = sub.add_parser("done", help="Item als erledigt markieren")
    p_done.add_argument("item_id")
    p_skip = sub.add_parser(
        "skip", help="Item ueberspringen (z.B. out-of-role, nicht durch diesen Agent erledigbar)"
    )
    p_skip.add_argument("item_id")
    p_skip.add_argument("--reason", default=None, help="Grund, wird als Comment gepostet")
    sub.add_parser("list", help="Aktuelle Checklist zeigen")


# ── Clarification / Help / Deliverable / Memory ──────────────────────────

def _cmd_question(args, client, cfg):
    board_id, _ = cfg.require_task_context()
    options = [o.strip() for o in args.options.split(",")] if args.options else None
    resp = client.request(
        "POST",
        f"/api/v1/agent/boards/{board_id}/clarification",
        body={"question": args.question, "options": options},
    )
    _emit(resp)
    return 0


def _add_question_args(p):
    p.add_argument("question")
    p.add_argument("--options", help="Komma-separierte Antwort-Optionen")


def _cmd_help(args, client, cfg):
    board_id, _ = cfg.require_task_context()
    resp = client.request(
        "POST",
        f"/api/v1/agent/boards/{board_id}/help-request",
        body={
            "needed_role": args.role,
            "title": args.title,
            "context": args.context,
            "priority": args.priority,
        },
    )
    _emit(resp)
    return 0


def _add_help_args(p):
    p.add_argument("role", help="Rolle die helfen soll (z.B. reviewer, developer)")
    p.add_argument("--title", required=True)
    p.add_argument("--context", required=True)
    p.add_argument("--priority", choices=["low", "medium", "high", "critical"])


def _cmd_delegate(args, client, cfg):
    """Atomar: Subtask erstellen + Parent-Task blockieren + warten auf Callback.

    Ersetzt `mc task-create + mc blocked getrennt`. Erzeugt KEINE Operator-Approval.
    """
    import uuid as _uuid

    board_id, _task_id = cfg.require_task_context()

    if not args.description or len(args.description.strip()) < 10:
        raise UsageError(
            "--description ist Pflicht (mind. 10 Zeichen) — beschreibe klar was der "
            "Ziel-Agent liefern soll, Guardrails und Definition of Done."
        )

    # --to: UUID durchlassen, Name via Board-Agent-Lookup aufloesen
    target_id: str | None = None
    try:
        _uuid.UUID(args.to)
        target_id = args.to
    except ValueError:
        agents_resp = client.request("GET", f"/api/v1/agent/boards/{board_id}/agents")
        if not isinstance(agents_resp, list):
            raise UsageError(f"Agent-Lookup fehlgeschlagen: {agents_resp}")
        wanted = args.to.lower()
        matches = [a for a in agents_resp if a.get("name", "").lower() == wanted]
        if len(matches) > 1:
            dup = ", ".join(f"{m.get('name', '?')} ({m['id']})" for m in matches)
            raise UsageError(
                f"Mehrere Agents mit Name '{args.to}': {dup}. "
                f"Nutze die UUID direkt statt des Namens."
            )
        if not matches:
            names = ", ".join(a.get("name", "?") for a in agents_resp)
            raise UsageError(
                f"Kein Agent '{args.to}' auf diesem Board. Verfuegbar: {names}"
            )
        target_id = matches[0]["id"]

    body = {
        "title": args.title,
        "description": args.description,
        "assigned_agent_id": target_id,
        "callback": not args.no_callback,
    }
    if args.priority:
        body["priority"] = args.priority

    resp = client.request(
        "POST",
        f"/api/v1/agent/boards/{board_id}/delegate",
        body=body,
    )
    _emit(resp)
    return 0


def _add_delegate_args(p):
    p.add_argument("title", help="Kurzer Titel fuer den Subtask")
    p.add_argument(
        "--to",
        required=True,
        help="Ziel-Agent (Name oder UUID) — z.B. --to Researcher",
    )
    p.add_argument(
        "--description",
        required=True,
        help="Was soll der Agent liefern? Ziel, Kontext, Guardrails, DoD.",
    )
    p.add_argument(
        "--priority",
        choices=["low", "medium", "high", "critical"],
        help="Priority fuer den Subtask (default: erbt vom Parent)",
    )
    p.add_argument(
        "--no-callback",
        action="store_true",
        help="Fire-and-Forget — Parent bleibt in_progress, kein Auto-Resume bei Subtask-done",
    )


def _cmd_deliverable(args, client, cfg):
    board_id, task_id = cfg.require_task_context()
    import os as _os
    import shutil as _shutil

    # Backend akzeptiert lokale Pfade unter zwei Task-scoped Prefixen:
    #   /deliverables/<task_id>/        Agent-Container (selbst-erzeugte Files)
    #   /shared-deliverables/<task_id>/ mc-playwright Sidecar (PDF, Screenshots)
    # URLs (http/https) + content-only sind ebenfalls erlaubt.
    path = args.path
    agent_prefix = f"/deliverables/{task_id}/"
    sidecar_prefix = f"/shared-deliverables/{task_id}/"
    deliverables_root = f"/deliverables/{task_id}"
    accepted_local_prefixes = (agent_prefix, sidecar_prefix)

    if path:
        is_url = path.startswith(("http://", "https://"))
        if is_url:
            pass  # durchlassen
        elif path.startswith("/"):
            if not any(path.startswith(p) for p in accepted_local_prefixes):
                # Absoluter Pfad ausserhalb der erlaubten Zonen:
                # - Wenn Quelldatei existiert + /deliverables/<task_id>/ schreibbar → auto-copy.
                # - /shared-deliverables/ ist read-only fuer Agents → kein auto-copy.
                # - Sonst: klare UsageError mit Fix-Optionen.
                filename = _os.path.basename(path.rstrip("/"))
                dest = _os.path.join(deliverables_root, filename)
                src_exists = _os.path.isfile(path)
                dest_writable = _os.path.isdir(deliverables_root) or _os.access(
                    _os.path.dirname(deliverables_root), _os.W_OK
                )
                if src_exists and dest_writable:
                    try:
                        _os.makedirs(deliverables_root, exist_ok=True)
                        _shutil.copy2(path, dest)
                        print(
                            f"(auto-copied {path} → {dest})",
                            file=sys.stderr,
                        )
                        path = dest
                    except OSError as e:
                        raise UsageError(
                            f"Deliverable-Pfad muss unter {agent_prefix} oder {sidecar_prefix} liegen.\n"
                            f"  Gegeben:   {path}\n"
                            f"  Auto-Copy nach {dest} fehlgeschlagen: {e}\n"
                            f"Manueller Fix: cp '{path}' '{dest}' && retry."
                        )
                else:
                    raise UsageError(
                        f"Deliverable-Pfad muss unter einem dieser Prefixe liegen:\n"
                        f"  {agent_prefix}  (selbst erzeugte Dateien)\n"
                        f"  {sidecar_prefix}  (mc pdf / mc verify Output)\n"
                        f"  Gegeben: {path}\n\n"
                        f"Fix-Optionen:\n"
                        f"  (a) Datei hinkopieren: cp '{path}' '{agent_prefix}{filename}'\n"
                        f"  (b) Nur Text: --type document --content \"<voller Text>\" (ohne --path)\n"
                        f"  (c) URL: --type url --path 'https://...'"
                    )
            # akzeptierter Prefix — durchlassen
        else:
            # Relativer Pfad → auto-rewrite nach /deliverables/<task_id>/<rel>.
            # Strip literal "./" prefix (nur das, nicht lstrip der alles frisst).
            relative = path[2:] if path.startswith("./") else path
            # Legacy-Form ".mc-deliverables/<task_id>/foo" → extrahiere Dateiname
            marker = f".mc-deliverables/{task_id}/"
            if marker in relative:
                relative = relative.split(marker, 1)[1]
            path = _os.path.join("/deliverables", str(task_id), relative)

    body = {
        "deliverable_type": args.type,
        "title": args.title,
        "path": path,
        "description": args.description,
        "content": args.content,
        "is_reusable": args.reusable,
        "task_id": str(task_id),  # Override fuer Backend-Resolver
    }
    from .errors import ClientError as _ClientError
    try:
        resp = client.request("POST", "/api/v1/agent/me/deliverable", body=body)
    except _ClientError as e:
        if "HTTP 404" in str(e):
            # Altes Backend ohne /me/* — Fallback auf Board-scoped URL
            body.pop("task_id", None)
            resp = client.request(
                "POST",
                f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/deliverables",
                body=body,
            )
        else:
            raise
    _emit(resp)
    return 0


def _add_deliverable_args(p):
    p.add_argument("--title", required=True)
    p.add_argument("--path", help="Relativer Pfad im Workspace (bevorzugt)")
    p.add_argument(
        "--type",
        dest="type",
        choices=["screenshot", "file", "url", "artifact", "document", "data", "video"],
        default="file",
    )
    p.add_argument("--description")
    p.add_argument("--content", help="Inline-Inhalt statt Datei")
    p.add_argument("--reusable", action="store_true", help="Cross-Project wiederverwendbar")


def _cmd_deliverable_get(args, client, cfg):
    """Einzelnes Deliverable mit vollem content-Feld lesen.

    Per default liefert der LIST-Endpoint (GET /deliverables) kein content —
    Response-Size-Begrenzung. Dieser Command ist die kanonische Verifikations-
    Route: nach dem Registrieren eines Deliverables kann der Agent den
    gespeicherten Inhalt hier gegenchecken (content_length, content-Body) statt
    spekulativ re-zu-registrieren.

    Nutzung:
      mc deliverable-get <deliverable-uuid>
      mc deliverable-get <uuid> --task <task-uuid>   # andere Task als die aktive

    Returns JSON mit content + content_length + meta-fields.
    """
    board_id, task_id = cfg.require_task_context()
    target_task = args.task or str(task_id)
    resp = client.request(
        "GET",
        f"/api/v1/agent/boards/{board_id}/tasks/{target_task}/deliverables/{args.deliverable_id}",
    )
    _emit(resp)
    return 0


def _add_deliverable_get_args(p):
    p.add_argument(
        "deliverable_id",
        help="UUID des Deliverables (aus POST /deliverables Response)",
    )
    p.add_argument(
        "--task",
        default=None,
        help=(
            "Optional: Task-UUID ueberschreiben (default: aktuelle Task aus "
            "/tmp/mc-context.env). Nuetzlich wenn der Deliverable-Owner ein "
            "anderer Task ist als der aktive — z.B. Boss liest ein Researcher-"
            "Deliverable aus dem abgeschlossenen Research-Subtask."
        ),
    )


def _cmd_verify(args, client, cfg):
    """Visual Verification — Screenshots + Performance-Metriken via mc-playwright Service.

    Nutzt den zentralen Playwright-Container im MC-Backend. Agents brauchen KEIN
    eigenes Browser-Setup mehr (Bug 3 vom 2026-04-22 — Tester verbrachte 12+ Min
    mit Chrome-Path-Suche).

    Einfache Nutzung:
      mc verify --url https://t2.argyelan.ch
      mc verify --url ... --viewports desktop,mobile,tablet --no-scroll
      mc verify --url ... --caption "Final check"

    Eingeloggter Zustand + Interaktion (seit 2026-04-23):
      # JWT direkt in localStorage setzen:
      mc verify --url http://caddy/ --auth-token "$JWT"

      # Login via Vault-Credential (credential_type=login, url gesetzt):
      mc verify --url http://caddy/ --login-as <credential-uuid>

      # Modal oeffnen + auf Render warten + nur Viewport schiessen:
      mc verify --url http://caddy/ --auth-token "$JWT" \\
        --click 'button:has-text("Neuer Auftrag")' \\
        --wait-for '[data-testid="create-task-modal"]' \\
        --no-full-page

      # Mehrere Clicks + Fills kombinieren (jedes Flag wiederholbar):
      mc verify --url http://caddy/ \\
        --fill 'input[name=email]=mark@example.com' \\
        --fill 'input[name=password]=secret' \\
        --click 'button[type=submit]'
    """
    board_id, task_id = cfg.require_task_context()
    viewports = [v.strip() for v in args.viewports.split(",") if v.strip()]

    # Interaktionen aufbauen — Reihenfolge wie auf der Command-Line (argparse
    # behaelt die Reihenfolge innerhalb einer Action-Liste, aber mischt sie
    # nicht zwischen --click und --fill. Fuer komplexe Skripte sollte man den
    # Endpoint direkt benutzen. Fuer 90% der Faelle reicht: zuerst fills, dann
    # clicks, am Ende wait).
    interactions: list[dict] = []
    for sel in (args.fill or []):
        if "=" not in sel:
            raise UsageError(f"--fill braucht Form 'selector=value' (bekam: {sel!r})")
        selector, value = sel.split("=", 1)
        interactions.append({"action": "fill", "selector": selector, "value": value})
    for sel in (args.click or []):
        interactions.append({"action": "click", "selector": sel})

    body: dict = {
        "url": args.url,
        "viewports": viewports,
        "scroll": not args.no_scroll,
        "metrics": not args.no_metrics,
        "send_to_telegram": not args.no_telegram,
        "full_page": not args.no_full_page,
    }
    if args.caption:
        body["caption"] = args.caption
    if args.auth_token:
        body["auth_token"] = args.auth_token
    if args.login_as:
        body["credential_id"] = args.login_as
    if interactions:
        body["interactions"] = interactions
    if args.wait_for:
        body["wait_for_selector"] = args.wait_for
    if args.force_telegram_resend:
        body["force_telegram_resend"] = True

    resp = client.request(
        "POST",
        f"/api/v1/agent/tasks/{task_id}/visual-verify",
        body=body,
    )
    _emit(resp)
    return 0


def _cmd_pdf(args, client, cfg):
    """Markdown/HTML → PDF via mc-playwright Sidecar. Zero local setup.

    Warum nicht selbst chromium/puppeteer installieren: der Agent-Container
    ist x86-Linux-Image unter Rosetta auf Mac M4 (ARM). Jedes lokal
    runtergeladene Chrome-Binary scheitert an `rosetta error: failed to open
    elf`. Der mc-playwright Sidecar laeuft ARM-nativ — `mc pdf` ist die
    sichere Alternative.

    Input: Markdown aus Datei (`--file`) oder stdin (`-`).
    Output: PDF als TaskDeliverable automatisch registriert, Deliverable-ID
    wird im Response zurueckgegeben (fuer `mc telegram --file <id>`).

    Beispiele:
      mc pdf --title "Q1 Report" --file report.md
      cat report.md | mc pdf --title "Q1 Report" -
      mc pdf --title "Report" --file report.md --filename-prefix q1-report
    """
    # Source: markdown content
    import sys as _sys
    if args.file == "-" or (args.file is None and not _sys.stdin.isatty()):
        markdown = _sys.stdin.read()
    elif args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                markdown = f.read()
        except OSError as e:
            raise UsageError(f"Markdown-Datei nicht lesbar: {args.file} ({e})")
    else:
        raise UsageError(
            "Kein Markdown-Input. Nutze --file <pfad.md> oder pipe via stdin (--file -)."
        )

    if not markdown.strip():
        raise UsageError("Markdown-Input ist leer.")

    # Task-Context: optional — Backend resolviert via spawn_session_key wenn nicht gesetzt
    try:
        board_id, task_id = cfg.require_task_context()
    except Exception:
        board_id, task_id = None, None

    body: dict = {
        "title": args.title,
        "markdown": markdown,
        "filename_prefix": args.filename_prefix or "report",
    }
    if task_id:
        body["task_id"] = str(task_id)  # Override fuer Backend-Resolver
    if args.description:
        body["description"] = args.description
    if args.custom_css:
        try:
            with open(args.custom_css, "r", encoding="utf-8") as f:
                body["custom_css"] = f.read()
        except OSError as e:
            raise UsageError(f"CSS-Datei nicht lesbar: {args.custom_css} ({e})")

    from .errors import ClientError as _ClientError
    try:
        resp = client.request("POST", "/api/v1/agent/me/pdf", body=body)
    except _ClientError as e:
        if "HTTP 404" in str(e) and board_id and task_id:
            # Altes Backend ohne /me/* — Fallback auf Board-scoped URL
            body.pop("task_id", None)
            resp = client.request(
                "POST",
                f"/api/v1/agent/boards/{board_id}/tasks/{task_id}/pdf",
                body=body,
            )
        else:
            raise
    _emit(resp)
    return 0


def _add_pdf_args(p):
    p.add_argument("--title", required=True, help="PDF-Titel (erscheint im Dokument-Header)")
    p.add_argument(
        "--file",
        default=None,
        help="Pfad zur Markdown-Datei, oder '-' fuer stdin",
    )
    p.add_argument(
        "--filename-prefix",
        dest="filename_prefix",
        default="report",
        help="Dateiname-Prefix (default: 'report' → report.pdf)",
    )
    p.add_argument("--description", help="Beschreibung fuer das Deliverable-Listing")
    p.add_argument(
        "--custom-css",
        dest="custom_css",
        help="Optional: Pfad zu custom CSS-Datei (wird zum Default-Stylesheet dazu geladen)",
    )


def _add_verify_args(p):
    p.add_argument("--url", required=True, help="Target-URL (https://...)")
    p.add_argument(
        "--viewports",
        default="desktop,mobile",
        help="Komma-separiert: desktop|mobile|tablet (default: desktop,mobile)",
    )
    p.add_argument("--no-scroll", action="store_true", help="keine Scroll-Positionen")
    p.add_argument("--no-metrics", action="store_true", help="keine Performance-Metriken")
    p.add_argument(
        "--no-telegram", action="store_true",
        help="Screenshots NICHT an Reports-Telegram senden (nur als Deliverable speichern)",
    )
    p.add_argument("--caption", help="HTML-Caption fuer erstes Telegram-Bild")
    # --- Interaktions-Mode ----------------------------------------------------
    p.add_argument(
        "--auth-token",
        help="JWT wird als localStorage['mc_auth_token'] gesetzt vor navigate (fuer MC UI eingeloggt).",
    )
    p.add_argument(
        "--login-as",
        help="Credential-UUID aus Vault (type=login, url gesetzt). Backend resolved und macht Form-Login.",
    )
    p.add_argument(
        "--click",
        action="append",
        help="CSS-Selector — wird vor Screenshot geklickt. Wiederholbar fuer mehrere Clicks.",
    )
    p.add_argument(
        "--fill",
        action="append",
        help="Form-Fill als 'selector=value'. Wiederholbar. Wird VOR --click ausgefuehrt.",
    )
    p.add_argument(
        "--wait-for",
        help="CSS-Selector — wartet bis sichtbar nach Navigate+Interactions, vor Screenshot.",
    )
    p.add_argument(
        "--no-full-page",
        action="store_true",
        help="Nur Viewport schiessen (nicht full-page). Sinnvoll fuer Modal-only Shots.",
    )
    p.add_argument(
        "--force-telegram-resend",
        action="store_true",
        help="Sendet Screenshots an Telegram auch wenn diese Task schon einen "
             "visual-verify Telegram-Send hatte. Default: per-task Dedup "
             "(24h TTL) verhindert Duplikate bei mehreren Selbst-Verify-Runs.",
    )


def _cmd_telegram(args, client, cfg):
    """Sende einen strukturierten Report an den Reports-Telegram-Chat des Operators.

    Text kommt als Positional-Arg ODER via stdin (bei `-` oder leerem arg). Unterstuetzt
    HTML-Tags: <b>fett</b>, <i>kursiv</i>, <code>code</code>, <a href="...">link</a>.

    Nutzung fuer Info-Delivery am Task-Ende — NICHT fuer Status-Spam.

    Task-Context: Im Subagent-Dispatch-Modus haben Worker keinen `current_task_id`
    im Agent-Record. Die CLI liest die Task-ID aus /tmp/mc-context.env (wird von
    poll.sh beim Dispatch gesetzt) und schickt sie als `task_id` mit — damit das
    Backend das Report-Sent-Flag auf dem korrekten Task setzen kann.
    """
    if args.text in (None, "", "-"):
        # stdin lesen (ermoeglicht heredoc + pipes)
        import sys as _sys
        text = _sys.stdin.read().strip()
        if not text:
            raise UsageError(
                "Kein Text. Nutze: `mc telegram \"<message>\"` oder pipe via stdin."
            )
    else:
        text = args.text.strip()

    body: dict = {"text": text}
    photo_id = getattr(args, "photo", None)
    file_id = getattr(args, "file", None)
    if photo_id and file_id:
        raise UsageError(
            "--photo und --file schliessen sich aus. Nutze EINEN von beiden."
        )
    if photo_id:
        # Photo-Anhang via Screenshot-Deliverable. Backend macht sendPhoto
        # statt sendMessage und nimmt text als Caption (1024 Zeichen Telegram-Limit).
        body["deliverable_id"] = photo_id
    elif file_id:
        # File-Anhang (PDF/Office/ZIP/...) via beliebiges Deliverable mit Pfad.
        # Backend macht sendDocument (keine Kompression, max 50 MB).
        body["document_deliverable_id"] = file_id
    try:
        _, task_id = cfg.require_task_context()
        body["task_id"] = str(task_id)
    except Exception:
        # Kein Task-Context verfuegbar (z.B. Board-Lead-Session ohne poll) — Backend
        # faellt zurueck auf agent.current_task_id.
        pass

    from .errors import ClientError as _ClientError
    try:
        resp = client.request("POST", "/api/v1/agent/me/telegram", body=body)
    except _ClientError as e:
        if "HTTP 404" in str(e):
            # Altes Backend ohne /me/* — Fallback auf alten Pfad
            resp = client.request("POST", "/api/v1/agent/telegram/send", body=body)
        else:
            raise
    _emit(resp)
    return 0


def _add_telegram_args(p):
    p.add_argument(
        "text",
        nargs="?",
        default=None,
        help=(
            "Report-Text (HTML erlaubt: <b>, <i>, <code>, <a>). "
            "Leer lassen oder '-' um via stdin zu lesen."
        ),
    )
    p.add_argument(
        "--photo",
        default=None,
        metavar="DELIVERABLE_ID",
        help=(
            "Optional: Screenshot-Deliverable als Telegram-Photo anhaengen "
            "(text wird zur Caption, 1024 Zeichen Telegram-Limit). "
            "Deliverable muss type=screenshot sein und ein resolvable File haben. "
            "Beispiel: mc telegram \"Caption\" --photo <deliverable-uuid>"
        ),
    )
    p.add_argument(
        "--file",
        default=None,
        metavar="DELIVERABLE_ID",
        help=(
            "Optional: Datei (PDF/Excel/PowerPoint/Word/ZIP/...) als Telegram-Document "
            "anhaengen — keine Kompression, max 50 MB. text wird zur Caption "
            "(1024 Zeichen Telegram-Limit). Deliverable muss einen File-Pfad haben "
            "(type != url, type != data). Mutex zu --photo. "
            "Beispiel: mc telegram \"Q1 Report anbei\" --file <deliverable-uuid>"
        ),
    )


def _cmd_memory(args, client, cfg):
    # Prefer new /me/memory/search endpoint; fall back to /memory/query if 404.
    try:
        resp = client.request(
            "GET", "/api/v1/agent/me/memory/search",
            query={"q": args.query, "limit": args.limit},
        )
    except Exception:
        resp = client.request(
            "POST", "/api/v1/agent/memory/query",
            body={"query": args.query, "limit": args.limit},
        )
    _emit(resp)
    return 0


def _add_memory_args(p):
    sub = p.add_subparsers(dest="action", required=True)
    p_search = sub.add_parser("search", help="Qdrant + Board-Memory Suche")
    p_search.add_argument("query")
    p_search.add_argument("--limit", type=int, default=10)


# ── Recovery (ADR-024) ────────────────────────────────────────────────────

def _cmd_recover(args, client, cfg):
    """Agent-initiated recovery — holt den aktuellen Task-Prompt + Recovery-
    Context vom Backend und gibt den Prompt auf stdout aus. Generiert eine
    frische dispatch_attempt_id, aber mutiert KEINEN Task-Status.

    Schreibt auch /tmp/mc-context.env mit TASK_ID/BOARD_ID/X_DISPATCH_ATTEMPT_ID
    damit nachfolgende `mc ack`/`mc done`-Calls den korrekten Header senden.

    Nutzen:
    - Nach Container/Session-Restart: `mc recover` zeigt dir wo du warst
    - Wenn du unsicher bist welcher Task gerade laeuft
    """
    resp = client.request("GET", "/api/v1/agent/me/active-task-recovery")
    active = resp.get("active") if isinstance(resp, dict) else None
    if not active:
        print("Kein aktiver Task — du bist frei.", file=sys.stderr)
        return 0
    task = resp["task"]
    # Context-File so schreiben dass nachfolgende mc-Calls den Header setzen
    # koennen. poll.sh schreibt diese Datei normalerweise bei new_task —
    # beim manuellen `mc recover` ausserhalb von poll.sh muss der CLI das
    # selbst tun.
    try:
        with open("/tmp/mc-context.env", "w", encoding="utf-8") as f:
            f.write(f"TASK_ID={task['id']}\n")
            f.write(f"BOARD_ID={task.get('board_id') or ''}\n")
            f.write(f"X_DISPATCH_ATTEMPT_ID={task.get('dispatch_attempt_id') or ''}\n")
    except OSError as e:
        print(f"Warn: /tmp/mc-context.env nicht schreibbar: {e}", file=sys.stderr)
    # Prompt auf stdout (agent liest das) — kein JSON-Wrapping
    print(f"# Recovery-Prompt fuer Task {task['id']}")
    print(f"# Title: {task['title']}  |  Status: {task.get('status', '?')}")
    print(f"# dispatch_attempt_id: {task['dispatch_attempt_id']}")
    print(f"# Context-File: /tmp/mc-context.env aktualisiert")
    print()
    print(task["prompt"])
    return 0


def _add_recover_args(p):
    pass  # keine args — nur GET


# ── Self-Lookup ──────────────────────────────────────────────────────────

def _cmd_me(args, client, cfg):
    """Eigene Agent-Info abrufen (id, role, scopes, current_task, cli_skills/plugins).

    Kanonischer Weg um sich zu orientieren statt GET /agent/agents/<id> (existiert
    nicht) oder /agent/me mit tippen Variationen durchzuprobieren. Jede Agent-Auth
    reicht, keine spezielle Scope-Anforderung.

    Nutzung:
      mc me

    Returns: JSON mit id, name, role, is_board_lead, board_id, agent_runtime,
    scopes, cli_skills, cli_plugins, skill_filter, current_task, provision_status.
    """
    resp = client.request("GET", "/api/v1/agent/me")
    _emit(resp)
    return 0


def _add_me_args(p):
    pass


# ── Plugin Management (Board Lead) ───────────────────────────────────────

def _resolve_target_agent_id(name_or_id: str, client: Client, cfg: Config) -> str:
    """Akzeptiert UUID oder Agent-Name (case-insensitive), returniert UUID als str.

    Suche im aktuellen Board (aus cfg). Raise UsageError wenn nichts oder
    mehreres matched.
    """
    import re
    _UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
    if _UUID_RE.match(name_or_id):
        return name_or_id

    board_id, _ = cfg.require_task_context()
    resp = client.request("GET", f"/api/v1/agent/boards/{board_id}/agents")
    agents = resp if isinstance(resp, list) else resp.get("agents", [])
    needle = name_or_id.lower()
    matches = [a for a in agents if (a.get("name") or "").lower() == needle]
    if not matches:
        available = sorted(a.get("name", "") for a in agents)
        raise UsageError(
            f"Agent '{name_or_id}' nicht im Board gefunden. "
            f"Verfuegbar: {', '.join(available) or '(keine)'}"
        )
    if len(matches) > 1:
        raise UsageError(
            f"Mehrere Agents mit Name '{name_or_id}' — nutze die UUID stattdessen."
        )
    return matches[0]["id"]


def _cmd_plugin_list(args, client, cfg):
    """Alle im shared cache installierten Plugins auflisten (was du zuweisen kannst).

    Nutzung:
      mc plugin-list

    Nur fuer Board Leads (agents:manage + is_board_lead=true).
    """
    resp = client.request("GET", "/api/v1/agent/plugins")
    _emit(resp)
    return 0


def _add_plugin_list_args(p):
    pass


def _cmd_plugin_show(args, client, cfg):
    """Aktuelle Plugin-Allowlist eines Worker-Agents anzeigen.

    Nutzung:
      mc plugin-show <agent-name-or-uuid>

    Return: agent_id, agent_name, cli_plugins (null/[]/list).
    """
    target_id = _resolve_target_agent_id(args.agent, client, cfg)
    resp = client.request("GET", f"/api/v1/agent/agents/{target_id}/plugins")
    _emit(resp)
    return 0


def _add_plugin_show_args(p):
    p.add_argument("agent", help="Agent-Name (z.B. Davinci) oder UUID")


def _cmd_plugin_assign(args, client, cfg):
    """Plugin-Allowlist eines Workers setzen (replace, nicht merge).

    Nutzung:
      mc plugin-assign <agent> [<plugin-key> ...]
      mc plugin-assign <agent> --all              # null = alle installierten
      mc plugin-assign <agent> --none             # [] = keine
      mc plugin-assign <agent> plugin1 plugin2 --restart

    Das ersetzt die ganze Allowlist. Wenn du EIN Plugin ergaenzen willst:
    erst `mc plugin-show <agent>` → cli_plugins kopieren → neuen Key anhaengen
    → `mc plugin-assign ...`.

    --restart (optional): Worker-Session neu starten damit Plugins sofort
    aktiv sind. ACHTUNG: laufender Task-Kontext geht verloren.
    """
    target_id = _resolve_target_agent_id(args.agent, client, cfg)

    if args.all_plugins and args.no_plugins:
        raise UsageError("--all und --none schliessen sich aus")
    if args.all_plugins:
        plugins: list[str] | None = None
    elif args.no_plugins:
        plugins = []
    elif args.plugins:
        plugins = list(args.plugins)
    else:
        raise UsageError(
            "Gib Plugin-Keys an, oder --all (alle installierten) / --none (keine)."
        )

    body = {"cli_plugins": plugins, "restart_worker": args.restart}
    resp = client.request("PATCH", f"/api/v1/agent/agents/{target_id}/plugins", body=body)
    _emit(resp)
    return 0


def _add_plugin_assign_args(p):
    p.add_argument("agent", help="Agent-Name (z.B. Davinci) oder UUID")
    p.add_argument(
        "plugins",
        nargs="*",
        help="Plugin-Keys (z.B. superpowers@claude-plugins-official) — explizite Allowlist",
    )
    p.add_argument(
        "--all",
        dest="all_plugins",
        action="store_true",
        help="cli_plugins=null → Worker bekommt alle installierten Plugins",
    )
    p.add_argument(
        "--none",
        dest="no_plugins",
        action="store_true",
        help="cli_plugins=[] → Worker bekommt keine Plugins",
    )
    p.add_argument(
        "--restart",
        action="store_true",
        help="Nach Zuweisung Worker-Session neu starten (Plugins sofort aktiv — Task-Kontext weg)",
    )


def _cmd_plugin_unassign(args, client, cfg):
    """Ein Plugin aus der Allowlist eines Workers entfernen (erhaltend).

    Nutzung:
      mc plugin-unassign <agent> <plugin-key> [--restart]

    Macht intern: GET current cli_plugins → filter → PATCH. Wenn cli_plugins
    aktuell `null` ist (= alle) wird das gewuenschte Plugin entfernt indem
    eine explizite Liste ALLER anderen Plugins gesetzt wird.
    """
    target_id = _resolve_target_agent_id(args.agent, client, cfg)

    current = client.request("GET", f"/api/v1/agent/agents/{target_id}/plugins")
    cli_plugins = current.get("cli_plugins")

    if cli_plugins is None:
        # Worker hat "alle installierten" — expand to explicit list minus target
        all_installed = client.request("GET", "/api/v1/agent/plugins")
        keys = [p["key"] for p in all_installed.get("plugins", [])]
        new_list = [k for k in keys if k != args.plugin]
    elif cli_plugins == []:
        # Hatte sowieso keine, nichts zu tun
        print(f"Agent {current.get('agent_name')} hat bereits keine Plugins — no-op")
        return 0
    else:
        if args.plugin not in cli_plugins:
            print(f"Plugin '{args.plugin}' nicht in aktueller Liste — no-op")
            return 0
        new_list = [p for p in cli_plugins if p != args.plugin]

    body = {"cli_plugins": new_list, "restart_worker": args.restart}
    resp = client.request("PATCH", f"/api/v1/agent/agents/{target_id}/plugins", body=body)
    _emit(resp)
    return 0


def _add_plugin_unassign_args(p):
    p.add_argument("agent", help="Agent-Name oder UUID")
    p.add_argument("plugin", help="Plugin-Key der entfernt werden soll")
    p.add_argument(
        "--restart",
        action="store_true",
        help="Nach Entfernen Worker-Session neu starten",
    )


def _cmd_worker_restart(args, client, cfg):
    """Worker-Session eines Workers manuell neu starten (claude in tmux Window 0).

    Nutzung:
      mc worker-restart <agent-name-or-uuid>

    Sinnvoll wenn: Plugins wurden ohne --restart zugewiesen, Worker steckt
    in altem State, Settings-Datei geaendert. Container bleibt up, poll.sh
    laeuft weiter, nur die claude-Session wird gekillt + neu gestartet.

    ACHTUNG: laufender Task-Kontext geht verloren. Pruefe vorher dass der
    Agent idle ist.
    """
    target_id = _resolve_target_agent_id(args.agent, client, cfg)
    resp = client.request("POST", f"/api/v1/agent/agents/{target_id}/worker/restart")
    _emit(resp)
    return 0


def _add_worker_restart_args(p):
    p.add_argument("agent", help="Agent-Name (z.B. Davinci) oder UUID")


# ── mc remember ──────────────────────────────────────────────────────────

def _cmd_remember(args, client, cfg):
    """mc remember — Quick vault note shortcut.

    Thin wrapper around vault-write with sensible defaults:
    - Title auto-generated from text (first 60 chars)
    - Type defaults to 'lesson'
    - Idempotency key auto-generated from content hash
    - Task ID auto-read from $TASK_ID env var
    """
    import hashlib
    import os

    text = args.text.strip() if args.text else ""
    if not text and not args.content:
        print("Fehler: Text darf nicht leer sein.", file=sys.stderr)
        return 2

    if args.content:
        title = text
        content = args.content
    else:
        title = text[:60] + "..." if len(text) > 60 else text
        content = text

    idem_key = "remember-" + hashlib.sha256(content.encode()).hexdigest()[:16]

    task_id = args.task_id or os.environ.get("TASK_ID")

    body: dict = {
        "title": title,
        "content": content,
        "type": args.type,
        "idempotency_key": idem_key,
    }
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]
    if task_id:
        body["task_id"] = task_id
    if args.related:
        body["related_notes"] = [r.strip() for r in args.related.split(",") if r.strip()]

    resp = client.request("POST", "/api/v1/agent/vault/note", body=body)
    target = resp.get("expected_target", "")
    if resp.get("ok"):
        print(f"Gespeichert: {target}")
        return 0
    else:
        _emit(resp)
        return 1


def _add_remember_args(p):
    p.add_argument("text", help="Was du dir merken willst (wird Title + Content wenn kein --content)")
    p.add_argument("--content", default=None, help="Separater Body (text wird dann zum Titel)")
    p.add_argument("--type", default="lesson", help="lesson | knowledge | reference (default: lesson)")
    p.add_argument("--tags", default=None, help="Komma-getrennte Tags (z.B. 'docker,scopes')")
    p.add_argument("--related", default=None, help="Komma-getrennte [[wikilinks]]")
    p.add_argument("--task-id", dest="task_id", default=None, help="Task-UUID (default: $TASK_ID)")


# ── mc file-answer ──────────────────────────────────────────────────────

def _cmd_file_answer(args, client, cfg):
    """mc file-answer — Save a query-answer pair as a vault knowledge note."""
    import sys

    query = (args.query or "").strip()
    answer = (args.answer or "").strip()
    if not query:
        print("Fehler: Frage darf nicht leer sein.", file=sys.stderr)
        return 2
    if not answer:
        print("Fehler: Antwort darf nicht leer sein.", file=sys.stderr)
        return 2

    body: dict = {
        "query": query,
        "answer": answer,
        "type": args.type,
    }
    if args.sources:
        body["source_note_ids"] = [s.strip() for s in args.sources.split(",") if s.strip()]
    if args.tags:
        body["tags"] = [t.strip() for t in args.tags.split(",") if t.strip()]

    resp = client.request("POST", "/api/v1/agent/vault/file-answer", body=body)
    target = resp.get("expected_target", "")
    if resp.get("ok"):
        print(f"Gespeichert: {target}")
    else:
        _emit(resp)
    return 0


def _add_file_answer_args(p):
    p.add_argument("query", help="Die Frage die recherchiert wurde")
    p.add_argument("--answer", required=True, help="Die Antwort / Recherche-Ergebnis")
    p.add_argument("--sources", default=None, help="Komma-getrennte UUIDs der Quell-Notes")
    p.add_argument("--type", default="knowledge", help="knowledge | lesson | note (default: knowledge)")
    p.add_argument("--tags", default=None, help="Komma-getrennte Tags")


# ── mc docs (local — no network, no client call) ──────────────────────────

def _docs_dir():
    """Directory L2 reference docs are read from.

    Resolution order:
      1. MC_DOCS_DIR — explicit override (tests, or a manual `mc docs` call
         pointed at a specific tree).
      2. $CLAUDE_CONFIG_DIR/docs — host agents (Boss/Hermes/Jarvis, ADR host-
         runtime) run with CLAUDE_CONFIG_DIR=<agent_dir>/claude-config and
         HOME=the operator's real home, NOT the agent's — docker_agent_sync.
         write_reference_docs() writes into exactly this directory
         (config_dir/docs), so this is where a host agent's own docs live.
      3. ~/.claude/docs — docker cli-bridge agents don't set CLAUDE_CONFIG_DIR
         and HOME is their real container home, where write_reference_docs()
         wrote the same docs/ tree.
    Falls through to the next candidate if a directory doesn't exist (e.g.
    CLAUDE_CONFIG_DIR is set but the agent hasn't been synced yet) instead of
    a permanent dead end.
    """
    import os
    from pathlib import Path

    override = os.environ.get("MC_DOCS_DIR")
    if override:
        return Path(override)

    claude_config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if claude_config_dir:
        candidate = Path(claude_config_dir) / "docs"
        if candidate.is_dir():
            return candidate

    return Path.home() / ".claude" / "docs"


_VALID_TOPIC_RE = re.compile(r"^[a-z][a-z0-9-]*$")


def _cmd_docs(args, client, cfg):
    """mc docs [topic] — read a local L2 reference doc. No network call.

    Without an argument: prints docs/INDEX.md (or, if that's missing, a list
    of the .md files found in the docs dir). With a topic: prints
    docs/<topic>.md to stdout. Purely local file reads — `client`/`cfg` are
    unused, matching the "local verb" contract (no mc-context.env / token
    needed to read reference docs).
    """
    import sys

    docs_dir = _docs_dir()
    topic = getattr(args, "topic", None)

    if topic and not _VALID_TOPIC_RE.match(topic):
        # Reject before touching the filesystem — a topic like "../../etc/passwd"
        # or an absolute path must never be joined into docs_dir (path
        # traversal). Only lowercase-alnum-hyphen slugs are legitimate topics.
        available = sorted(p.stem for p in docs_dir.glob("*.md")) if docs_dir.is_dir() else []
        print(f"mc docs: ungueltiges Topic '{topic}' — nur [a-z][a-z0-9-]* erlaubt.", file=sys.stderr)
        if available:
            print(f"Verfuegbare Topics: {', '.join(available)}", file=sys.stderr)
        return 1

    if not topic:
        index_path = docs_dir / "INDEX.md"
        if index_path.is_file():
            print(index_path.read_text(encoding="utf-8"))
            return 0
        topics = sorted(p.stem for p in docs_dir.glob("*.md")) if docs_dir.is_dir() else []
        if not topics:
            print(f"Keine Reference Docs gefunden unter {docs_dir}.", file=sys.stderr)
            return 1
        print("Verfuegbare Topics:")
        for t in topics:
            print(f"  mc docs {t}")
        return 0

    doc_path = docs_dir / f"{topic}.md"
    if not doc_path.is_file():
        available = sorted(p.stem for p in docs_dir.glob("*.md")) if docs_dir.is_dir() else []
        print(f"mc docs: Topic '{topic}' nicht gefunden unter {docs_dir}.", file=sys.stderr)
        if available:
            print(f"Verfuegbare Topics: {', '.join(available)}", file=sys.stderr)
        else:
            print("Keine Reference Docs gefunden — noch nicht synced?", file=sys.stderr)
        return 1

    print(doc_path.read_text(encoding="utf-8"))
    return 0


def _add_docs_args(p):
    p.add_argument("topic", nargs="?", default=None, help="Topic-Slug (z.B. 'telegram'). Ohne Arg: INDEX/Topic-Liste.")


# ── Registry ──────────────────────────────────────────────────────────────

_STATUS_ENDPOINT = ("PATCH /boards/{board_id}/tasks/{task_id}",)

REGISTRY: dict[str, CommandSpec] = {
    "ack": CommandSpec(
        name="ack",
        help="Dispatch bestätigen (status → in_progress)",
        endpoints=_STATUS_ENDPOINT,
        scope="tasks:write",
        handler=_cmd_ack,
        add_args=_add_optional_task_id,
    ),
    "done": CommandSpec(
        name="done",
        help="Task abschliessen (status → done). --force schliesst offene Checklist-Items automatisch.",
        endpoints=_STATUS_ENDPOINT,
        scope="tasks:write",
        handler=_cmd_done,
        add_args=_add_done_args,
    ),
    "patch": CommandSpec(
        name="patch",
        help="Status setzen — mc patch --status done|review|in_progress|blocked|failed",
        endpoints=_STATUS_ENDPOINT,
        scope="tasks:write",
        handler=_cmd_patch,
        add_args=_add_patch_args,
    ),
    "task-get": CommandSpec(
        name="task-get",
        help="Aktuellen Task-Status und Details abrufen",
        endpoints=("GET /boards/{board_id}/tasks/{task_id}/detail",),
        scope="tasks:read",
        handler=_cmd_task_get,
        add_args=_add_optional_task_id,
    ),
    "vault-search": CommandSpec(
        name="vault-search",
        help="FTS5-Suche über Vault (Notes + Deliverable-Wrappers + PDF-Text)",
        endpoints=("GET /agent/vault/search",),
        scope="vault:read",
        handler=_cmd_vault_search,
        add_args=_add_vault_search_args,
    ),
    "vault-related": CommandSpec(
        name="vault-related",
        help="Alle Notes + Wrappers + Lessons mit derselben task_id (Phase E Task-Klammer)",
        endpoints=("GET /agent/vault/related/{task_id}",),
        scope="vault:read",
        handler=_cmd_vault_related,
        add_args=_add_vault_related_args,
    ),
    "vault-write": CommandSpec(
        name="vault-write",
        help="Vault-Note via Inbox-API schreiben (für cross-agent shared paths)",
        endpoints=("POST /agent/vault/note",),
        scope="vault:write",
        handler=_cmd_vault_write,
        add_args=_add_vault_write_args,
    ),
    "review": CommandSpec(
        name="review",
        help="Task zu Review übergeben (status → review)",
        endpoints=_STATUS_ENDPOINT,
        scope="tasks:write",
        handler=_cmd_review,
        add_args=_add_optional_task_id,
    ),
    "approve": CommandSpec(
        name="approve",
        help="Review approven (decision=approve). Optional --feedback als Notiz.",
        endpoints=("POST /boards/{board_id}/tasks/{task_id}/review",),
        scope="tasks:write",
        handler=_cmd_approve,
        add_args=_add_approve_args,
    ),
    "reject": CommandSpec(
        name="reject",
        help="Changes anfordern (decision=request_changes). --feedback ist Pflicht.",
        endpoints=("POST /boards/{board_id}/tasks/{task_id}/review",),
        scope="tasks:write",
        handler=_cmd_reject,
        add_args=_add_reject_args,
    ),
    "finish": CommandSpec(
        name="finish",
        help="Reflexion posten + Status setzen (atomic) — vermeidet 'Pflicht-Reflexion fehlt' 400",
        endpoints=(
            "POST /boards/{board_id}/tasks/{task_id}/comments",
            "PATCH /boards/{board_id}/tasks/{task_id}",
        ),
        scope="tasks:write",
        handler=_cmd_finish,
        add_args=_add_finish_args,
    ),
    "blocked": CommandSpec(
        name="blocked",
        help="Task blockieren mit Frage/Beschreibung",
        endpoints=_STATUS_ENDPOINT,
        scope="tasks:write",
        handler=_cmd_blocked,
        add_args=_add_blocked_args,
    ),
    "failed": CommandSpec(
        name="failed",
        help="Task als failed markieren",
        endpoints=(
            "PATCH /boards/{board_id}/tasks/{task_id}",
            "POST /boards/{board_id}/tasks/{task_id}/comments",
        ),
        scope="tasks:write",
        handler=_cmd_failed,
        add_args=_add_failed_args,
    ),
    "comment": CommandSpec(
        name="comment",
        help="Kommentar posten (progress/blocker/feedback/resolution[terminal]/...)",
        endpoints=("POST /boards/{board_id}/tasks/{task_id}/comments",),
        scope="tasks:write",
        handler=_cmd_comment,
        add_args=_add_comment_args,
    ),
    "checklist": CommandSpec(
        name="checklist",
        help="Checklist verwalten (add/done/list)",
        endpoints=(
            "POST /boards/{board_id}/tasks/{task_id}/checklist",
            "PATCH /boards/{board_id}/tasks/{task_id}/checklist/{item_id}",
            "GET /boards/{board_id}/tasks/{task_id}/checklist",
        ),
        scope="tasks:write",
        handler=_cmd_checklist,
        add_args=_add_checklist_args,
    ),
    "question": CommandSpec(
        name="question",
        help="Klärungsfrage an den Operator stellen",
        endpoints=("POST /boards/{board_id}/clarification",),
        scope="tasks:help",  # backend require_scope(Scope.TASKS_HELP) — Aligned with agent_scoped.py:2179
        handler=_cmd_question,
        add_args=_add_question_args,
    ),
    "help": CommandSpec(
        name="help",
        help="Hilfe von anderem Agent anfordern",
        endpoints=("POST /boards/{board_id}/help-request",),
        scope="tasks:help",  # backend require_scope(Scope.TASKS_HELP) — Aligned with agent_scoped.py:2074
        handler=_cmd_help,
        add_args=_add_help_args,
    ),
    "delegate": CommandSpec(
        name="delegate",
        help="Subtask an anderen Agent delegieren + Parent blockieren (atomic, keine Approval)",
        endpoints=("POST /boards/{board_id}/delegate",),
        scope="tasks:create",
        handler=_cmd_delegate,
        add_args=_add_delegate_args,
    ),
    "deliverable": CommandSpec(
        name="deliverable",
        help="Deliverable registrieren",
        endpoints=(
            "POST /me/deliverable",                                      # primary (auto-resolves task)
            "POST /boards/{board_id}/tasks/{task_id}/deliverables",      # fallback (old backend)
        ),
        scope="tasks:write",
        handler=_cmd_deliverable,
        add_args=_add_deliverable_args,
    ),
    "deliverable-get": CommandSpec(
        name="deliverable-get",
        help="Deliverable-Inhalt (mit content-Feld) lesen — Verifikations-Route",
        endpoints=("GET /boards/{board_id}/tasks/{task_id}/deliverables/{deliverable_id}",),
        scope="tasks:read",
        handler=_cmd_deliverable_get,
        add_args=_add_deliverable_get_args,
    ),
    "telegram": CommandSpec(
        name="telegram",
        help="Report an den Telegram-Reports-Chat des Operators senden (Info-Delivery am Task-Ende)",
        endpoints=(
            "POST /me/telegram",    # primary (auto-resolves task)
            "POST /telegram/send",  # fallback (old backend)
        ),
        scope="chat:write",
        handler=_cmd_telegram,
        add_args=_add_telegram_args,
    ),
    "verify": CommandSpec(
        name="verify",
        help="Visual Verification: Screenshots + Metrics + Telegram-Anhang via mc-playwright",
        endpoints=("POST /tasks/{task_id}/visual-verify",),
        scope="chat:write",
        handler=_cmd_verify,
        add_args=_add_verify_args,
    ),
    "pdf": CommandSpec(
        name="pdf",
        help="Markdown → PDF via mc-playwright Sidecar (keine lokale Chrome-Installation noetig)",
        endpoints=(
            "POST /me/pdf",                                    # primary (auto-resolves task)
            "POST /boards/{board_id}/tasks/{task_id}/pdf",     # fallback (old backend)
        ),
        scope="tasks:write",
        handler=_cmd_pdf,
        add_args=_add_pdf_args,
    ),
    "memory": CommandSpec(
        name="memory",
        help="Memory-Suche (Qdrant + Board-Memory)",
        endpoints=(
            "GET /me/memory/search",           # added by A3
            "POST /memory/query",              # fallback
        ),
        scope="memory:read",
        handler=_cmd_memory,
        add_args=_add_memory_args,
    ),
    "recover": CommandSpec(
        name="recover",
        help="Aktuellen Task-Prompt nach Restart/Crash neu holen",
        endpoints=("GET /me/active-task-recovery",),
        scope="tasks:read",
        handler=_cmd_recover,
        add_args=_add_recover_args,
    ),
    "me": CommandSpec(
        name="me",
        help="Eigene Agent-Info (id, role, scopes, current_task, cli_skills/plugins)",
        endpoints=("GET /me",),
        scope="heartbeat",  # jede Agent-Auth reicht; heartbeat ist der universellste Scope
        handler=_cmd_me,
        add_args=_add_me_args,
    ),
    "plugin-list": CommandSpec(
        name="plugin-list",
        help="Shared-Cache Plugins auflisten (Board-Lead-only)",
        endpoints=("GET /plugins",),
        scope="agents:manage",
        handler=_cmd_plugin_list,
        add_args=_add_plugin_list_args,
    ),
    "plugin-show": CommandSpec(
        name="plugin-show",
        help="Aktuelle Plugin-Allowlist eines Workers zeigen",
        endpoints=("GET /agents/{target_agent_id}/plugins",),
        scope="agents:manage",
        handler=_cmd_plugin_show,
        add_args=_add_plugin_show_args,
    ),
    "plugin-assign": CommandSpec(
        name="plugin-assign",
        help="Plugin-Allowlist eines Workers setzen (replace)",
        endpoints=("PATCH /agents/{target_agent_id}/plugins",),
        scope="agents:manage",
        handler=_cmd_plugin_assign,
        add_args=_add_plugin_assign_args,
    ),
    "plugin-unassign": CommandSpec(
        name="plugin-unassign",
        help="Ein Plugin aus der Allowlist eines Workers entfernen",
        endpoints=("PATCH /agents/{target_agent_id}/plugins",),
        scope="agents:manage",
        handler=_cmd_plugin_unassign,
        add_args=_add_plugin_unassign_args,
    ),
    "worker-restart": CommandSpec(
        name="worker-restart",
        help="Worker-Session eines CLI-Bridge Agents neu starten (claude in tmux reload)",
        endpoints=("POST /agents/{target_agent_id}/worker/restart",),
        scope="agents:manage",
        handler=_cmd_worker_restart,
        add_args=_add_worker_restart_args,
    ),
    "remember": CommandSpec(
        name="remember",
        help="Schnell etwas im Vault merken (Shortcut fuer vault-write)",
        endpoints=("POST /agent/vault/note",),
        scope="vault:write",
        handler=_cmd_remember,
        add_args=_add_remember_args,
    ),
    "file-answer": CommandSpec(
        name="file-answer",
        help="Recherche-Ergebnis als Vault-Note speichern",
        endpoints=("POST /agent/vault/file-answer",),
        scope="vault:write",
        handler=_cmd_file_answer,
        add_args=_add_file_answer_args,
    ),
    "docs": CommandSpec(
        name="docs",
        help="L2 Reference Doc lesen (lokal, kein Netzwerk) — mc docs [topic]",
        endpoints=(),
        scope="",
        handler=_cmd_docs,
        add_args=_add_docs_args,
    ),
}
