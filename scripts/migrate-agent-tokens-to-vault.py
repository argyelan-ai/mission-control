#!/usr/bin/env python3
"""Migriert MC_TOKEN_* aus docker/.env.agents in die secrets Tabelle (Fernet-encrypted).

Einmalig ausfuehren. Danach generiert start-all.sh die .env.agents aus der DB.

Usage:
    docker compose exec backend python3 /app/scripts/migrate-agent-tokens-to-vault.py

Oder vom Host (mit Backend-venv):
    cd backend && source .venv/bin/activate
    python3 ../scripts/migrate-agent-tokens-to-vault.py
"""
import asyncio
import os
import sys
from pathlib import Path

# Backend-Module laden
sys.path.insert(0, str(Path(__file__).parent.parent / "backend"))
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://mc:mc@localhost:5432/mission_control")


async def migrate():
    from sqlmodel import select
    from sqlmodel.ext.asyncio.session import AsyncSession
    from app.database import engine
    from app.models.secret import Secret
    from app.services.encryption import encrypt

    # 1. .env.agents lesen
    env_file = Path(__file__).parent.parent / "docker" / ".env.agents"
    if not env_file.exists():
        print(f"FEHLER: {env_file} nicht gefunden")
        return

    tokens: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.startswith("MC_TOKEN_") and value:
            agent_name = key.replace("MC_TOKEN_", "").lower()
            tokens[agent_name] = value

    if not tokens:
        print("Keine MC_TOKEN_* Eintraege gefunden")
        return

    print(f"Gefunden: {len(tokens)} Agent-Tokens")

    # 2. In secrets Tabelle speichern
    async with AsyncSession(engine) as session:
        for agent_name, raw_token in tokens.items():
            secret_key = f"mc_token_{agent_name}"

            # Pruefen ob schon existiert
            existing = (await session.exec(
                select(Secret).where(Secret.key == secret_key)
            )).first()

            encrypted = encrypt(raw_token)

            if existing:
                existing.encrypted_value = encrypted
                session.add(existing)
                print(f"  {agent_name}: aktualisiert")
            else:
                secret = Secret(
                    key=secret_key,
                    encrypted_value=encrypted,
                    provider="mc-agent",
                    label=f"Agent Token: {agent_name.title()}",
                    description=f"PBKDF2-Auth Token fuer Docker-Agent {agent_name}",
                )
                session.add(secret)
                print(f"  {agent_name}: neu angelegt")

        await session.commit()

    print(f"\nDone — {len(tokens)} Tokens in secrets Tabelle migriert.")
    print("start-all.sh generiert .env.agents jetzt automatisch aus der DB.")


if __name__ == "__main__":
    asyncio.run(migrate())
