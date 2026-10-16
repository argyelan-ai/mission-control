"""SSH host keys: trust on first use (TOFU) for every box MC connects to.

Before this module every asyncssh connection ran with ``known_hosts=None`` —
host-key checking off. A different machine answering on a box's address
(swapped device, spoofed IP/ARP on the LAN) would have received MC's
commands, and during password onboarding the password itself.

The rule now, for every SSH connection to a registry host:

  1. No key stored yet (new box, or a row from before migration 0216):
     the key the box presents is accepted, and once the connection is
     established it is stored on the host row (``hosts.ssh_host_key``) and
     logged as an activity event (``host.ssh_key_learned``).
  2. Key stored: the box must present exactly that key. asyncssh is told to
     trust only that key (which also pins the key algorithm, like OpenSSH's
     known_hosts does), so a different key ends the handshake before any
     credential or command is sent.
  3. Different key: the connection is refused with
     :class:`HostKeyMismatchError` (an ``asyncssh.HostKeyNotVerifiable``, so
     every existing ``except asyncssh.Error`` keeps working). The presented
     key is kept as ``hosts.ssh_host_key_rejected`` and an error event
     (``host.ssh_key_mismatch``) is written — once per new key, not on every
     retry of a watcher loop. Nothing overrides this silently.
  4. Re-trust: an admin accepts exactly the rejected key via
     ``POST /api/v1/hosts/{id}/ssh-host-key/trust`` with its fingerprint
     (:func:`trust_rejected_key`). The fingerprint has to match, so what gets
     trusted is the key the operator looked at, not whatever answers next.

A connection whose target carries no ``host_id`` (the ``settings.dgx_ssh_*``
fallback, the legacy ``runtime.host`` field, callers passing
``to_registry_dict()`` like the idle stop or the readiness gate, memory prep)
is pinned to the host row with the same address (``Host.ssh_host``) — the
settings box has such a row from the host seeder. Only an address with no
row at all (the box wizard's ad-hoc probe of a box not yet added) stays
unpinned, with a warning in the log. Several rows on one address: the row
that already holds a key wins, then the oldest (:func:`pick_host_row`).

Which key gets learned: asyncssh's default order prefers RSA; in learn mode
MC asks for ed25519 first, then ECDSA, then RSA (:data:`LEARN_HOST_KEY_ALGS`).
Once pinned, asyncssh negotiates exactly the stored key's algorithm, so a
row that already learned an RSA key keeps working. Messages and events name
the algorithm and the matching ``/etc/ssh/ssh_host_*_key.pub`` file.

The system ``ssh`` binary is used in one place: backend container → the MC
host itself (``host.docker.internal``, host-agent launchctl calls). Those
calls use :func:`system_ssh_host_key_options` — ``accept-new`` against an
MC-owned known_hosts file under ``~/.mc/ssh/`` (the user's ``~/.ssh`` is
mounted read-only), i.e. the same TOFU rule enforced by OpenSSH itself.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import asyncssh
from sqlalchemy import update

from app.utils import utcnow

logger = logging.getLogger("mc.ssh_host_keys")

#: Stable code for the mismatch — UI/clients match on this, not on the sentence.
MISMATCH_CODE = "ssh_host_key_mismatch"

#: Learn mode: host-key algorithms in MC's order of preference. asyncssh's
#: default puts RSA first; ed25519 is the key every current OpenSSH server
#: has and the one an operator usually compares.
LEARN_HOST_KEY_ALGS = [
    "ssh-ed25519",
    "ecdsa-sha2-nistp256",
    "ecdsa-sha2-nistp384",
    "ecdsa-sha2-nistp521",
    "rsa-sha2-512",
    "rsa-sha2-256",
    "ssh-rsa",
]


def host_key_file(algorithm: str) -> str:
    """Where the box keeps the public half of a host key of this type."""
    if algorithm == "ssh-ed25519":
        name = "ed25519"
    elif algorithm.startswith("ecdsa-"):
        name = "ecdsa"
    elif algorithm in ("ssh-rsa", "rsa-sha2-256", "rsa-sha2-512"):
        name = "rsa"
    else:
        name = "*"
    return f"/etc/ssh/ssh_host_{name}_key.pub"


def describe(key: asyncssh.SSHKey) -> str:
    """'ssh-ed25519 SHA256:...' — algorithm plus fingerprint, as ssh-keygen -l shows both."""
    return f"{key.get_algorithm()} {key.get_fingerprint()}"


class HostKeyMismatchError(asyncssh.HostKeyNotVerifiable):
    """The box presented a different host key than the one MC stored.

    Subclass of ``asyncssh.HostKeyNotVerifiable`` (→ ``asyncssh.Error``) so
    every caller that already handles connection errors handles this too —
    only the message is new.
    """

    # `code` is asyncssh's disconnect code (keep it); this is ours.
    error_code = MISMATCH_CODE

    def __init__(self, *, label: str, host_ref: str | None, expected: asyncssh.SSHKey, presented: asyncssh.SSHKey):
        self.label = label
        self.host_ref = host_ref
        self.expected_fingerprint = expected.get_fingerprint()
        self.presented_fingerprint = presented_fp = presented.get_fingerprint()
        self.expected_algorithm = expected.get_algorithm()
        self.presented_algorithm = presented.get_algorithm()
        how = (
            f"POST /api/v1/hosts/{host_ref}/ssh-host-key/trust with "
            f'{{"fingerprint": "{presented_fp}"}}'
            if host_ref
            else "re-trust the box on its host entry"
        )
        message = (
            f"SSH host key of '{label}' has changed — connection refused. "
            f"Expected {describe(expected)}, the box presented {describe(presented)}. "
            "This happens after a reinstall, but it is also what a different "
            "device on that address looks like. Check the fingerprint on the box "
            f"(ssh-keygen -lf {host_key_file(self.presented_algorithm)}); if it matches, {how}."
        )
        super().__init__(message)


def parse_key(key_line: str | None) -> asyncssh.SSHKey | None:
    """OpenSSH public key line → SSHKey; None for empty/unreadable values
    (an unreadable stored key is logged and treated as "not learned")."""
    if not key_line or not key_line.strip():
        return None
    try:
        return asyncssh.import_public_key(key_line.strip())
    except (asyncssh.KeyImportError, ValueError) as exc:
        logger.warning("Stored SSH host key is unreadable (%s) — treated as not learned.", exc)
        return None


def export_key(key: asyncssh.SSHKey) -> str:
    return key.export_public_key().decode().strip()


def fingerprint(key_line: str | None) -> str | None:
    """SHA256 fingerprint of a stored key line (the form ``ssh-keygen -lf`` prints)."""
    key = parse_key(key_line)
    return key.get_fingerprint() if key is not None else None


# ── the pin: what one connection must see ────────────────────────────────────


# rule: R-ssh-host-key-pinned - a box must present the key it presented first
class _PinningClient(asyncssh.SSHClient):
    """Records the key the server presented. asyncssh only asks this client
    when the key is NOT among the trusted keys we passed in — so with a
    stored key this returns False (mismatch), without one True (learn)."""

    def __init__(self, pin: "HostKeyPin"):
        self._pin = pin

    def validate_host_public_key(self, host, addr, port, key) -> bool:  # noqa: D401 — asyncssh hook
        self._pin.presented = key
        return self._pin.expected is None


@dataclass
class HostKeyPin:
    """Host-key expectation for one SSH connection.

    ``host_id`` is the registry row to learn into / record a refusal on
    (None = nothing persisted, e.g. inside an onboarding run before the row
    exists). ``expected`` None = learn mode.
    """

    label: str
    host_id: uuid.UUID | None = None
    host_ref: str | None = None  # slug for the re-trust hint
    expected: asyncssh.SSHKey | None = None
    presented: asyncssh.SSHKey | None = field(default=None)

    def connect_kwargs(self) -> dict:
        """kwargs for asyncssh.connect that switch host-key checking ON.

        ``known_hosts`` must not be None (that disables validation entirely)
        and must not be empty bytes either (asyncssh would then read
        ``~/.ssh/known_hosts`` of the container user) — an explicit
        (trusted, ca, revoked) tuple is exactly "trust these keys, nothing else".
        """
        kwargs: dict = {"client_factory": lambda: _PinningClient(self)}
        if self.expected is not None:
            # Pinned: asyncssh offers only the stored key's algorithm.
            kwargs["known_hosts"] = ([self.expected], [], [])
        else:
            kwargs["known_hosts"] = ([], [], [])
            kwargs["server_host_key_algs"] = LEARN_HOST_KEY_ALGS
        return kwargs

    @property
    def learned(self) -> asyncssh.SSHKey | None:
        """The key to store after a successful connection (learn mode only)."""
        return self.presented if self.expected is None else None

    def mismatch_from(self, exc: BaseException) -> HostKeyMismatchError | None:
        """Turns asyncssh's generic HostKeyNotVerifiable into our clear
        mismatch error — only when we really saw a different key."""
        if not isinstance(exc, asyncssh.HostKeyNotVerifiable) or isinstance(exc, HostKeyMismatchError):
            return None
        if self.expected is None or self.presented is None or self.presented == self.expected:
            return None
        return HostKeyMismatchError(
            label=self.label, host_ref=self.host_ref, expected=self.expected, presented=self.presented,
        )


def pin_for_host_row(host) -> HostKeyPin:
    """Pin from a loaded Host row (no DB access)."""
    return HostKeyPin(
        label=host.slug or host.ssh_host or "?",
        host_id=host.id,
        host_ref=host.slug or (str(host.id) if host.id else None),
        expected=parse_key(host.ssh_host_key),
    )


def pick_host_row(rows):
    """Deterministic choice among host rows sharing one address: a row that
    already holds a key wins (that key must keep being enforced), then the
    oldest row, then the slug."""
    rows = list(rows)
    if not rows:
        return None
    return sorted(
        rows,
        key=lambda h: (h.ssh_host_key is None, h.created_at is None, str(h.created_at or ""), h.slug or ""),
    )[0]


async def host_row_for_address(session, address: str | None):
    """The host row registered for this SSH address (see pick_host_row)."""
    from sqlmodel import select

    from app.models.host import Host

    if not address:
        return None
    rows = (await session.exec(select(Host).where(Host.ssh_host == address))).all()
    return pick_host_row(rows)


async def load_pin(host_id: uuid.UUID | None, *, label: str, address: str | None = None) -> HostKeyPin | None:
    """Pin for a host, read fresh from the DB (short-lived session, like
    runtime_manager._load_vault_ssh_private_key).

    ``host_id`` set: that row. Not set: the row with the same address, so the
    settings fallback and ``to_registry_dict()`` callers are pinned to the
    registered box they really talk to. None only when no row exists — the
    caller then connects unpinned and says so in the log."""
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.database import engine
    from app.models.host import Host

    if host_id is None and not address:
        return None
    async with AsyncSession(engine, expire_on_commit=False) as session:
        host = await session.get(Host, host_id) if host_id is not None else None
        if host is None:
            if host_id is not None:
                logger.warning("SSH to '%s': host row %s is gone — trying the address.", label, host_id)
            host = await host_row_for_address(session, address)
    if host is None:
        return None
    return pin_for_host_row(host)


_unpinned_warned: set[str] = set()


def warn_unpinned(address: str | None, source: str) -> None:
    """Once per address and process: say that this connection is not pinned."""
    key = f"{source}:{address}"
    if key in _unpinned_warned:
        return
    _unpinned_warned.add(key)
    logger.warning(
        "SSH to %s (%s) has no host row — host key NOT pinned. Add the box under "
        "/hosts and bind the runtime to it to get host-key checking.",
        address, source,
    )


# ── persistence: learn / refuse / re-trust ───────────────────────────────────


async def _emit(event_type: str, title: str, severity: str, detail: dict) -> None:
    """Activity event in its own session; never lets a notify problem break SSH."""
    try:
        from sqlmodel.ext.asyncio.session import AsyncSession

        from app.database import engine
        from app.services.activity import emit_event

        async with AsyncSession(engine, expire_on_commit=False) as session:
            await emit_event(session, event_type, title, severity=severity, detail=detail)
    except Exception:  # noqa: BLE001 — the event is a report, the SSH result stands
        logger.exception("could not write activity event %s", event_type)


async def remember_learned_key(pin: HostKeyPin) -> HostKeyMismatchError | None:
    """After a successful connection in learn mode: store the key.

    Conditional write (``WHERE ssh_host_key IS NULL``) — two first
    connections at the same time cannot overwrite each other. If another
    connection stored a DIFFERENT key in between, that is a mismatch and is
    returned (the caller raises it).
    """
    key = pin.learned
    if key is None or pin.host_id is None:
        return None
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.database import engine
    from app.models.host import Host

    key_line = export_key(key)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        result = await session.execute(
            update(Host)
            .where(Host.id == pin.host_id, Host.ssh_host_key.is_(None))
            .values(ssh_host_key=key_line, ssh_host_key_learned_at=utcnow())
        )
        await session.commit()
        if result.rowcount == 0:
            host = await session.get(Host, pin.host_id)
            stored = parse_key(host.ssh_host_key) if host is not None else None
            if stored is not None and stored != key:
                return HostKeyMismatchError(label=pin.label, host_ref=pin.host_ref, expected=stored, presented=key)
            return None
    await emit_learned_event(pin.host_id, pin.label, key)
    return None


async def emit_learned_event(host_id: uuid.UUID, label: str, key: asyncssh.SSHKey) -> None:
    """One info event per learned key: rollout is visible, not silent."""
    logger.info("SSH host key of '%s' learned: %s", label, key.get_fingerprint())
    await _emit(
        "host.ssh_key_learned",
        f"SSH host key of '{label}' stored (first connection): {describe(key)}",
        "info",
        {
            "host_id": str(host_id),
            "fingerprint": key.get_fingerprint(),
            "algorithm": key.get_algorithm(),
            "key_file": host_key_file(key.get_algorithm()),
        },
    )


async def record_mismatch(pin: HostKeyPin, err: HostKeyMismatchError) -> None:
    """Keep the refused key for the re-trust path; one error event per NEW key."""
    if pin.host_id is None or pin.presented is None:
        logger.error("%s", err)
        return
    from sqlmodel.ext.asyncio.session import AsyncSession

    from app.database import engine
    from app.models.host import Host

    key_line = export_key(pin.presented)
    async with AsyncSession(engine, expire_on_commit=False) as session:
        host = await session.get(Host, pin.host_id)
        if host is None:
            return
        already = parse_key(host.ssh_host_key_rejected) == pin.presented
        host.ssh_host_key_rejected = key_line
        host.ssh_host_key_rejected_at = utcnow()
        session.add(host)
        await session.commit()
    logger.error("%s", err)
    if not already:
        await _emit(
            "host.ssh_key_mismatch",
            f"SSH host key of '{pin.label}' changed — connections refused until re-trusted",
            "error",
            {
                "code": MISMATCH_CODE,
                "host_id": str(pin.host_id),
                "expected_fingerprint": err.expected_fingerprint,
                "expected_algorithm": err.expected_algorithm,
                "presented_fingerprint": err.presented_fingerprint,
                "presented_algorithm": err.presented_algorithm,
                "key_file": host_key_file(err.presented_algorithm),
            },
        )


class TrustError(Exception):
    """Re-trust refused (nothing pending, or the fingerprint does not match)."""


async def trust_rejected_key(session, host, fingerprint_to_trust: str) -> str:
    """Operator re-trust: replace the stored key with the last REFUSED key.

    The operator passes the fingerprint they checked on the box; it must
    match the refused key exactly. Returns the new fingerprint.
    """
    pending = parse_key(host.ssh_host_key_rejected)
    if pending is None:
        raise TrustError(
            "No refused host key is waiting for this box. A new key is recorded "
            "the first time the box presents one — connect once (e.g. probe the box), then trust it."
        )
    wanted = (fingerprint_to_trust or "").strip()
    if pending.get_fingerprint() != wanted:
        raise TrustError(
            f"Fingerprint does not match the refused key ({pending.get_fingerprint()}) — nothing changed."
        )
    old_fp = fingerprint(host.ssh_host_key)
    host.ssh_host_key = export_key(pending)
    host.ssh_host_key_learned_at = utcnow()
    host.ssh_host_key_rejected = None
    host.ssh_host_key_rejected_at = None
    session.add(host)
    await session.commit()
    await _emit(
        "host.ssh_key_trusted",
        f"SSH host key of '{host.slug}' re-trusted by an admin: {pending.get_fingerprint()}",
        "warning",
        {"host_id": str(host.id), "fingerprint": pending.get_fingerprint(), "previous_fingerprint": old_fp},
    )
    return pending.get_fingerprint()


# ── one entry point for asyncssh connections ─────────────────────────────────


async def after_connect(pin: HostKeyPin | None) -> None:
    """Call right after the connection is up: stores a learned key. Raises
    HostKeyMismatchError if a concurrent first connection stored a different
    key in between (recorded like any other mismatch)."""
    if pin is None:
        return
    race = await remember_learned_key(pin)
    if race is not None:
        await record_mismatch(pin, race)
        raise race


async def refused(pin: HostKeyPin | None, exc: BaseException) -> HostKeyMismatchError | None:
    """Call from ``except asyncssh.HostKeyNotVerifiable``: returns the clear
    mismatch error to raise (already recorded), or None → re-raise ``exc``."""
    if pin is None:
        return None
    err = pin.mismatch_from(exc)
    if err is not None:
        await record_mismatch(pin, err)
    return err


def connect_kwargs(pin: HostKeyPin | None) -> dict:
    """Host-key kwargs for asyncssh.connect: the pin, or (no row to pin to)
    the old unchecked behaviour."""
    return pin.connect_kwargs() if pin is not None else {"known_hosts": None}


async def connect(pin: HostKeyPin | None, **kwargs):
    """``await asyncssh.connect(...)`` with the pin applied; returns the open
    connection. Raises HostKeyMismatchError on a changed key."""
    try:
        conn = await asyncssh.connect(**kwargs, **connect_kwargs(pin))
    except asyncssh.HostKeyNotVerifiable as exc:
        err = await refused(pin, exc)
        if err is None:
            raise
        raise err from None
    try:
        await after_connect(pin)
    except HostKeyMismatchError:
        conn.close()
        raise
    return conn


# ── system ssh (backend container → MC host) ─────────────────────────────────


def mc_known_hosts_path() -> Path:
    """MC-owned known_hosts file. Lives under ~/.mc (bind-mounted read-write
    at the same absolute path in the container — HOME_HOST); the user's own
    ~/.ssh is mounted read-only and stays untouched."""
    from app.config import settings

    return Path(settings.home_host) / ".mc" / "ssh" / "known_hosts"


def system_ssh_host_key_options() -> list[str]:
    """``-o`` options for the system ssh binary: learn the key on first
    contact, refuse a changed key (OpenSSH ``accept-new``). The directory is
    created on demand; if that fails, ssh still connects for an already
    known key and refuses a changed one — it only cannot learn."""
    path = mc_known_hosts_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("Cannot create %s (%s) — ssh cannot record new host keys.", path.parent, exc)
    return [
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", f"UserKnownHostsFile={path}",
    ]
