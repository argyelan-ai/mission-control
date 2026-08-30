"""Host model — generic multi-host registry (ADR-048, Host Registry Welle 1).

Replaces the "new box = new runtime_type + copy-paste control code" pattern
(unsloth_porsche ADR-042, hermes, omp). A Host describes a machine the
control-plane can reach (SSH box, flask_wol sleeper, or the local MC host);
runtimes bind to it via runtimes.host_id. Legacy per-runtime fields
(host / control_url / wol_mac_address / power_managed) stay as fallback —
see host_resolver.resolve_host_for_runtime() for the back-compat chain.

Fresh installs without any GPU box run with 0 hosts: cloud runtimes never
need one.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Text, text
from sqlalchemy import JSON
from sqlmodel import Column, Field, SQLModel


class Host(SQLModel, table=True):
    __tablename__ = "hosts"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=64)  # e.g. dgx-spark, porsche
    display_name: str = Field(max_length=128)

    # How the control-plane talks to this box:
    #   ssh       — always-on box reached via SSH (nvidia-smi, docker, tmux)
    #   flask_wol — sleeping box woken via WoL + driven over its Flask control server
    #   local     — the MC host itself (no remote control channel)
    #   agent     — self-registered via routers/nodes.py; no inbound channel at
    #               all, the box pushes telemetry to us (Fleet & Rezepte v2, Phase 1)
    kind: str = Field(max_length=32)  # ssh | flask_wol | local | agent

    # ssh kind (nullable for flask_wol/local)
    ssh_host: str | None = Field(default=None, max_length=128)  # IP/hostname
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_key_path: str | None = Field(default=None, max_length=512)  # path inside backend container/host

    # Vault-backed SSH key (Fleet & Rezepte v2, Phase 2 — Auto-Onboarding).
    # Points at a Credential(credential_type='ssh_key') holding the Fernet-
    # encrypted {private_key_pem, public_key, username} MC generated for
    # itself during onboarding (services/host_onboarding.py). ssh_key_path
    # stays the fallback for hosts set up before this existed — see
    # runtime_manager._ssh_run's resolution order (credential → path →
    # settings). ON DELETE SET NULL: a deleted credential must not take the
    # host down with it, only its auto-access (same reasoning as
    # host_pairing_codes.host_id, migration 0187).
    ssh_credential_id: uuid.UUID | None = Field(default=None, foreign_key="credentials.id", index=True)

    # Tailscale address for this box, when it has one (100.64.0.0/10 or a
    # *.ts.net MagicDNS name — see services/address_classify). Optional and
    # separate from ssh_host on purpose: SSH from the backend container often
    # tolerates the LAN IP just fine, but a runtime endpoint consumed by a
    # HOST agent (launchd/tmux on the Mac) can silently fail against the same
    # LAN IP when a Tailscale route hijacks it there. When set, endpoint
    # construction (runtime_manager._host_ip) prefers this over ssh_host.
    tailscale_host: str | None = Field(default=None, max_length=128)

    # flask_wol kind (nullable for ssh/local)
    control_url: str | None = Field(default=None, max_length=512)  # e.g. http://192.0.2.1:5555
    wol_mac_address: str | None = Field(default=None, max_length=32)
    power_managed: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
    )

    # GPU profile, quirks, ops notes — free text for humans
    notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    enabled: bool = Field(default=True, sa_column=Column(Boolean, server_default=text("true"), nullable=False))
    ui_order: int = Field(default=0)

    # ── kind=agent (Fleet & Rezepte v2, Phase 1) ─────────────────────────────
    #
    # A self-registered box that pushes telemetry over HTTPS instead of being
    # pulled over SSH — see routers/nodes.py. `agent_token_hash` is a sha256
    # hex digest, never the token itself (the token only ever exists on the
    # device and in the one pairing response). `agent_telemetry` holds only
    # the LAST heartbeat's snapshot (not a history — that would grow forever).
    agent_token_hash: str | None = Field(default=None, max_length=64)
    agent_last_seen_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )
    agent_telemetry: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    agent_version: str | None = Field(default=None, max_length=32)

    # Model-weights inventory (Nachtrag 30.08.2026, für Phase 2 — "schon auf
    # dem Gerät, kein erneuter Download"). Sent only when it changed (agent
    # hashes its own scan result and skips the field otherwise), so this is
    # also "the last snapshot", updated independently of agent_telemetry.
    agent_inventory: list[Any] | None = Field(default=None, sa_column=Column(JSON, nullable=True))
    agent_inventory_updated_at: datetime | None = Field(
        default=None, sa_column=Column(DateTime(timezone=True), nullable=True)
    )

    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=datetime.utcnow,
            nullable=False,
        ),
    )
