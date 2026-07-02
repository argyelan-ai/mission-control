"""Runtime model — DB-backed runtime registry.

Replaces the static backend/config/runtimes.json. JSON stays as a seed source
for first deployment (see runtime_seeder.py). Authoritative state lives in this
table so the UI can CRUD runtimes without a code deploy.
"""
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Text, text
from sqlmodel import Column, Field, SQLModel


class Runtime(SQLModel, table=True):
    __tablename__ = "runtimes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=64)
    display_name: str = Field(max_length=128)
    runtime_type: str = Field(max_length=32)  # lmstudio | vllm_docker | unsloth | openai_compatible | cloud
    endpoint: str = Field(max_length=512)
    healthcheck_path: str | None = Field(default=None, max_length=128)

    # Model identifier sent as OPENAI_MODEL to agents
    model_identifier: str | None = Field(default=None, max_length=256)

    # Type-specific management hooks
    container_name: str | None = Field(default=None, max_length=128)  # vllm_docker
    lms_identifier: str | None = Field(default=None, max_length=256)  # lmstudio
    lms_cli_path: str | None = Field(default=None, max_length=256)  # lmstudio

    # Recipe-aware re-launch (vllm_docker). When `docker start <container_name>`
    # finds no container (e.g. sparkrun `--rm` auto-removed it after stop),
    # start_runtime() executes this command via SSH instead. The command is
    # responsible for labelling the resulting container so stop/restart can
    # find it again. Nullable — agents without a recipe keep the
    # docker-start-only path.
    launch_command: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # ── Host binding (ADR-048, Host Registry) ─────────────────────────────────
    # FK into the generic hosts table — the authoritative source for WHERE this
    # runtime runs (SSH box, flask_wol sleeper, local). Resolution order lives in
    # host_resolver.resolve_host_for_runtime(): host_id → legacy `host` string →
    # settings.dgx_ssh_host → None. Nullable: cloud/HTTP-only runtimes need no host.
    host_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            "host_id",
            ForeignKey("hosts.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )

    # DEPRECATED (ADR-048): SSH host override — superseded by host_id → hosts.ssh_host.
    # Kept as legacy fallback in the resolver chain, do not use for new runtimes.
    host: str | None = Field(default=None, max_length=128)

    # ── Power-managed runtime (unsloth_porsche) ──────────────────────────────
    # For a runtime on a host that is NOT always on (the PORSCHE Windows box with
    # a local unsloth OpenAI server). Unlike the DGX (SSH/tmux, always up), this
    # host sleeps and is controlled via its Flask `:5555` server + Wake-on-LAN.
    # All three are nullable / default-off so every existing runtime is unaffected.
    # DEPRECATED (ADR-048): all three superseded by host_id → hosts (kind flask_wol).
    # Kept as legacy fallback (no breaking change), do not use for new runtimes.
    control_url: str | None = Field(default=None, max_length=512)  # Flask control plane, e.g. http://192.0.2.20:5555
    wol_mac_address: str | None = Field(default=None, max_length=32)  # target MAC for the WoL magic packet
    power_managed: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
    )

    role_tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))

    supports_tools: bool = Field(default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False))
    supports_reasoning: bool = Field(default=False, sa_column=Column(Boolean, server_default=text("false"), nullable=False))
    supports_streaming: bool = Field(default=True, sa_column=Column(Boolean, server_default=text("true"), nullable=False))

    preferred_context_len: int | None = None
    max_context_len: int | None = None

    gpu_profile: str | None = Field(default=None, max_length=64)
    memory_notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    startup_notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    ui_order: int = Field(default=999)
    enabled: bool = Field(default=True, sa_column=Column(Boolean, server_default=text("true"), nullable=False))

    # Phase 24 (HERM-04, D-08): Generic single-instance flag for host-side workers.
    # When true, only one Worker container/process is permitted at a time on this
    # runtime. Hermes is the first user; future host-side workers (e.g. local fine-
    # tuned models) inherit the pattern automatically without hardcoded slug checks.
    single_instance: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
    )

    api_key_secret_id: uuid.UUID | None = Field(
        default=None,
        sa_column=Column(
            "api_key_secret_id",
            ForeignKey("secrets.id", ondelete="SET NULL"),
            nullable=True,
        ),
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

    def to_registry_dict(self) -> dict[str, Any]:
        """Shape compatible with the legacy runtimes.json format — consumed by
        runtime_manager helpers that still expect dicts."""
        return {
            "id": self.slug,
            "display_name": self.display_name,
            "runtime_type": self.runtime_type,
            "provider": "local",
            "endpoint": self.endpoint,
            "healthcheck_path": self.healthcheck_path,
            "model_identifier": self.model_identifier,
            "container_name": self.container_name,
            "lms_identifier": self.lms_identifier,
            "lms_cli_path": self.lms_cli_path,
            "launch_command": self.launch_command,
            "host": self.host,
            "control_url": self.control_url,
            "wol_mac_address": self.wol_mac_address,
            "power_managed": self.power_managed,
            "role_tags": self.role_tags or [],
            "supports_tools": self.supports_tools,
            "supports_reasoning": self.supports_reasoning,
            "supports_streaming": self.supports_streaming,
            "preferred_context_len": self.preferred_context_len,
            "max_context_len": self.max_context_len,
            "gpu_profile": self.gpu_profile,
            "memory_notes": self.memory_notes or "",
            "startup_notes": self.startup_notes or "",
            "ui_order": self.ui_order,
            "enabled": self.enabled,
            "single_instance": self.single_instance,
        }
