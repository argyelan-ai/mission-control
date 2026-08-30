"""Auto-onboarding a box by IP+user+password (Fleet & Rezepte v2, Phase 2).

Marks Zielbild: the operator types an address, a username and a password (or
hands over a private key / an existing credential) — MC does everything else
itself: connects, leaves its own key behind, anchors access in the Vault,
optionally bootstraps the box (docker/nvidia) and installs the node-agent.
The operator never runs ssh by hand.

Shape of a run (services/recipe_install.py's job pattern — Redis-backed
live log via services/job_log.JobLog):

  1. SSH connectivity test with whatever credential the operator supplied
     (password OR an existing key). The password lives in a local variable
     for exactly this step and is never referenced again — never logged,
     never persisted (see the security tests in test_host_onboarding.py that
     grep the job log AND the DB for the literal password string).
  2. Generate a fresh Ed25519 keypair (asyncssh.generate_private_key) and
     append its public half to ~/.ssh/authorized_keys — idempotently (a
     re-run replaces this host's own previous line via a marker comment,
     `# mc-fleet <host_slug>`, instead of piling up duplicates or touching
     anything a human put there).
  3. Reconnect using ONLY the new key (no password, no fallback) — proof the
     box actually accepts it before anything gets persisted. If this fails,
     the whole run fails and NOTHING is written to the Vault or the hosts
     table; the box is left exactly as found (the appended key is harmless
     dead weight at that point, not a half-configured credential MC thinks
     it can use).
  4. Persist: encrypt {private_key_pem, public_key, username} into a
     Credential(credential_type='ssh_key') (existing Vault, Fernet — see
     app.services.encryption; NO new crypto), create-or-update the Host row
     (kind='ssh', ssh_credential_id set).
  5. Optional: run the EXISTING host_bootstrap flow (docker/nvidia). Never a
     password prompt there either (sudo -n only) — a `needs_sudo` outcome
     becomes this job's terminal status too, with the same instructions.
  6. Optional: install the node-agent over the SSH session we already have —
     write scripts/mc-node-agent.py (this instance's own copy, same file
     GET /api/v1/nodes/agent-script serves), mint a pairing code internally
     (routers/nodes.py.mint_pairing_code — no HTTP round-trip to ourselves),
     then `--pair CODE --install` via systemd if passwordless sudo works,
     else a plain `nohup ... &` with a log line telling the operator how to
     upgrade to a real service later.

Rate limiting: max 3 failed SSH AUTH attempts per address per 10 minutes
(module-level, mirrors routers/auth.py's login limiter) — this endpoint lets
an admin point MC's own network position at an arbitrary address, and a
tight cap is the difference between "typo'd a password" and "MC as someone's
SSH brute-force tool".
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass

import asyncssh

from app.models.credential import Credential
from app.models.host import Host
from app.services.encryption import encrypt
from app.services.host_resolver import ResolvedHost
from app.services.job_log import JobLog

logger = logging.getLogger("mc.host_onboarding")

LOG_TTL = 3600
STATUS_TTL = 3600
_NAMESPACE = "host:onboard"

STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_NEEDS_SUDO = "needs_sudo"
# The two OnboardingError statuses the frontend distinguishes with their own
# instructions (spec: "Fehlerzustände: auth_failed / unreachable / needs_sudo
# mit klarer Anleitung") — terminal outcomes just like the three above.
STATUS_AUTH_FAILED = "auth_failed"
STATUS_UNREACHABLE = "unreachable"
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED, STATUS_NEEDS_SUDO, STATUS_AUTH_FAILED, STATUS_UNREACHABLE)

_CONNECT_TIMEOUT = 15.0
_CHECK_TIMEOUT = 30.0

AGENT_INSTALL_PATH = "/usr/local/bin/mc-node-agent.py"


def job_for(job_id: str) -> JobLog:
    return JobLog(_NAMESPACE, job_id, log_ttl=LOG_TTL, status_ttl=STATUS_TTL, logger=logger)


async def get_status(job_id: str) -> dict | None:
    return await job_for(job_id).get_status()


async def read_log(job_id: str, cursor: int = 0) -> dict:
    result = await job_for(job_id).read(cursor)
    result.setdefault("job_id", job_id)
    return result


# ── Rate limiting (mirrors routers/auth.py's login limiter) ─────────────────

_RATE_LIMIT_MAX = 3
_RATE_LIMIT_WINDOW_S = 600  # 10 minutes
_auth_failures: dict[str, list[float]] = defaultdict(list)


class RateLimitExceeded(Exception):
    def __init__(self, address: str):
        self.address = address
        super().__init__(f"Zu viele fehlgeschlagene SSH-Logins für '{address}'")


def check_rate_limit(address: str) -> None:
    """Raises RateLimitExceeded if `address` has hit the failure cap. Called
    BEFORE a run even starts — a locked-out address never gets to try again
    silently via a fresh job_id."""
    now = time.time()
    _auth_failures[address] = [t for t in _auth_failures[address] if now - t < _RATE_LIMIT_WINDOW_S]
    if len(_auth_failures[address]) >= _RATE_LIMIT_MAX:
        raise RateLimitExceeded(address)


def _record_auth_failure(address: str) -> None:
    _auth_failures[address].append(time.time())


def _clear_auth_failures(address: str) -> None:
    _auth_failures.pop(address, None)


# ── Pure helpers (no network — unit-tested directly) ─────────────────────────


def authorized_keys_marker(host_slug: str) -> str:
    return f"mc-fleet {host_slug}"


def upsert_authorized_keys(existing_content: str | None, public_key: str, host_slug: str) -> str:
    """Idempotent authorized_keys content: any PRIOR line carrying this
    host's marker comment is dropped first (a re-onboard replaces the old
    key instead of piling up duplicates), then the new key line is appended
    with `mc-fleet <host_slug>` as its trailing comment — a clean, specific
    tag a later removal can grep for without touching a key a human added
    by hand or another host's marker.
    """
    marker = authorized_keys_marker(host_slug)
    lines = [ln for ln in (existing_content or "").splitlines() if ln.strip()]
    kept = [ln for ln in lines if not ln.rstrip().endswith(marker)]
    kept.append(f"{public_key.strip()} {marker}")
    return "\n".join(kept) + "\n"


class OnboardingError(Exception):
    """Carries the terminal status the router/job should end on."""

    def __init__(self, status: str, message: str):
        self.status = status  # "auth_failed" | "unreachable" | "failed"
        self.message = message
        super().__init__(message)


@dataclass
class OnboardParams:
    address: str
    username: str
    password: str | None = None
    private_key_pem: str | None = None
    existing_credential_id: uuid.UUID | None = None
    display_name: str | None = None
    bootstrap: bool = True
    install_agent: bool = True


class _Run:
    def __init__(self, job_id: str, params: OnboardParams):
        self.job_id = job_id
        self.params = params
        self.job = job_for(job_id)
        self.host_id: str | None = None
        self.host_slug: str | None = None

    async def log(self, text: str, level: str = "info") -> None:
        await self.job.append(text, level)

    async def set_status(self, status: str, *, phase: str, message: str | None = None) -> None:
        extra: dict = {"job_id": self.job_id}
        if self.host_id:
            extra["host_id"] = self.host_id
        if self.host_slug:
            extra["host_slug"] = self.host_slug
        await self.job.set_status(status, phase=phase, message=message, extra=extra)

    # ── step 1+3: connect (initial credential, then key-only proof) ────────

    async def _connect_with_supplied_credential(self):
        """Step 1 — connect with whatever the operator gave us. The password
        (if any) lives ONLY in this call's kwargs and this function's local
        scope; nothing here stores or logs it."""
        p = self.params
        connect_kwargs: dict = dict(
            host=p.address, username=p.username, known_hosts=None, connect_timeout=_CONNECT_TIMEOUT,
        )
        if p.existing_credential_id is not None:
            from app.services.runtime_manager import _load_vault_ssh_private_key  # noqa: SLF001

            pem = await _load_vault_ssh_private_key(p.existing_credential_id)
            if not pem:
                raise OnboardingError(
                    "failed", "Das gewählte vorhandene Credential enthält keinen lesbaren privaten Schlüssel."
                )
            connect_kwargs["client_keys"] = [asyncssh.import_private_key(pem)]
        elif p.private_key_pem is not None:
            try:
                connect_kwargs["client_keys"] = [asyncssh.import_private_key(p.private_key_pem)]
            except asyncssh.KeyImportError as e:
                raise OnboardingError("failed", f"Der angegebene private Schlüssel ist unlesbar: {e}")
        else:
            connect_kwargs["password"] = p.password  # local only — see docstring above

        try:
            return await asyncssh.connect(**connect_kwargs)
        except asyncssh.PermissionDenied as e:
            _record_auth_failure(p.address)
            raise OnboardingError("auth_failed", f"SSH-Login fehlgeschlagen (falsches Passwort/Key?): {e}")
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
            raise OnboardingError("unreachable", f"Box nicht erreichbar unter '{p.address}': {e}")

    async def _connect_with_new_key(self, private_key: asyncssh.SSHKey):
        """Step 3 — the gegenprobe: reconnect using ONLY the freshly
        generated key, no password, no other fallback. Proof-before-persist:
        nothing is written to the Vault/hosts table until this succeeds."""
        try:
            return await asyncssh.connect(
                host=self.params.address,
                username=self.params.username,
                client_keys=[private_key],
                known_hosts=None,
                connect_timeout=_CONNECT_TIMEOUT,
            )
        except (asyncssh.Error, OSError, asyncio.TimeoutError) as e:
            raise OnboardingError(
                "failed",
                f"Der neu hinterlegte Schlüssel wurde nicht akzeptiert (Gegenprobe fehlgeschlagen): {e}",
            )

    # ── step 2: authorized_keys ──────────────────────────────────────────────

    async def _append_authorized_key(self, conn, public_key: str, host_slug: str) -> None:
        await conn.run("mkdir -p ~/.ssh && chmod 700 ~/.ssh", check=False, timeout=_CHECK_TIMEOUT)
        existing = await conn.run("cat ~/.ssh/authorized_keys 2>/dev/null || true", check=False, timeout=_CHECK_TIMEOUT)
        new_content = upsert_authorized_keys(existing.stdout or "", public_key, host_slug)
        b64 = base64.b64encode(new_content.encode("utf-8")).decode("ascii")
        write_cmd = (
            f"echo {b64} | base64 -d > ~/.ssh/authorized_keys.mc-tmp "
            f"&& mv ~/.ssh/authorized_keys.mc-tmp ~/.ssh/authorized_keys "
            f"&& chmod 600 ~/.ssh/authorized_keys"
        )
        result = await conn.run(write_cmd, check=False, timeout=_CHECK_TIMEOUT)
        if result.exit_status != 0:
            raise OnboardingError(
                "failed",
                f"authorized_keys konnte nicht geschrieben werden: {(result.stderr or '').strip()}",
            )

    # ── step 6: node-agent install (via _ssh_run — the credential is already
    #    persisted by this point, so this goes through the SAME Vault-backed
    #    resolution path as every other host, not a hand-rolled connection) ──

    async def _install_node_agent(self, resolved: ResolvedHost, host_id: str) -> None:
        from app.services.runtime_manager import _ssh_run  # noqa: SLF001
        from app.database import async_session_maker
        from app.routers.nodes import mint_pairing_code, read_agent_script_or_none
        from app.config import node_agent_base_url

        script = read_agent_script_or_none()
        if script is None:
            await self.log(
                "Monitoring-Agent übersprungen: mc-node-agent.py ist auf dieser Instanz "
                "nicht verfügbar (fehlender docker-compose-Mount).",
                level="warn",
            )
            return

        await self.log("Übertrage mc-node-agent.py auf die Box …")
        b64 = base64.b64encode(script.encode("utf-8")).decode("ascii")
        _, stderr, exit_code = await _ssh_run(
            f"echo {b64} | base64 -d > {AGENT_INSTALL_PATH}.mc-tmp "
            f"&& mv {AGENT_INSTALL_PATH}.mc-tmp {AGENT_INSTALL_PATH} "
            f"&& chmod +x {AGENT_INSTALL_PATH}",
            host=resolved, timeout=_CHECK_TIMEOUT,
        )
        if exit_code != 0:
            await self.log(f"Agent-Skript konnte nicht übertragen werden: {stderr.strip()}", level="warn")
            return

        async with async_session_maker() as session:
            pairing = await mint_pairing_code(session, host_id=uuid.UUID(host_id))

        base_url = node_agent_base_url()
        _, _, sudo_exit = await _ssh_run("sudo -n true", host=resolved, timeout=_CHECK_TIMEOUT)
        if sudo_exit == 0:
            await self.log("Installiere node-agent als systemd-Dienst (sudo -n verfügbar) …")
            stdout, stderr, exit_code = await _ssh_run(
                f"sudo -n python3 {AGENT_INSTALL_PATH} --mc-url {base_url} --pair {pairing.code} --install",
                host=resolved, timeout=_CHECK_TIMEOUT,
            )
            if exit_code == 0:
                await self.log("Monitoring-Agent als systemd-Dienst installiert.")
            else:
                tail = "\n".join(stdout.splitlines()[-6:])
                await self.log(
                    f"Agent-Installation als Dienst schlug fehl (exit {exit_code}): {stderr.strip() or tail}",
                    level="warn",
                )
        else:
            await self.log(
                "Kein passwortloses sudo — der Agent läuft OHNE systemd (nohup), "
                "startet also nicht automatisch nach einem Neustart der Box. Für einen "
                "dauerhaften Dienst später einmalig auf der Box: "
                f"sudo python3 {AGENT_INSTALL_PATH} --mc-url {base_url} --install",
                level="warn",
            )
            _, stderr, exit_code = await _ssh_run(
                f"mkdir -p ~/.cache/mc && nohup python3 {AGENT_INSTALL_PATH} --mc-url {base_url} "
                f"--pair {pairing.code} > ~/.cache/mc/node-agent.log 2>&1 & disown; echo started",
                host=resolved, timeout=_CHECK_TIMEOUT,
            )
            if exit_code == 0:
                await self.log("Monitoring-Agent per nohup gestartet (siehe Hinweis oben für dauerhaften Dienst).")
            else:
                await self.log(f"Agent-Start (nohup) schlug fehl: {stderr.strip()}", level="warn")

    # ── the whole run ────────────────────────────────────────────────────────

    async def run(self) -> None:
        from app.database import async_session_maker
        from sqlmodel import select

        p = self.params
        try:
            await self.set_status(STATUS_RUNNING, phase="connect")
            await self.log(f"Verbinde zu {p.username}@{p.address} …")
            conn = await self._connect_with_supplied_credential()
            _clear_auth_failures(p.address)
            await self.log("Verbindung erfolgreich.")

            async with conn:
                await self.set_status(STATUS_RUNNING, phase="key")
                await self.log("Erzeuge neuen Ed25519-Schlüssel für MC …")
                private_key = asyncssh.generate_private_key("ssh-ed25519")
                public_key = private_key.export_public_key().decode().strip()
                private_key_pem = private_key.export_private_key().decode()

                # slug computed before persistence — authorized_keys' marker
                # needs a name even though no host row exists yet.
                async with async_session_maker() as session:
                    from app.routers.nodes import _unique_slug  # noqa: SLF001

                    existing_host = (
                        await session.exec(select(Host).where(Host.ssh_host == p.address))
                    ).first()
                    self.host_slug = existing_host.slug if existing_host else await _unique_slug(
                        session, p.display_name or p.address
                    )

                await self.log(f"Hinterlege Public Key in ~/.ssh/authorized_keys (Marker: mc-fleet {self.host_slug}) …")
                await self._append_authorized_key(conn, public_key, self.host_slug)

            await self.set_status(STATUS_RUNNING, phase="verify")
            await self.log("Gegenprobe: verbinde erneut NUR mit dem neuen Schlüssel …")
            verify_conn = await self._connect_with_new_key(private_key)
            verify_conn.close()  # sync in asyncssh — NOT a coroutine, awaiting it would raise
            await self.log("Gegenprobe erfolgreich — der Schlüssel funktioniert eigenständig.")

            # ── persist (only now — proof came before anything durable) ────
            await self.set_status(STATUS_RUNNING, phase="persist")
            async with async_session_maker() as session:
                credential = Credential(
                    name=f"SSH — {self.host_slug}",
                    credential_type="ssh_key",
                    encrypted_data=encrypt(json.dumps({
                        "private_key_pem": private_key_pem,
                        "public_key": public_key,
                        "username": p.username,
                    })),
                )
                session.add(credential)
                await session.flush()

                existing_host = (
                    await session.exec(select(Host).where(Host.ssh_host == p.address))
                ).first()
                if existing_host:
                    existing_host.ssh_user = p.username
                    existing_host.ssh_credential_id = credential.id
                    existing_host.kind = "ssh"
                    if p.display_name:
                        existing_host.display_name = p.display_name
                    host = existing_host
                else:
                    host = Host(
                        slug=self.host_slug,
                        display_name=p.display_name or p.address,
                        kind="ssh",
                        ssh_host=p.address,
                        ssh_user=p.username,
                        ssh_credential_id=credential.id,
                    )
                session.add(host)
                await session.commit()
                await session.refresh(host)
            self.host_id = str(host.id)
            await self.log(f"Zugang im Vault gespeichert, Host '{self.host_slug}' angelegt/aktualisiert.")

            resolved = ResolvedHost(
                ssh_host=p.address, ssh_user=p.username, ssh_credential_id=host.ssh_credential_id,
                kind="ssh", slug=self.host_slug, display_name=host.display_name, host_id=host.id,
                source="registry",
            )

            terminal_status = STATUS_DONE
            terminal_message = f"Onboarding von '{self.host_slug}' abgeschlossen."

            if p.bootstrap:
                await self.set_status(STATUS_RUNNING, phase="bootstrap")
                await self.log(
                    f"Starte Box-Vorbereitung (Docker/NVIDIA) — Details: "
                    f"GET /api/v1/hosts/{self.host_id}/bootstrap/log"
                )
                from app.services import host_bootstrap

                await host_bootstrap.run_bootstrap(self.host_id, resolved)
                bootstrap_status = await host_bootstrap.get_status(self.host_id) or {}
                bstatus = bootstrap_status.get("status")
                if bstatus == host_bootstrap.STATUS_NEEDS_SUDO:
                    hint = bootstrap_status.get("message") or "Passwortloses sudo fehlt."
                    await self.log(f"Box-Vorbereitung braucht sudo: {hint}", level="warn")
                    terminal_status = STATUS_NEEDS_SUDO
                    terminal_message = hint
                elif bstatus == host_bootstrap.STATUS_FAILED:
                    msg = bootstrap_status.get("message") or "unbekannter Fehler"
                    await self.log(f"Box-Vorbereitung fehlgeschlagen: {msg}", level="warn")
                else:
                    await self.log("Box-Vorbereitung abgeschlossen.")

            if p.install_agent:
                await self.set_status(STATUS_RUNNING, phase="agent")
                await self._install_node_agent(resolved, self.host_id)

            await self.set_status(terminal_status, phase="done", message=terminal_message)
            await self.log(terminal_message)

        except OnboardingError as e:
            # e.status IS the terminal status verbatim ("auth_failed" /
            # "unreachable" / "failed") — the frontend distinguishes these
            # (spec: klare Anleitung je Fehlerzustand), so it must reach the
            # log unmapped, not collapsed onto a generic STATUS_FAILED.
            await self.log(e.message, level="error")
            await self.set_status(e.status, phase=e.status, message=e.message)
        except Exception as exc:  # noqa: BLE001 — the run reports, it never propagates
            logger.exception("onboarding job %s failed", self.job_id)
            await self.log(str(exc), level="error")
            await self.set_status(STATUS_FAILED, phase="failed", message=str(exc))


async def start_onboarding(params: OnboardParams) -> str:
    """Validates the rate limit, mints a fresh job_id, spawns the run in the
    background. Raises RateLimitExceeded synchronously (before any job
    exists) so the router can answer 429 without ever starting a run."""
    check_rate_limit(params.address)
    job_id = str(uuid.uuid4())
    job = job_for(job_id)
    await job.reset()
    await job.set_status(STATUS_RUNNING, phase="starting", extra={"job_id": job_id})
    asyncio.create_task(_Run(job_id, params).run())
    return job_id
