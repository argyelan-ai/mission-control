"""Proof gate ("Beweis-Tor") — a card only reaches review/done with evidence.

Why: the most expensive pattern in this fleet is the false finish report — an
agent reports "PR open, CI green, reviewer approved" and none of it is true.
Until now a human (the board lead or the operator) found those by hand, days
later. This gate checks the claim on the server, at the moment of the status
change, so ``mc finish --force`` or a raw PATCH cannot walk around it.

What is checked (only for cards that carry a PR):

  a) ``pr_url`` points at a PR that really exists on GitHub.
  b) the branch head on GitHub (remote SHA) equals the SHA the agent reported
     (``head_sha`` in the PATCH; the mc CLI sends it from the local checkout).
  c) CI is green (``statusCheckRollup``).
  d) after the last "change" feedback (review decision ``changes_requested``
     in MC, or a GitHub review in state ``CHANGES_REQUESTED``) a new commit
     landed on the PR.
  e) a reviewer the card relies on commented himself — looked up in the MC
     database first (MC-internal reviews live there), GitHub second.

Modes (``PROOF_GATE_MODE``): ``off`` | ``warn`` | ``enforce``.
``warn`` logs, writes a comment on the card and emits a ``task.proof_gate``
event — the transition still happens. ``enforce`` answers 422 for any *hard*
finding. ``PROOF_GATE_ENFORCE_AFTER`` (ISO date) flips ``warn`` to ``enforce``
automatically once that date is reached — the rollout is "7 days warn, then
enforce" without a second deploy.

Fail-open, but loud: without a GitHub token, or when GitHub cannot be reached,
nothing is blocked — the card gets a visible warning instead, because an
outage must not freeze every PR card on the board (same reasoning as the
ADR gate's inconclusive-lookup path).

GitHub access reuses ``pr_merge_monitor`` (``parse_pr_url`` + ``_run_gh``)
and ``github_config`` (vault > env token).

``gefangen_von`` (caught by): every finding the gate records carries
``gefangen_von="tor"`` in the event detail. The other catchers write the same
field on their events (``task.review_rejected``: ``rolle`` / ``boss`` /
``mark``), so "who caught the false finish report" can be counted from the
activity log without a schema change. ``live`` is reserved for findings from
a live probe after deploy.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from fastapi import HTTPException
from sqlmodel import select

logger = logging.getLogger("mc.proof_gate")

PROOF_GATE_EVENT = "task.proof_gate"

#: Allowed values of the ``gefangen_von`` field (who caught a false finish).
CAUGHT_BY_VALUES = ("tor", "rolle", "boss", "mark", "live")

#: Finding levels. ``hard`` blocks in enforce mode, ``notice`` is shown on the
#: card but never blocks, ``info`` is only logged.
HARD, NOTICE, INFO = "hard", "notice", "info"

_GH_PR_FIELDS = "number,state,url,headRefName,headRefOid,statusCheckRollup,commits,reviews,comments"

_CI_RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
_CI_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}

_NOT_FOUND_MARKERS = (
    "could not resolve to a pullrequest",
    "could not resolve to a repository",
    "http 404",
    "not found",
)


@dataclass
class Finding:
    code: str
    level: str
    message: str

    def as_dict(self) -> dict:
        return {"code": self.code, "level": self.level, "message": self.message}


@dataclass
class ProofGateResult:
    mode: str
    target_status: str
    pr_url: str | None = None
    checked: bool = False
    findings: list[Finding] = field(default_factory=list)

    @property
    def hard(self) -> list[Finding]:
        return [f for f in self.findings if f.level == HARD]

    @property
    def visible(self) -> list[Finding]:
        return [f for f in self.findings if f.level in (HARD, NOTICE)]

    @property
    def blocks(self) -> bool:
        return self.mode == "enforce" and bool(self.hard)


class PrNotFound(RuntimeError):
    pass


class GithubUnavailable(RuntimeError):
    pass


# ── Mode ────────────────────────────────────────────────────────────────


def effective_mode(today: date | None = None) -> str:
    """``off`` | ``warn`` | ``enforce`` from settings, incl. the auto-flip date."""
    from app.config import settings

    mode = (getattr(settings, "proof_gate_mode", "warn") or "warn").strip().lower()
    if mode not in ("off", "warn", "enforce"):
        logger.warning("PROOF_GATE_MODE=%r unbekannt — benutze 'warn'", mode)
        mode = "warn"
    if mode == "warn":
        raw = (getattr(settings, "proof_gate_enforce_after", "") or "").strip()
        if raw:
            try:
                flip = date.fromisoformat(raw)
            except ValueError:
                logger.warning("PROOF_GATE_ENFORCE_AFTER=%r ist kein ISO-Datum — ignoriert", raw)
            else:
                if (today or datetime.now(timezone.utc).date()) >= flip:
                    mode = "enforce"
    return mode


def caught_by_for_actor(actor_agent) -> str:
    """``gefangen_von`` for a review rejection: operator / board lead / role."""
    if actor_agent is None:
        return "mark"
    if getattr(actor_agent, "is_board_lead", False):
        return "boss"
    return "rolle"


# ── GitHub access (reuses pr_merge_monitor) ─────────────────────────────


async def _github_token(session) -> str:
    from app.services.github_config import resolve_github_config

    try:
        config = await resolve_github_config(session)
    except Exception:  # noqa: BLE001 — the resolver must never break a PATCH
        logger.warning("proof_gate: GitHub-Konfiguration nicht lesbar", exc_info=True)
        return ""
    return config.token or ""


async def fetch_pr_evidence(owner: str, repo: str, number: int, token: str) -> dict:
    """One ``gh pr view`` with everything the gate needs.

    Raises ``PrNotFound`` when GitHub says the PR (or repo) does not exist and
    ``GithubUnavailable`` for every other failure — the two must not be
    confused: the first is a false claim, the second is an outage.
    """
    import os

    from app.services import pr_merge_monitor

    env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}
    rc, out, err = await pr_merge_monitor._run_gh(
        "gh", "pr", "view", str(number),
        "--repo", f"{owner}/{repo}",
        "--json", _GH_PR_FIELDS,
        env=env,
    )
    if rc != 0:
        low = (err or "").lower()
        if any(marker in low for marker in _NOT_FOUND_MARKERS):
            raise PrNotFound(err.strip()[:300])
        raise GithubUnavailable((err or f"gh exit {rc}").strip()[:300])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GithubUnavailable(f"gh pr view: JSON nicht lesbar ({exc})") from exc
    if not isinstance(data, dict):
        raise GithubUnavailable("gh pr view: unerwartete Antwort")
    return data


# ── Pure helpers ────────────────────────────────────────────────────────


def _parse_ts(raw) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    try:
        value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def classify_ci(rollup) -> str:
    """``green`` | ``red`` | ``pending`` | ``none`` for a statusCheckRollup."""
    if not rollup:
        return "none"
    pending = False
    for check in rollup:
        if not isinstance(check, dict):
            continue
        conclusion = (check.get("conclusion") or "").upper()
        state = (check.get("state") or "").upper()  # StatusContext
        status = (check.get("status") or "").upper()  # CheckRun
        if conclusion in _CI_RED or state in ("FAILURE", "ERROR"):
            return "red"
        if conclusion in _CI_OK or state == "SUCCESS":
            continue
        if status and status != "COMPLETED":
            pending = True
            continue
        if state in ("PENDING", "EXPECTED"):
            pending = True
            continue
        if conclusion == "" and status == "COMPLETED":
            continue
        pending = True
    return "pending" if pending else "green"


def _last_commit_at(pr: dict) -> datetime | None:
    stamps = []
    for commit in pr.get("commits") or []:
        if not isinstance(commit, dict):
            continue
        ts = _parse_ts(commit.get("committedDate") or commit.get("authoredDate"))
        if ts:
            stamps.append(ts)
    return max(stamps) if stamps else None


def _github_change_requests(pr: dict) -> list[datetime]:
    out = []
    for review in pr.get("reviews") or []:
        if isinstance(review, dict) and (review.get("state") or "").upper() == "CHANGES_REQUESTED":
            ts = _parse_ts(review.get("submittedAt"))
            if ts:
                out.append(ts)
    return out


def _github_authors(pr: dict) -> set[str]:
    authors = set()
    for item in (pr.get("reviews") or []) + (pr.get("comments") or []):
        if not isinstance(item, dict):
            continue
        login = ((item.get("author") or {}).get("login") or "").strip().lower()
        if login:
            authors.add(login)
    return authors


# ── PR reference ────────────────────────────────────────────────────────


async def resolve_pr_ref(session, task, updates: dict) -> tuple[str | None, tuple[str, str, int] | None]:
    """(pr_url, (owner, repo, number)) for the card, PATCH values first.

    ``(None, None)`` means "this card has no PR" — the gate does not apply.
    ``(url, None)`` means a PR reference exists but is not a GitHub PR URL.
    """
    from app.services.pr_merge_monitor import parse_pr_url

    pr_url = updates.get("pr_url") or getattr(task, "pr_url", None)
    pr_number = updates.get("pr_number") or getattr(task, "pr_number", None)
    if pr_url:
        return pr_url, parse_pr_url(pr_url)
    if not pr_number:
        return None, None

    full_name = None
    if getattr(task, "repo_id", None):
        from app.models.repo import Repo

        repo = await session.get(Repo, task.repo_id)
        full_name = getattr(repo, "full_name", None) if repo else None
    if not full_name and getattr(task, "project_id", None):
        from app.models.board import Project

        project = await session.get(Project, task.project_id)
        full_name = getattr(project, "github_repo_name", None) if project else None
    if not full_name or "/" not in full_name:
        return None, None
    owner, repo_name = full_name.split("/", 1)
    url = f"https://github.com/{owner}/{repo_name}/pull/{int(pr_number)}"
    return url, (owner, repo_name, int(pr_number))


# ── MC-side evidence ────────────────────────────────────────────────────


async def _last_review_entry_at(session, task) -> datetime | None:
    from app.models.task import TaskEvent

    row = (await session.exec(
        select(TaskEvent.created_at)
        .where(TaskEvent.task_id == task.id, TaskEvent.to_status == "review")
        .order_by(TaskEvent.created_at.desc())
        .limit(1)
    )).first()
    return _parse_ts(row) if row else None


async def _agent_commented(session, task, agent_id: uuid.UUID, since: datetime | None) -> bool:
    from app.models.task import TaskComment

    task_ids = [task.id]
    reviewed = getattr(task, "source_task_id", None)
    if reviewed:
        task_ids.append(reviewed)
    rows = (await session.exec(
        select(TaskComment.created_at).where(
            TaskComment.task_id.in_(task_ids),  # type: ignore[union-attr]
            TaskComment.author_agent_id == agent_id,
        )
    )).all()
    for created in rows:
        ts = _parse_ts(created)
        if since is None or ts is None or ts >= since:
            return True
    return False


async def _named_reviewer_in_mc(session, task, name: str, since: datetime | None) -> bool | None:
    """True/False when ``name`` is an MC agent on this board, None otherwise."""
    from sqlalchemy import func

    from app.models.agent import Agent

    agent = (await session.exec(
        select(Agent).where(
            Agent.board_id == task.board_id,
            func.lower(Agent.name) == name.strip().lower(),
        )
    )).first()
    if agent is None:
        return None
    return await _agent_commented(session, task, agent.id, since)


# ── The verdict ─────────────────────────────────────────────────────────


async def evaluate_proof_gate(
    session,
    task,
    *,
    target_status: str,
    old_status: str,
    actor_agent,
    updates: dict,
    reviewer: str | None = None,
    head_sha: str | None = None,
    head_branch: str | None = None,
    reviewer_comment_in_flight: bool = False,
) -> ProofGateResult:
    """Collect findings. Never raises for GitHub trouble (fail-open, loud)."""
    mode = effective_mode()
    result = ProofGateResult(mode=mode, target_status=target_status)
    if mode == "off" or target_status not in ("review", "done") or target_status == old_status:
        return result

    pr_url, ref = await resolve_pr_ref(session, task, updates)
    result.pr_url = pr_url
    if pr_url is None:
        return result  # no PR on this card — nothing to prove here
    result.checked = True

    if ref is None:
        result.findings.append(Finding(
            "pr_url_invalid", HARD,
            f"pr_url '{pr_url}' ist keine GitHub-PR-Adresse "
            "(erwartet: https://github.com/<owner>/<repo>/pull/<nr>).",
        ))
        return result

    token = await _github_token(session)
    if not token:
        result.findings.append(Finding(
            "no_github_token", NOTICE,
            "Kein GitHub-Token konfiguriert — Beweis-Tor konnte PR, CI und "
            "Reviews NICHT pruefen (Statuswechsel ungeprueft zugelassen). "
            "Token setzen: Einstellungen → GitHub.",
        ))
        return result

    owner, repo, number = ref
    try:
        pr = await fetch_pr_evidence(owner, repo, number, token)
    except PrNotFound as exc:
        result.findings.append(Finding(
            "pr_not_found", HARD,
            f"PR {owner}/{repo}#{number} existiert auf GitHub nicht ({exc}).",
        ))
        return result
    except GithubUnavailable as exc:
        result.findings.append(Finding(
            "github_unavailable", NOTICE,
            f"GitHub nicht erreichbar — PR {owner}/{repo}#{number} ungeprueft "
            f"zugelassen ({exc}).",
        ))
        return result

    # (b) remote SHA == reported SHA
    remote_sha = (pr.get("headRefOid") or "").strip().lower()
    remote_branch = (pr.get("headRefName") or "").strip()
    reported = (head_sha or "").strip().lower()
    if not reported:
        result.findings.append(Finding(
            "sha_not_reported", INFO,
            "Kein lokaler Stand (head_sha) gemeldet — SHA-Vergleich uebersprungen.",
        ))
    elif head_branch and remote_branch and head_branch.strip() != remote_branch:
        result.findings.append(Finding(
            "sha_other_branch", INFO,
            f"Gemeldeter Stand stammt von Branch '{head_branch}', der PR laeuft auf "
            f"'{remote_branch}' — SHA-Vergleich uebersprungen.",
        ))
    elif remote_sha and not (remote_sha.startswith(reported) or reported.startswith(remote_sha)):
        result.findings.append(Finding(
            "sha_mismatch", HARD,
            f"Gemeldeter Stand {reported[:12]} ist nicht der Stand auf GitHub "
            f"({remote_sha[:12]}, Branch '{remote_branch}'). Erst pushen, dann melden.",
        ))

    # (c) CI green
    ci = classify_ci(pr.get("statusCheckRollup"))
    if ci == "red":
        result.findings.append(Finding(
            "ci_red", HARD, f"CI auf PR #{number} ist rot — erst gruen machen, dann melden.",
        ))
    elif ci == "pending":
        result.findings.append(Finding(
            "ci_pending", HARD if target_status == "done" else INFO,
            f"CI auf PR #{number} laeuft noch — 'done' braucht gruenes CI.",
        ))
    elif ci == "none":
        result.findings.append(Finding("ci_none", INFO, f"PR #{number} hat keine CI-Checks."))

    # (d) a new commit after the last change request
    change_times = list(_github_change_requests(pr))
    if getattr(task, "review_decision", None) == "changes_requested":
        decided = _parse_ts(getattr(task, "review_decided_at", None))
        if decided:
            change_times.append(decided)
    if change_times:
        last_change = max(change_times)
        last_commit = _last_commit_at(pr)
        if last_commit is None:
            result.findings.append(Finding(
                "commit_unknown", INFO, "Commit-Zeiten des PR nicht lesbar — Pruefung (d) uebersprungen.",
            ))
        elif last_commit <= last_change:
            result.findings.append(Finding(
                "no_commit_after_change", HARD,
                f"Seit dem letzten Change-Feedback ({last_change:%Y-%m-%d %H:%M} UTC) "
                f"kam kein neuer Commit auf PR #{number} (letzter: "
                f"{last_commit:%Y-%m-%d %H:%M} UTC).",
            ))

    # (e) the reviewer commented himself
    since = await _last_review_entry_at(session, task)
    if reviewer:
        in_mc = await _named_reviewer_in_mc(session, task, reviewer, since)
        if in_mc is not True:
            on_github = reviewer.strip().lower().lstrip("@") in _github_authors(pr)
            if not on_github:
                result.findings.append(Finding(
                    "reviewer_silent", HARD,
                    f"Genannter Reviewer '{reviewer}' hat weder auf der Karte noch "
                    f"auf PR #{number} selbst kommentiert.",
                ))
    elif (
        target_status == "done"
        and old_status == "review"
        and actor_agent is not None
        and not reviewer_comment_in_flight
    ):
        # review → done by PATCH: the actor IS the reviewer — he must have left
        # his own words on the card (POST /review writes them atomically; the
        # bare PATCH path does not).
        if not await _agent_commented(session, task, actor_agent.id, since):
            result.findings.append(Finding(
                "reviewer_silent", HARD,
                f"{actor_agent.name} schliesst den Review ohne eigenen Kommentar "
                "auf der Karte — Urteil mit `mc approve --feedback \"…\"` begruenden.",
            ))

    return result


# ── Reporting ───────────────────────────────────────────────────────────


def _comment_text(result: ProofGateResult) -> str:
    head = (
        "**Beweis-Tor: Abschluss abgelehnt**" if result.blocks
        else "**Beweis-Tor: Warnung** (Modus `warn`, Statuswechsel zugelassen)"
    )
    lines = [head, ""]
    if result.pr_url:
        lines.append(f"PR: {result.pr_url}")
    for f in result.visible:
        lines.append(f"- `{f.code}` — {f.message}")
    return "\n".join(lines)


def _event_detail(result: ProofGateResult) -> dict:
    return {
        "gefangen_von": "tor",
        "mode": result.mode,
        "blocked": result.blocks,
        "target_status": result.target_status,
        "pr_url": result.pr_url,
        "findings": [f.as_dict() for f in result.findings],
    }


def _stage_comment(session, task_id, result: ProofGateResult) -> None:
    from app.models.task import TaskComment

    session.add(TaskComment(
        task_id=task_id,
        author_type="system",
        comment_type="system",
        content=_comment_text(result),
    ))


async def publish(session, *, task_id, board_id, title: str, result: ProofGateResult,
                  agent_id=None) -> None:
    """Emit the ``task.proof_gate`` event. Commits — call after the transition."""
    if not result.visible:
        return
    try:
        from app.services.activity import emit_event

        codes = ", ".join(f.code for f in result.visible)
        await emit_event(
            session, PROOF_GATE_EVENT,
            f"Beweis-Tor ({'abgelehnt' if result.blocks else result.mode}): "
            f"'{(title or '')[:60]}' — {codes}",
            severity="warning",
            board_id=board_id,
            task_id=task_id,
            agent_id=agent_id,
            detail=_event_detail(result),
        )
    except Exception:  # noqa: BLE001 — the audit trail must not break the PATCH
        logger.warning("proof_gate: Event fehlgeschlagen", exc_info=True)


async def run_proof_gate(
    session,
    task,
    *,
    target_status: str,
    old_status: str,
    actor_agent,
    updates: dict,
    reviewer: str | None = None,
    head_sha: str | None = None,
    head_branch: str | None = None,
    reviewer_comment_in_flight: bool = False,
) -> ProofGateResult:
    """Evaluate, log, and in enforce mode refuse with 422.

    Call BEFORE the status mutation. Non-blocking: a card comment is staged in
    the session (it commits together with the transition) and the caller emits
    the event with ``publish`` after its own commit. Blocking: everything the
    request staged so far is rolled back first (e.g. the review_decision
    fallback), then the comment + event are committed and 422 is raised — the
    refused transition leaves no half state behind.
    """
    try:
        result = await evaluate_proof_gate(
            session, task,
            target_status=target_status, old_status=old_status,
            actor_agent=actor_agent, updates=updates, reviewer=reviewer,
            head_sha=head_sha, head_branch=head_branch,
            reviewer_comment_in_flight=reviewer_comment_in_flight,
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 — a gate bug must not freeze the board
        logger.error("proof_gate: Pruefung fehlgeschlagen fuer Task %s: %s", task.id, exc, exc_info=True)
        result = ProofGateResult(mode=effective_mode(), target_status=target_status)
        result.findings.append(Finding(
            "gate_error", NOTICE, f"Beweis-Tor konnte nicht pruefen ({type(exc).__name__}) — zugelassen.",
        ))

    for f in result.findings:
        log = logger.warning if f.level in (HARD, NOTICE) else logger.info
        log("proof_gate[%s] task=%s %s→%s %s: %s",
            result.mode, task.id, old_status, target_status, f.code, f.message)

    if result.blocks:
        task_id, board_id, title = task.id, task.board_id, task.title
        agent_id = getattr(actor_agent, "id", None)
        await session.rollback()
        _stage_comment(session, task_id, result)
        await session.commit()  # the refusal stays visible on the card
        await publish(session, task_id=task_id, board_id=board_id, title=title,
                      result=result, agent_id=agent_id)
        raise HTTPException(
            status_code=422,
            detail={
                "error": "proof_gate",
                "message": "Beweis-Tor: Abschluss ohne Beleg abgelehnt. "
                           + " ".join(f.message for f in result.hard),
                "findings": [f.as_dict() for f in result.hard],
            },
        )
    if result.visible:
        _stage_comment(session, task.id, result)
    return result
