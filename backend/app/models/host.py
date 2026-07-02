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

from sqlalchemy import Boolean, DateTime, Text, text
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
    kind: str = Field(max_length=32)  # ssh | flask_wol | local

    # ssh kind (nullable for flask_wol/local)
    ssh_host: str | None = Field(default=None, max_length=128)  # IP/hostname
    ssh_user: str | None = Field(default=None, max_length=64)
    ssh_key_path: str | None = Field(default=None, max_length=512)  # path inside backend container/host

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
