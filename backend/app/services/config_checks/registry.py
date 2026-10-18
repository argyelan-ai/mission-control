"""The check registry: one entry per kind of configuration check (ADR-093 §2, §4).

A kind is the answer to "how do I test this sort of thing": a procedure, a
cost hint and a way to list the things of that sort. Adding a new provider,
channel or box type that can be tested is one new :class:`CheckKind` (and,
for an API key, one :class:`SecretTester`) — never a new endpoint or a branch
in shared code. The store (``store.py``) runs any entry the same way and
keeps its latest result in ``config_checks``.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from sqlmodel.ext.asyncio.session import AsyncSession

#: What one run of a check costs. The UI uses it to decide when a check may
#: run on its own: ``free`` on every save, ``rate_limited`` sparingly,
#: ``quota`` only on save and on an explicit click (decision E8a).
COST_HINTS: dict[str, str] = {
    "free": "No cost: a read-only call (status, whoami, model list).",
    "rate_limited": "No money, but it uses up the provider's rate limit.",
    "quota": "A real request that spends a little of the plan's quota.",
}


@dataclass
class Subject:
    """One thing a kind can check — a secret key, a host, a bot.

    ``material`` is what the procedure needs (a token, an address). It never
    leaves the process: the API only shows ``id``/``label``/flags, and the
    store keeps a keyed hash of it to notice a replaced value.
    """

    id: str
    label: str
    configured: bool = True
    material: Any = None
    #: Overrides the kind's cost/testability for this subject (secret keys
    #: differ per provider; a key without a tester is listed, not testable).
    cost: str | None = None
    testable: bool = True
    #: The secret key this subject reads, so the secrets page can find it.
    secret_key: str | None = None
    #: The loaded row (host, runtime) the procedure works on. Not hashed,
    #: not shown — ``material`` alone decides whether the value changed.
    ref: Any = None


@dataclass
class Outcome:
    """What a procedure found. ``message`` is short and shown to the user;
    ``detail`` is the procedure's full answer, which old endpoints return
    unchanged (never stored)."""

    ok: bool
    message: str
    detail: dict = field(default_factory=dict)


ListSubjects = Callable[[AsyncSession], Awaitable[list[Subject]]]
RunCheck = Callable[[AsyncSession, Subject], Awaitable[Outcome]]


@dataclass(frozen=True)
class CheckKind:
    key: str
    label: str
    cost: str
    list_subjects: ListSubjects
    run: RunCheck


REGISTRY: dict[str, CheckKind] = {}


def register(kind: CheckKind) -> CheckKind:
    if kind.cost not in COST_HINTS:
        raise ValueError(f"unknown cost hint {kind.cost!r} for check kind {kind.key!r}")
    if kind.key in REGISTRY:
        raise ValueError(f"check kind {kind.key!r} registered twice")
    REGISTRY[kind.key] = kind
    return kind
