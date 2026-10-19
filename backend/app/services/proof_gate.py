"""Proof gate ("Beweis-Tor") — a card only reaches review/done with evidence.

Why: the most expensive pattern in this fleet is the false finish report — an
agent reports "PR open, CI green, reviewer approved" and none of it is true.
Until now a human (the board lead or the operator) found those by hand, days
later. This gate checks the claim on the server, at the moment of the status
change, so ``mc finish --force`` or a raw PATCH cannot walk around it.

What is checked (only for cards that carry a PR). The gate fetches its own
evidence — what the agent sends along is a claim to compare, never the proof:

  a) ``pr_url`` points at a PR that really exists on GitHub, it is OPEN (for
     ``done``: OPEN or MERGED), its branch is the card's ``branch_name`` and
     no unrelated card already holds it — a foreign, merged, green PR does
     not prove anything about this card.
  b) the SHA the agent reported (``head_sha``, sent by the mc CLI on the
     developer's move to ``review``) is the PR head on GitHub. The server
     records the PR head itself on every checked move (``head_sha`` in the
     ``task.proof_gate`` event detail), so (d) and the review→done check do
     not depend on the agent reporting anything.
  c) the required CI checks are green.
  d) after the last "change" feedback (MC ``changes_requested`` or a GitHub
     *Request changes*) the PR really changed: the head moved away from the
     SHA recorded when the card last entered review, and the diff between
     the two is not empty (an empty commit or a faked commit date does not
     count). Cards without such a record fall back to commit timestamps.
  e) review → done is closed by someone other than the developer, who left
     his own verdict on the card (a reflection does not count); a reviewer
     the agent names must exist, must not be the developer and must have
     commented himself — MC card first, GitHub second (never the PR author's
     own login: the fleet pushes with one shared account).
  f) a PR URL quoted in the agent's own comments (the "PR open, CI green"
     text) must exist — also on cards that carry no PR at all.

Modes (``PROOF_GATE_MODE``): ``off`` | ``warn`` | ``enforce``.
``warn`` logs, writes a comment on the card and emits a ``task.proof_gate``
event — the transition still happens. ``enforce`` answers 422 for any *hard*
finding. ``PROOF_GATE_ENFORCE_AFTER`` (ISO date) flips ``warn`` to ``enforce``
automatically once that date is reached AND the gate has checked at least
``PROOF_GATE_ENFORCE_MIN_CHECKS`` transitions — a calendar date alone is no
sample (1–2 finishes a week), so the flip waits for real evidence.

Fail-open, but loud: without a GitHub token, when GitHub cannot be reached
(every ``gh`` call has a time limit) or when the token cannot see the repo,
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

import asyncio
import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException
from sqlmodel import select

logger = logging.getLogger("mc.proof_gate")

PROOF_GATE_EVENT = "task.proof_gate"

#: Allowed values of the ``gefangen_von`` field (who caught a false finish).
CAUGHT_BY_VALUES = ("tor", "rolle", "boss", "mark", "live")

#: Finding levels. ``hard`` blocks in enforce mode, ``notice`` is shown on the
#: card but never blocks, ``info`` is only logged.
HARD, NOTICE, INFO = "hard", "notice", "info"

#: Seconds one ``gh`` call may take inside a request before the gate gives up
#: (fail-open). A hung ``gh`` must not hang every review/done PATCH.
GH_TIMEOUT_S = 10.0

#: A reported SHA shorter than this is no evidence ("a" would match 1/16 of
#: all SHAs by prefix).
MIN_SHA_LEN = 7

#: Same card, same finding codes within this window → no second comment.
DEDUP_WINDOW = timedelta(minutes=30)

#: Comment types that are not a reviewer's verdict: the reflection (``mc
#: finish`` writes one right before its PATCH) and status chatter ("schaue
#: mir das an …") — a verdict is review / feedback / message / handoff / …
_NOT_A_VERDICT = {"reflection", "system", "progress", "subtask_completed", "checkpoint",
                  "blocker"}

#: Commits walked one by one when no diff fingerprint is on record.
MAX_COMMITS_WALKED = 5

#: Branches a PR never runs FROM — a card on these is no PR card.
_NOT_A_PR_BRANCH = {"main", "master", "deploy-main", "develop"}

_PR_CREATED_MARKER = "PR erstellt:"

_GH_PR_FIELDS = (
    "number,state,url,author,createdAt,headRefName,headRefOid,changedFiles,statusCheckRollup,commits,"
    "reviews,comments"
)

_CI_RED = {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "ERROR"}
_CI_OK = {"SUCCESS", "NEUTRAL", "SKIPPED"}

_PR_NOT_FOUND_MARKERS = (
    "could not resolve to a pullrequest",
    "http 404",
    "not found",
)
_REPO_NOT_FOUND_MARKER = "could not resolve to a repository"

_PR_URL_IN_TEXT = re.compile(r"https://github\.com/[\w.-]+/[\w.-]+/pull/\d+", re.IGNORECASE)


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
    #: What the gate saw on GitHub — recorded in the event detail so later
    #: checks compare against the server's own record, not the agent's word.
    head_sha: str | None = None
    head_branch: str | None = None
    pr_state: str | None = None
    #: Fingerprint of the PR's own diff (file + blob per changed file) — a
    #: merge from main, an empty commit or a same-content force-push leave
    #: it unchanged, a real change does not.
    diff_fp: str | None = None

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


class RepoNotVisible(RuntimeError):
    pass


class GithubUnavailable(RuntimeError):
    pass


# ── Mode ────────────────────────────────────────────────────────────────


def _configured_mode() -> str:
    from app.config import settings

    mode = (getattr(settings, "proof_gate_mode", "warn") or "warn").strip().lower()
    if mode not in ("off", "warn", "enforce"):
        logger.warning("PROOF_GATE_MODE=%r unbekannt — benutze 'warn'", mode)
        mode = "warn"
    return mode


def _flip_date() -> date | None:
    from app.config import settings

    raw = (getattr(settings, "proof_gate_enforce_after", "") or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw)
    except ValueError:
        logger.warning("PROOF_GATE_ENFORCE_AFTER=%r ist kein ISO-Datum — ignoriert", raw)
        return None


def effective_mode(today: date | None = None, checks_so_far: int | None = None) -> str:
    """``off`` | ``warn`` | ``enforce`` from settings, incl. the auto-flip.

    The auto-flip needs the date AND ``PROOF_GATE_ENFORCE_MIN_CHECKS`` checked
    transitions (``checks_so_far``; None = not counted → no flip). An explicit
    ``PROOF_GATE_MODE=enforce`` needs neither.
    """
    from app.config import settings

    mode = _configured_mode()
    if mode != "warn":
        return mode
    flip = _flip_date()
    if flip is None or (today or datetime.now(timezone.utc).date()) < flip:
        return mode
    needed = int(getattr(settings, "proof_gate_enforce_min_checks", 15) or 0)
    if needed > 0 and (checks_so_far is None or checks_so_far < needed):
        return mode
    return "enforce"


async def count_checks(session) -> int:
    """Checked transitions so far (every checked move emits one event)."""
    from sqlalchemy import func

    from app.models.activity import ActivityEvent

    try:
        return int((await session.exec(
            select(func.count()).select_from(ActivityEvent)
            .where(ActivityEvent.event_type == PROOF_GATE_EVENT)
        )).one())
    except Exception:  # noqa: BLE001 — counting must never break a PATCH
        logger.warning("proof_gate: Zaehlung fehlgeschlagen", exc_info=True)
        return 0


async def resolve_mode(session, today: date | None = None) -> str:
    """``effective_mode`` with the check count read from the activity log —
    only queried when the flip date has been reached."""
    if _configured_mode() != "warn":
        return _configured_mode()
    flip = _flip_date()
    if flip is None or (today or datetime.now(timezone.utc).date()) < flip:
        return "warn"
    return effective_mode(today, await count_checks(session))


async def caught_by_stats(session, days: int = 30) -> dict:
    """Who caught false finish reports in the last ``days`` days.

    Reads the ``gefangen_von`` field from the activity log — no new table:
    ``task.proof_gate`` events with at least one hard finding count for
    ``tor`` (would have blocked in enforce), ``task.review_rejected`` events
    for the role / lead / operator that sent the card back. Rejections from
    before the field existed count as ``unbekannt``. The ratio tor : rest is
    the measure whether the machine catches what people used to catch.
    """
    from app.models.activity import ActivityEvent

    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, int(days)))
    rows = (await session.exec(
        select(ActivityEvent.event_type, ActivityEvent.detail).where(
            ActivityEvent.event_type.in_((PROOF_GATE_EVENT, "task.review_rejected")),  # type: ignore[union-attr]
            ActivityEvent.created_at >= cutoff.replace(tzinfo=None),
        )
    )).all()
    caught = {key: 0 for key in CAUGHT_BY_VALUES}
    caught["unbekannt"] = 0
    findings: dict[str, int] = {}
    checks = blocked = notices = 0
    for etype, detail in rows:
        detail = detail if isinstance(detail, dict) else {}
        if etype == PROOF_GATE_EVENT:
            checks += 1
            blocked += 1 if detail.get("blocked") else 0
            levels = set()
            for f in detail.get("findings") or []:
                if not isinstance(f, dict):
                    continue
                levels.add(f.get("level"))
                if f.get("level") in (HARD, NOTICE):
                    findings[f.get("code") or "?"] = findings.get(f.get("code") or "?", 0) + 1
            if HARD in levels:
                caught["tor"] += 1
            elif NOTICE in levels:
                notices += 1
            continue
        who = detail.get("gefangen_von")
        caught[who if who in caught else "unbekannt"] += 1
    from app.config import settings

    return {
        "window_days": max(1, int(days)),
        "checks": checks,
        "blocked": blocked,
        "notices": notices,
        "caught_by": caught,
        "findings": dict(sorted(findings.items(), key=lambda kv: -kv[1])),
        "mode": _configured_mode(),
        "enforce_after": (getattr(settings, "proof_gate_enforce_after", "") or None),
        "enforce_min_checks": int(getattr(settings, "proof_gate_enforce_min_checks", 15) or 0),
    }


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


async def _gh(token: str, *args: str) -> tuple[int, str, str]:
    """``gh`` with the resolved token and a hard time limit.

    Timeout → ``GithubUnavailable`` (fail-open, loud): the request goes on.
    """
    import os

    from app.services import pr_merge_monitor

    env = {**os.environ, "GH_TOKEN": token, "GITHUB_TOKEN": token}
    try:
        return await pr_merge_monitor._run_gh("gh", *args, env=env, timeout=GH_TIMEOUT_S)
    except asyncio.TimeoutError as exc:
        raise GithubUnavailable(f"gh antwortet nicht innerhalb {GH_TIMEOUT_S:.0f}s") from exc
    except OSError as exc:  # gh binary missing
        raise GithubUnavailable(f"gh nicht ausfuehrbar ({exc})") from exc


async def _repo_visible(owner: str, repo: str, token: str) -> bool:
    rc, _out, _err = await _gh(token, "api", f"repos/{owner}/{repo}", "--jq", ".full_name")
    return rc == 0


async def fetch_pr_evidence(owner: str, repo: str, number: int, token: str) -> dict:
    """One ``gh pr view`` with everything the gate needs.

    Raises ``PrNotFound`` only when the repo is visible and the PR is not —
    GitHub answers 404 for a private repo the token cannot read, and that is
    a configuration gap, not a false claim (``RepoNotVisible``). Every other
    failure is ``GithubUnavailable`` (an outage).
    """
    rc, out, err = await _gh(
        token, "pr", "view", str(number), "--repo", f"{owner}/{repo}", "--json", _GH_PR_FIELDS,
    )
    if rc != 0:
        low = (err or "").lower()
        if _REPO_NOT_FOUND_MARKER in low:
            raise RepoNotVisible(err.strip()[:300])
        if any(marker in low for marker in _PR_NOT_FOUND_MARKERS):
            if await _repo_visible(owner, repo, token):
                raise PrNotFound(err.strip()[:300])
            raise RepoNotVisible(err.strip()[:300])
        raise GithubUnavailable((err or f"gh exit {rc}").strip()[:300])
    try:
        data = json.loads(out)
    except json.JSONDecodeError as exc:
        raise GithubUnavailable(f"gh pr view: JSON nicht lesbar ({exc})") from exc
    if not isinstance(data, dict):
        raise GithubUnavailable("gh pr view: unerwartete Antwort")
    return data


async def fetch_required_checks(owner: str, repo: str, number: int, token: str) -> list[dict] | None:
    """The PR's *required* checks (``gh pr checks --required``), or None.

    Why not the whole statusCheckRollup: a repo can run non-blocking checks
    that fail by design (measured live on this repo: "kz check (warning
    only)" is red on merged PRs). Green means "the checks the branch rule
    demands are green". None = the repo has no required checks (or the call
    failed) → the caller falls back to the full rollup.
    """
    try:
        rc, out, err = await _gh(
            token, "pr", "checks", str(number), "--repo", f"{owner}/{repo}",
            "--required", "--json", "name,bucket",
        )
    except GithubUnavailable as exc:
        logger.info("proof_gate: gh pr checks %s/%s#%d: %s", owner, repo, number, exc)
        return None
    if rc not in (0, 8):  # 8 = "checks pending" in gh's exit-code contract
        if "no required checks" not in (err or "").lower():
            logger.info("proof_gate: gh pr checks %s/%s#%d failed: %s", owner, repo, number, err)
        return None
    try:
        data = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) and data else None


def diff_fingerprint(lines: list[str]) -> str | None:
    """Stable hash of ``"<file> <blob sha> <status>"`` lines (order-free)."""
    rows = sorted({ln.strip() for ln in lines if ln and ln.strip()})
    if not rows:
        return None
    import hashlib

    return hashlib.sha256("\n".join(rows).encode()).hexdigest()[:20]


async def fetch_diff_fingerprint(owner: str, repo: str, number: int, token: str) -> str | None:
    """Fingerprint of the PR's own diff, None if unknown.

    The PR files API lists what the PR changes against its merge base, with
    the blob each file ends up as. "Update branch" (merging main in) moves
    the merge base, not the PR's own changes; an empty commit changes no
    blob — both leave the fingerprint as it was.
    """
    try:
        rc, out, _err = await _gh(
            token, "api", f"repos/{owner}/{repo}/pulls/{number}/files", "--paginate",
            "--jq", '.[] | "\\(.filename) \\(.sha) \\(.status)"',
        )
    except GithubUnavailable:
        return None
    if rc != 0:
        return None
    return diff_fingerprint((out or "").splitlines())


async def fetch_real_change(owner: str, repo: str, base_sha: str, head_sha: str,
                            token: str) -> bool | None:
    """Did anything real land between two commits? None = unknown.

    Walks the new commits: merge commits (a merge from main brings files
    that are not this card's work) and empty commits do not count. Used
    when no diff fingerprint is on record for the old head.
    """
    try:
        rc, out, _err = await _gh(
            token, "api", f"repos/{owner}/{repo}/compare/{base_sha}...{head_sha}",
            "--jq", "{status: .status, files: (.files | length), "
                    "commits: [.commits[] | {sha: .sha, p: (.parents | length)}]}",
        )
    except GithubUnavailable:
        return None
    if rc != 0:
        return None
    try:
        data = json.loads(out or "{}")
        files = int(data.get("files") or 0)
        commits = [c for c in data.get("commits") or [] if isinstance(c, dict)]
    except (ValueError, TypeError, AttributeError):
        return None
    if files == 0:
        return False
    own = [c.get("sha") for c in commits if int(c.get("p") or 0) <= 1 and c.get("sha")]
    if not own:
        return False  # only merge commits ("Update branch")
    for sha in list(reversed(own))[:MAX_COMMITS_WALKED]:
        try:
            rc, out, _err = await _gh(
                token, "api", f"repos/{owner}/{repo}/commits/{sha}", "--jq", ".files | length",
            )
        except GithubUnavailable:
            return None
        if rc != 0:
            return None
        try:
            if int((out or "0").strip()) > 0:
                return True
        except ValueError:
            return None
    return False if len(own) <= MAX_COMMITS_WALKED else None


async def find_pr_for_branch(owner: str, repo: str, branch: str, token: str) -> str | None:
    """URL of the newest PR running from ``branch`` (any state), or None."""
    try:
        rc, out, _err = await _gh(
            token, "pr", "list", "--repo", f"{owner}/{repo}", "--head", branch,
            "--state", "all", "--json", "number,url", "--limit", "1",
        )
    except GithubUnavailable:
        return None
    if rc != 0:
        return None
    try:
        rows = json.loads(out or "[]")
    except json.JSONDecodeError:
        return None
    if rows and isinstance(rows[0], dict) and rows[0].get("url"):
        return str(rows[0]["url"])
    return None


def classify_required(checks: list[dict]) -> str:
    """``green`` | ``red`` | ``pending`` | ``none`` for ``gh pr checks`` buckets."""
    if not checks:
        return "none"
    buckets = {(c.get("bucket") or "").lower() for c in checks if isinstance(c, dict)}
    if buckets & {"fail", "cancel"}:
        return "red"
    if "pending" in buckets:
        return "pending"
    return "green"


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


def _pr_author(pr: dict) -> str:
    return ((pr.get("author") or {}).get("login") or "").strip().lower()


def _github_authors(pr: dict) -> set[str]:
    """Logins that reviewed or commented — WITHOUT the PR author.

    Verified live 2026-10-19: every fleet PR on this repo is authored by the
    one shared account. If that login counted, ``--reviewer <that account>``
    would pass on every PR — the author's own words are no review.
    """
    author = _pr_author(pr)
    authors = set()
    for item in (pr.get("reviews") or []) + (pr.get("comments") or []):
        if not isinstance(item, dict):
            continue
        login = ((item.get("author") or {}).get("login") or "").strip().lower()
        if login and login != author:
            authors.add(login)
    return authors


def _github_approved_by_other(pr: dict) -> bool:
    author = _pr_author(pr)
    for review in pr.get("reviews") or []:
        if not isinstance(review, dict):
            continue
        login = ((review.get("author") or {}).get("login") or "").strip().lower()
        if (review.get("state") or "").upper() == "APPROVED" and login and login != author:
            return True
    return False


def _same_pr(a: str | None, b: str | None) -> bool:
    from app.services.pr_merge_monitor import parse_pr_url

    ra, rb = parse_pr_url(a), parse_pr_url(b)
    if ra is None or rb is None:
        return False
    return (ra[0].lower(), ra[1].lower(), ra[2]) == (rb[0].lower(), rb[1].lower(), rb[2])


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

    full_name = await _repo_full_name(session, task)
    if not full_name:
        return None, None
    owner, repo_name = full_name.split("/", 1)
    url = f"https://github.com/{owner}/{repo_name}/pull/{int(pr_number)}"
    return url, (owner, repo_name, int(pr_number))


async def _repo_full_name(session, task) -> str | None:
    """``owner/name`` of the card's repo (registry repo first, then project)."""
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
        return None
    return full_name


async def discover_pr(session, task, token: str) -> tuple[str | None, str]:
    """The card's PR when the card itself names none: (url, where found).

    The agent does not have to hand the PR over — the server looks where the
    card's own data points: the source card of a review card, the "PR
    erstellt:" marker the project path writes, the card's branch on GitHub.
    """
    from app.models.task import Task, TaskComment
    from app.services.pr_merge_monitor import parse_pr_url

    source_id = getattr(task, "source_task_id", None)
    if source_id:
        source = await session.get(Task, source_id)
        if source is not None:
            url, ref = await resolve_pr_ref(session, source, {})
            if url and ref:
                return url, "source_task"
    rows = (await session.exec(
        select(TaskComment.content).where(
            TaskComment.task_id == task.id,
            TaskComment.author_type == "system",
            TaskComment.content.like(f"%{_PR_CREATED_MARKER}%"),  # type: ignore[union-attr]
        ).order_by(TaskComment.created_at.desc()).limit(1)
    )).all()
    for content in rows:
        for url in _PR_URL_IN_TEXT.findall(content or ""):
            if parse_pr_url(url):
                return url, "marker"
    branch = (getattr(task, "branch_name", None) or "").strip()
    if branch and branch not in _NOT_A_PR_BRANCH and token:
        full_name = await _repo_full_name(session, task)
        if full_name:
            owner, repo_name = full_name.split("/", 1)
            url = await find_pr_for_branch(owner, repo_name, branch, token)
            if url and parse_pr_url(url):
                return url, "branch"
    return None, ""


async def _card_branches(session, task) -> set[str]:
    """Branch names that belong to this card: the one it carries and the
    ones MC's workspaces push (``task/<slug>``), also of its source card."""
    from app.services.git_service import slugify_project, slugify_workspace_slug

    cards = [task]
    source_id = getattr(task, "source_task_id", None)
    if source_id:
        from app.models.task import Task

        source = await session.get(Task, source_id)
        if source is not None:
            cards.append(source)
    names: set[str] = set()
    for card in cards:
        if getattr(card, "branch_name", None):
            names.add(card.branch_name.strip())
        title = getattr(card, "title", None) or ""
        if title:
            names.add(f"task/{slugify_workspace_slug(title)}")
            names.add(f"task/{slugify_project(title)}")
    return names


def code_card_level(task, target_status: str) -> str | None:
    """How loud "no PR" is for this card: HARD | NOTICE | INFO | None.

    HARD:   a code_change card on a known repo — it produces a PR by design.
    NOTICE: other code hints (code_change, a work branch).
    INFO:   only a repo on the card (investigations run on repos too).
    None:   no code hint at all — research, ops, briefings.
    Project cards moving to review get their PR created right after the
    gate, in the same PATCH — not a finding there.
    """
    if target_status == "review" and getattr(task, "project_id", None):
        return None
    branch = (getattr(task, "branch_name", None) or "").strip()
    work_branch = bool(branch) and branch not in _NOT_A_PR_BRANCH
    code = getattr(task, "delegation_type", None) == "code_change"
    has_repo = bool(getattr(task, "repo_id", None) or getattr(task, "project_id", None))
    if code and has_repo and (work_branch or not branch):
        return HARD
    if code or work_branch:
        return NOTICE
    if has_repo:
        return INFO
    return None


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


async def _last_status_change_at(session, task) -> datetime | None:
    from app.models.task import TaskEvent

    row = (await session.exec(
        select(TaskEvent.created_at)
        .where(TaskEvent.task_id == task.id)
        .order_by(TaskEvent.created_at.desc())
        .limit(1)
    )).first()
    return _parse_ts(row) if row else None


async def _agent_commented(session, task, agent_id: uuid.UUID, since: datetime | None) -> bool:
    """Did ``agent_id`` leave a verdict on the card since ``since``?

    A reflection is not a verdict: ``mc finish`` writes one right before its
    PATCH, so counting it would make the check pass by construction.
    """
    from app.models.task import TaskComment

    task_ids = [task.id]
    reviewed = getattr(task, "source_task_id", None)
    if reviewed:
        task_ids.append(reviewed)
    rows = (await session.exec(
        select(TaskComment.created_at, TaskComment.comment_type).where(
            TaskComment.task_id.in_(task_ids),  # type: ignore[union-attr]
            TaskComment.author_agent_id == agent_id,
        )
    )).all()
    for created, ctype in rows:
        if (ctype or "") in _NOT_A_VERDICT:
            continue
        ts = _parse_ts(created)
        if since is None or ts is None or ts >= since:
            return True
    return False


async def _board_agent_by_name(session, task, name: str):
    from sqlalchemy import func

    from app.models.agent import Agent

    return (await session.exec(
        select(Agent).where(
            Agent.board_id == task.board_id,
            func.lower(Agent.name) == name.strip().lower().lstrip("@"),
        )
    )).first()


async def _developer(session, task):
    """The agent who built the card (server-side, never the agent's word)."""
    try:
        from app.services.work_context import find_last_developer

        target = task
        if getattr(task, "source_task_id", None):
            from app.models.task import Task

            target = await session.get(Task, task.source_task_id) or task
        return await find_last_developer(session, target)
    except Exception:  # noqa: BLE001 — a lookup failure must not break the PATCH
        logger.info("proof_gate: Entwickler nicht ermittelbar fuer %s", task.id, exc_info=True)
        return None


async def _independent_review_exists(session, task, developer_id, pr: dict) -> bool:
    """Any evidence that someone other than the developer reviewed the card."""
    from app.models.task import Task

    if getattr(task, "review_decision", None) == "approved":
        return True
    review_cards = (await session.exec(
        select(Task).where(Task.source_task_id == task.id)
    )).all()
    for card in review_cards:
        if card.review_decision == "approved" or (
            card.status == "done" and card.assigned_agent_id != developer_id
        ):
            return True
    return _github_approved_by_other(pr)


async def _pr_on_other_card(session, task, pr_url: str):
    """An UNRELATED card that already holds this PR, or None.

    Related cards share a PR by design: the review card of this card
    (``source_task_id``), its parent, its subtasks and siblings.
    """
    from app.models.task import Task

    m = re.search(r"/pull/(\d+)", pr_url or "")
    if not m:
        return None
    rows = (await session.exec(
        select(Task).where(Task.id != task.id, Task.pr_url.like(f"%/pull/{m.group(1)}%"))  # type: ignore[union-attr]
    )).all()
    related = {x for x in (task.id, getattr(task, "source_task_id", None),
                           getattr(task, "parent_task_id", None)) if x}
    for other in rows:
        if not _same_pr(other.pr_url, pr_url):
            continue
        if (other.id in related or other.source_task_id in related
                or other.parent_task_id in related):
            continue
        return other
    return None


async def _review_snapshots(session, task) -> list[tuple[datetime | None, dict]]:
    """(created_at, detail) of this card's recorded review entries, newest first.

    Only moves that went through (not refused) and carry the server's own
    record of the PR head.
    """
    from app.models.activity import ActivityEvent

    rows = (await session.exec(
        select(ActivityEvent.created_at, ActivityEvent.detail)
        .where(ActivityEvent.task_id == task.id, ActivityEvent.event_type == PROOF_GATE_EVENT)
        .order_by(ActivityEvent.created_at.desc())
        .limit(50)
    )).all()
    out = []
    for created, detail in rows:
        if not isinstance(detail, dict):
            continue
        if detail.get("target_status") != "review" or detail.get("blocked"):
            continue
        if not detail.get("head_sha"):
            continue
        out.append((_parse_ts(created), detail))
    return out


async def _claimed_pr_urls(session, task, actor_agent, since: datetime | None) -> list[str]:
    """PR URLs the acting agent quoted in his own comments in this work phase."""
    from app.models.task import TaskComment

    if actor_agent is None:
        return []
    rows = (await session.exec(
        select(TaskComment.content, TaskComment.created_at).where(
            TaskComment.task_id == task.id,
            TaskComment.author_agent_id == actor_agent.id,
        )
    )).all()
    urls: list[str] = []
    for content, created in rows:
        ts = _parse_ts(created)
        if since is not None and ts is not None and ts < since:
            continue
        for url in _PR_URL_IN_TEXT.findall(content or ""):
            if not any(_same_pr(url, seen) for seen in urls):
                urls.append(url)
    return urls[:3]


async def _real_change_since(owner: str, repo: str, old_detail: dict, new_sha: str,
                             new_fp: str | None, token: str) -> bool | None:
    """Real change between a recorded snapshot and the current head?

    The PR's diff fingerprint first (both recorded: identical = nothing
    real happened, whatever the commits say), else walk the new commits
    skipping merge and empty ones. None = could not tell (fail-open).
    """
    old_sha = str(old_detail.get("head_sha") or "").lower()
    if old_sha and old_sha == new_sha:
        return False
    old_fp = old_detail.get("diff_fp")
    if old_fp and new_fp:
        return old_fp != new_fp
    if not old_sha:
        return None
    return await fetch_real_change(owner, repo, old_sha, new_sha, token)


# ── The verdict ─────────────────────────────────────────────────────────


async def _check_claims(session, task, result: ProofGateResult, actor_agent, token: str,
                        card_pr_url: str | None) -> None:
    """(f) PR URLs quoted in the agent's own text must exist."""
    from app.services.pr_merge_monitor import parse_pr_url

    since = await _last_status_change_at(session, task)
    for url in await _claimed_pr_urls(session, task, actor_agent, since):
        if card_pr_url and _same_pr(url, card_pr_url):
            continue
        ref = parse_pr_url(url)
        if ref is None:
            continue
        owner, repo, number = ref
        try:
            await fetch_pr_evidence(owner, repo, number, token)
        except PrNotFound:
            result.findings.append(Finding(
                "claimed_pr_not_found", HARD,
                f"Im eigenen Kommentar genannter PR {owner}/{repo}#{number} "
                "existiert auf GitHub nicht.",
            ))
        except (RepoNotVisible, GithubUnavailable):
            continue


# rule: R-proof-gate-finish - every check below is one false-finish pattern
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
    if target_status not in ("review", "done") or target_status == old_status:
        return ProofGateResult(mode=_configured_mode(), target_status=target_status)
    mode = await resolve_mode(session)
    result = ProofGateResult(mode=mode, target_status=target_status)
    if mode == "off":
        return result

    pr_url, ref = await resolve_pr_ref(session, task, updates)
    token = ""
    if pr_url is None:
        # The card names no PR. The server looks for it itself — what the
        # agent leaves away is no reason to skip the check.
        token = await _github_token(session)
        found, where = await discover_pr(session, task, token)
        if found:
            from app.services.pr_merge_monitor import parse_pr_url

            pr_url, ref = found, parse_pr_url(found)
            result.findings.append(Finding(
                "pr_not_on_card", NOTICE,
                f"Karte nennt keinen PR — das Tor hat {found} selbst gefunden "
                f"({ {'branch': 'Branch der Karte', 'marker': 'PR-erstellt-Notiz', 'source_task': 'Ursprungskarte'}.get(where, where) }) "
                "und prueft ihn. `pr_url` auf der Karte setzen.",
            ))
    result.pr_url = pr_url
    if pr_url is None:
        # b) a code card without any PR is itself a finding
        level = code_card_level(task, target_status)
        if level is not None:
            result.findings.append(Finding(
                "pr_missing", level,
                f"Code-Karte ohne PR: weder auf der Karte, noch als PR-Notiz, noch auf "
                f"Branch '{getattr(task, 'branch_name', None) or '-'}' auf GitHub gefunden — "
                f"'{target_status}' ohne PR belegt keine Code-Aenderung.",
            ))
        # (f) the agent's text may still claim one
        if token and actor_agent is not None:
            await _check_claims(session, task, result, actor_agent, token, None)
        result.checked = bool(result.findings)
        return result
    result.checked = True

    if ref is None:
        result.findings.append(Finding(
            "pr_url_invalid", HARD,
            f"pr_url '{pr_url}' ist keine GitHub-PR-Adresse "
            "(erwartet: https://github.com/<owner>/<repo>/pull/<nr>).",
        ))
        return result

    token = token or await _github_token(session)
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
    except RepoNotVisible:
        result.findings.append(Finding(
            "repo_not_visible", NOTICE,
            f"Repo {owner}/{repo} ist mit dem GitHub-Token nicht sichtbar (privat "
            "ohne Zugriff, oder Repo-Name falsch) — PR ungeprueft zugelassen. "
            "Token-Rechte bzw. Repo auf der Karte pruefen.",
        ))
        return result
    except GithubUnavailable as exc:
        result.findings.append(Finding(
            "github_unavailable", NOTICE,
            f"GitHub nicht erreichbar — PR {owner}/{repo}#{number} ungeprueft "
            f"zugelassen ({exc}).",
        ))
        return result

    remote_sha = (pr.get("headRefOid") or "").strip().lower()
    remote_branch = (pr.get("headRefName") or "").strip()
    state = (pr.get("state") or "").strip().upper()
    result.head_sha, result.head_branch, result.pr_state = remote_sha or None, remote_branch or None, state or None

    # (a) the PR belongs to THIS card and is still in play
    allowed = ("OPEN",) if target_status == "review" else ("OPEN", "MERGED")
    if state and state not in allowed:
        result.findings.append(Finding(
            "pr_not_open", HARD,
            f"PR #{number} ist {state} — '{target_status}' braucht einen "
            f"{' oder '.join(allowed)}-PR. Ein geschlossener oder fremder PR belegt nichts.",
        ))
    card_branch = (getattr(task, "branch_name", None) or "").strip()
    if card_branch and remote_branch and remote_branch not in await _card_branches(session, task):
        # Agents pick their own branch names and MC's workspace pushes
        # `task/<slug>` — a different name alone is no foreign PR (dry run
        # 2026-10-19: 6 of 8 real code cards). A PR opened BEFORE the card
        # existed cannot be this card's work (createdAt is GitHub's clock,
        # not the agent's).
        pr_created = _parse_ts(pr.get("createdAt"))
        card_created = _parse_ts(getattr(task, "created_at", None))
        foreign = pr_created is None or card_created is None or pr_created < card_created
        result.findings.append(Finding(
            "pr_branch_mismatch", HARD if foreign else NOTICE,
            f"PR #{number} laeuft auf Branch '{remote_branch}', die Karte auf "
            f"'{card_branch}'"
            + (" — und der PR ist aelter als die Karte: nicht der PR dieser Karte."
               if foreign else " — Branch auf der Karte nachtragen."),
        ))
    other = await _pr_on_other_card(session, task, pr_url)
    if other is not None:
        result.findings.append(Finding(
            "pr_on_other_card", HARD,
            f"PR #{number} gehoert schon zur Karte '{(other.title or '')[:60]}' "
            f"({str(other.id)[:8]}) — ein PR belegt nur die eigene Karte.",
        ))

    # b) a PR that changes nothing proves no work (an empty branch pushed
    # only to get a PR number) — and the server's own record of what the PR
    # changes, for (d) and the review→done comparison
    changed_files = pr.get("changedFiles")
    if isinstance(changed_files, int) and changed_files == 0 and state != "MERGED":
        result.findings.append(Finding(
            "pr_empty", HARD,
            f"PR #{number} aendert keine einzige Datei — ein leerer PR belegt keine Arbeit.",
        ))
    result.diff_fp = await fetch_diff_fingerprint(owner, repo, number, token)

    # (b) the agent's reported SHA is the pushed head — only the developer's
    # move to review: at review→done the reviewer's local checkout may lag
    # behind a later "Update branch" without anything being wrong.
    reported = (head_sha or "").strip().lower()
    if target_status == "review" and reported:
        if len(reported) < MIN_SHA_LEN:
            result.findings.append(Finding(
                "sha_too_short", INFO,
                f"Gemeldeter Stand '{reported}' ist kuerzer als {MIN_SHA_LEN} Zeichen — ignoriert.",
            ))
        elif head_branch and remote_branch and head_branch.strip() != remote_branch:
            result.findings.append(Finding(
                "sha_other_branch", INFO,
                f"Gemeldeter Stand stammt von Branch '{head_branch}', der PR laeuft auf "
                f"'{remote_branch}' — SHA-Vergleich uebersprungen.",
            ))
        elif remote_sha and not remote_sha.startswith(reported):
            result.findings.append(Finding(
                "sha_mismatch", HARD,
                f"Gemeldeter Stand {reported[:12]} ist nicht der Stand auf GitHub "
                f"({remote_sha[:12]}, Branch '{remote_branch}'). Erst pushen, dann melden.",
            ))

    # (c) CI green — the required checks when the repo has any, else all
    required = await fetch_required_checks(owner, repo, number, token)
    ci = classify_required(required) if required is not None else classify_ci(pr.get("statusCheckRollup"))
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

    snapshots = await _review_snapshots(session, task)

    # (d) the PR really changed after the last change request
    change_times = list(_github_change_requests(pr))
    if getattr(task, "review_decision", None) == "changes_requested":
        decided = _parse_ts(getattr(task, "review_decided_at", None))
        if decided:
            change_times.append(decided)
    if change_times:
        last_change = max(change_times)
        before = next((d for ts, d in snapshots if ts is not None and ts <= last_change), None)
        if before is not None and remote_sha:
            old_sha = str(before["head_sha"]).lower()
            real = await _real_change_since(owner, repo, before, remote_sha, result.diff_fp, token)
            if old_sha == remote_sha:
                result.findings.append(Finding(
                    "no_commit_after_change", HARD,
                    f"Seit dem Change-Feedback ({last_change:%Y-%m-%d %H:%M} UTC) steht "
                    f"PR #{number} unveraendert auf {remote_sha[:12]} — erst den Change umsetzen.",
                ))
            elif real is False:
                result.findings.append(Finding(
                    "no_commit_after_change", HARD,
                    f"Seit dem Change-Feedback kam auf PR #{number} keine echte Aenderung "
                    f"({old_sha[:12]} → {remote_sha[:12]}): nur leere Commits, ein Merge von "
                    "main oder derselbe Inhalt neu gepusht.",
                ))
        else:
            # no server record from before the change (older card) → timestamps
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

    # (e) an independent reviewer — determined on the server
    since = await _last_review_entry_at(session, task)
    developer = await _developer(session, task)
    developer_id = getattr(developer, "id", None)
    if developer_id is None and old_status != "review":
        # the log names nobody — the card's own assignee built it
        developer_id = getattr(task, "assigned_agent_id", None)
    if reviewer:
        named = await _board_agent_by_name(session, task, reviewer)
        if named is not None and developer_id and named.id == developer_id:
            result.findings.append(Finding(
                "reviewer_is_developer", HARD,
                f"Genannter Reviewer '{reviewer}' hat die Karte selbst gebaut — "
                "kein unabhaengiger Review.",
            ))
        else:
            in_mc = await _agent_commented(session, task, named.id, since) if named else False
            on_github = reviewer.strip().lower().lstrip("@") in _github_authors(pr)
            if not in_mc and not on_github:
                result.findings.append(Finding(
                    "reviewer_silent", HARD,
                    f"Genannter Reviewer '{reviewer}' hat weder auf der Karte noch "
                    f"auf PR #{number} selbst kommentiert (der PR-Autor zaehlt nicht).",
                ))
    if target_status == "done" and actor_agent is not None:
        if developer_id and actor_agent.id == developer_id and old_status == "review":
            result.findings.append(Finding(
                "self_review", HARD,
                f"{actor_agent.name} hat die Karte gebaut und schliesst den eigenen "
                "Review — das muss jemand anderes tun.",
            ))
        elif old_status == "review" and not reviewer_comment_in_flight:
            # review → done by PATCH: the actor IS the reviewer — he must have
            # left his own verdict on the card (POST /review writes it
            # atomically; the bare PATCH path does not).
            if not await _agent_commented(session, task, actor_agent.id, since):
                result.findings.append(Finding(
                    "reviewer_silent", HARD,
                    f"{actor_agent.name} schliesst den Review ohne eigenes Urteil "
                    "auf der Karte (eine Reflexion zaehlt nicht) — "
                    "`mc approve --feedback \"…\"` benutzen.",
                ))
        elif old_status != "review" and not getattr(task, "skip_review", False):
            # The reviewer is determined here, not named by the agent: an
            # approve in MC, a done review card of someone else, or an
            # approve on GitHub by another login. The developer closing his
            # own PR card without any of these skipped the review.
            if not await _independent_review_exists(session, task, developer_id, pr):
                own = bool(developer_id) and actor_agent.id == developer_id
                result.findings.append(Finding(
                    "no_independent_review", HARD if own else NOTICE,
                    f"'{getattr(task, 'title', '')[:60]}' geht ohne Review direkt auf done — "
                    "kein Approve in MC, keine Review-Karte, kein Approve eines anderen auf GitHub."
                    + (" Der Entwickler schliesst seine eigene PR-Karte." if own else ""),
                ))

    # review → done: did the PR really change after it entered review? A
    # merge from main or an empty commit does not count; real changes the
    # review may not have seen are shown (the server cannot know which
    # stand the reviewer read, so this stays a notice).
    if target_status == "done" and snapshots and remote_sha:
        seen = str(snapshots[0][1]["head_sha"]).lower()
        if seen != remote_sha:
            real = await _real_change_since(owner, repo, snapshots[0][1], remote_sha,
                                            result.diff_fp, token)
            if real is False:
                result.findings.append(Finding(
                    "head_moved_since_review", INFO,
                    f"PR #{number} bewegte sich seit dem Review nur durch Merge/leere Commits.",
                ))
            else:
                result.findings.append(Finding(
                    "head_moved_since_review", NOTICE,
                    f"PR #{number} steht jetzt auf {remote_sha[:12]}, beim Eintritt in Review "
                    f"stand er auf {seen[:12]} — der Review galt evtl. einem aelteren Stand.",
                ))

    # (f) other PRs quoted in the agent's own text
    await _check_claims(session, task, result, actor_agent, token, pr_url)

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
        "pr_state": result.pr_state,
        "head_sha": result.head_sha,
        "head_branch": result.head_branch,
        "diff_fp": result.diff_fp,
        "findings": [f.as_dict() for f in result.findings],
    }


async def _recent_duplicate(session, task_id, content: str) -> bool:
    """The same gate comment already sits on the card (last 30 min)."""
    from app.models.task import TaskComment

    try:
        rows = (await session.exec(
            select(TaskComment.created_at).where(
                TaskComment.task_id == task_id,
                TaskComment.author_type == "system",
                TaskComment.content == content,
            )
        )).all()
    except Exception:  # noqa: BLE001
        return False
    cutoff = datetime.now(timezone.utc) - DEDUP_WINDOW
    return any((_parse_ts(ts) or cutoff) > cutoff for ts in rows)


async def _stage_comment(session, task_id, result: ProofGateResult) -> None:
    from app.models.task import TaskComment

    content = _comment_text(result)
    if await _recent_duplicate(session, task_id, content):
        return  # a looping agent must not flood the card
    session.add(TaskComment(
        task_id=task_id,
        author_type="system",
        comment_type="system",
        content=content,
    ))


async def publish(session, *, task_id, board_id, title: str, result: ProofGateResult,
                  agent_id=None) -> None:
    """Emit the ``task.proof_gate`` event. Commits — call after the transition.

    Every checked move gets one: a clean one as ``info`` (it records the PR
    head the server saw — the base for check (d) and the rollout count), one
    with findings as ``warning``.
    """
    if not (result.checked or result.visible):
        return
    try:
        from app.services.activity import emit_event

        if result.visible:
            codes = ", ".join(f.code for f in result.visible)
            title_text = (f"Beweis-Tor ({'abgelehnt' if result.blocks else result.mode}): "
                          f"'{(title or '')[:60]}' — {codes}")
            severity = "warning"
        else:
            title_text = f"Beweis-Tor: '{(title or '')[:60]}' geprueft, sauber"
            severity = "info"
        await emit_event(
            session, PROOF_GATE_EVENT, title_text,
            severity=severity,
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
    verdict_text: str | None = None,
) -> ProofGateResult:
    """Evaluate, log, and in enforce mode refuse with 422.

    Call BEFORE the status mutation. Non-blocking: a card comment is staged in
    the session (it commits together with the transition) and the caller emits
    the event with ``publish`` after its own commit. Blocking: everything the
    request staged so far is rolled back first (e.g. the review_decision
    fallback), then the comment + event are committed and 422 is raised — the
    refused transition leaves no half state behind. ``verdict_text`` (POST
    /review approve): the reviewer's words survive the rollback as his own
    comment, so a re-approve once CI is green does not need them again.
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
        result = ProofGateResult(mode=_configured_mode(), target_status=target_status)
        result.findings.append(Finding(
            "gate_error", NOTICE, f"Beweis-Tor konnte nicht pruefen ({type(exc).__name__}) — zugelassen.",
        ))

    for f in result.findings:
        log = logger.warning if f.level in (HARD, NOTICE) else logger.info
        log("proof_gate[%s] task=%s %s→%s %s: %s",
            result.mode, task.id, old_status, target_status, f.code, f.message)

    if result.blocks:
        from app.models.task import TaskComment

        task_id, board_id, title = task.id, task.board_id, task.title
        agent_id = getattr(actor_agent, "id", None)
        await session.rollback()
        if verdict_text and agent_id:
            session.add(TaskComment(
                task_id=task_id, author_type="agent", author_agent_id=agent_id,
                comment_type="review",
                content=("Review-Urteil (approve) — vom Beweis-Tor angehalten, "
                         "erneut approven sobald behoben:\n\n" + verdict_text),
            ))
        await _stage_comment(session, task_id, result)
        await session.commit()  # the refusal stays visible on the card
        await publish(session, task_id=task_id, board_id=board_id, title=title,
                      result=result, agent_id=agent_id)
        hint = (" Dein Urteil steht als Kommentar auf der Karte — `mc approve` erneut, "
                "sobald behoben." if verdict_text else "")
        raise HTTPException(
            status_code=422,
            detail={
                "error": "proof_gate",
                "message": "Beweis-Tor: Abschluss ohne Beleg abgelehnt. "
                           + " ".join(f.message for f in result.hard) + hint,
                "findings": [f.as_dict() for f in result.hard],
            },
        )
    if result.visible:
        await _stage_comment(session, task.id, result)
    return result
