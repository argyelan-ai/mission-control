"""HostPairingCode — one-shot pairing code for self-registering node agents.

Fleet & Rezepte v2, Phase 1 (see docs/plans/2026-08-30-node-agent-telemetry-phase1.md).
An operator mints a code via POST /api/v1/nodes/pairing-codes (auth required),
then runs the printed install one-liner on the target box. The unauthenticated
POST /api/v1/nodes/pair endpoint trades the code for a node_token — the code
is single-use (``used_at``) and short-lived (``expires_at``, 15 minutes), so a
leaked install command (shoulder-surfed, pasted into the wrong chat) is a dead
end once it is redeemed or expires.

``host_id`` is nullable: an operator can pre-create the Host row (known device,
chosen slug/display name) and pair a code to it, or leave it null and let
POST /pair create the host on the fly from the reporting hostname.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, Text, text
from sqlmodel import Column, Field, SQLModel


class HostPairingCode(SQLModel, table=True):
    __tablename__ = "host_pairing_codes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    code: str = Field(index=True, unique=True, max_length=12)
    host_id: uuid.UUID | None = Field(default=None, foreign_key="hosts.id")
    display_name_hint: str | None = Field(default=None, max_length=128)
    # P2 (Vertrag 02.09.2026): Rolle und SSH-Adresse, die der Betreiber beim
    # Minten mitgibt — beim Einlösen wandern sie auf den Host. Am Code
    # gespeichert (nicht am Host), weil der Host beim Minten oft noch nicht
    # existiert: er entsteht erst, wenn das Gerät den Code einlöst.
    # TEXT wie hosts.role/fabric_ip (Migration 0192) — Modell und Migration
    # dürfen nicht auseinanderlaufen (Review 03.09.2026).
    role: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    ssh_host: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), nullable=False))
    used_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), nullable=True))
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False),
    )
