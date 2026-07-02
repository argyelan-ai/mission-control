#!/usr/bin/env python3
"""E2E Integration Test — mc pdf Flow ohne Mocks.

Verifiziert die volle Verkettung mit live docker-compose stack:
  HTTP-Call (agent-token) → Backend-Endpoint (auth + scope + ownership)
  → Backend → mc-playwright Sidecar (HTTP, intra-compose)
  → Sidecar → Chromium page.pdf() → Disk /shared-deliverables/<task>/
  → Backend DB-Insert TaskDeliverable
  → PDF-Magic-Bytes check + Cleanup

Muss IM Backend-Container laufen (sidecar hostname `mc-playwright:8790` nur
im Compose-Netzwerk). Aus dem Repo-Root:

  docker compose exec -T backend python3 /app/scripts/e2e-pdf-test.py

Exit-Code 0 = PASS, !=0 = FAIL mit detailliertem Trace.

Das ist KEIN pytest-Test — Standard-pytest-Suite nutzt Mocks (schneller, kein
Sidecar-Dependency). Dieser Script hier ist der out-of-band Sanity-Check
gegen den echten live-stack. Manuell ausgefuehrt nach jedem mc-pdf-/Sidecar-
Change.
"""

import asyncio
import os
import sys
import uuid

# Script ist fuer Execution IM backend-container gedacht — Mount via /app
# Wir haengen den PYTHONPATH nach /app wenn noetig
sys.path.insert(0, os.environ.get("APP_PATH", "/app"))

import httpx  # noqa: E402
from sqlmodel import select  # noqa: E402
from sqlmodel.ext.asyncio.session import AsyncSession  # noqa: E402


# Compose-DNS: mc-playwright:8790, backend:8000. Fallback auf localhost fuer
# dev-setups mit gepublished ports.
BACKEND_URL = os.environ.get("MC_BACKEND_URL", "http://localhost:8000")
SIDECAR_URL = os.environ.get("MC_PLAYWRIGHT_URL", "http://mc-playwright:8790")


async def main() -> int:
    # Lazy-imports damit der Script-Header sauber bleibt
    from app.database import engine
    from app.models.agent import Agent
    from app.models.board import Board
    from app.models.deliverable import TaskDeliverable
    from app.models.task import Task
    from app.auth import generate_agent_token

    # Preflight
    try:
        async with httpx.AsyncClient(timeout=3.0) as c:
            r = await c.get(f"{BACKEND_URL}/health")
            assert r.status_code == 200, f"Backend /health {r.status_code}"
            r = await c.get(f"{SIDECAR_URL}/health")
            assert r.status_code == 200, f"Sidecar /health {r.status_code}"
    except Exception as e:
        print(f"✗ Preflight FAIL: {e}", file=sys.stderr)
        return 2

    test_agent_id = uuid.uuid4()
    test_task_id = uuid.uuid4()
    test_board_id = uuid.uuid4()
    token_raw, token_hash = generate_agent_token()

    try:
        # Setup (sequential commits wegen FK-Order Board→Agent→Task)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            s.add(Board(
                id=test_board_id, name="E2E PDF Test",
                slug=f"e2e-pdf-{uuid.uuid4().hex[:8]}",
            ))
            await s.commit()

        async with AsyncSession(engine, expire_on_commit=False) as s:
            s.add(Agent(
                id=test_agent_id, name=f"E2E_Agent_{uuid.uuid4().hex[:6]}",
                role="developer", board_id=test_board_id,
                agent_token_hash=token_hash,
                scopes=["tasks:read", "tasks:write"],
                gateway_agent_id=f"gw-e2e-{uuid.uuid4().hex[:6]}",
                provision_status="provisioned",
            ))
            await s.commit()

        async with AsyncSession(engine, expire_on_commit=False) as s:
            s.add(Task(
                id=test_task_id, board_id=test_board_id,
                title="E2E PDF Test Task", status="in_progress",
                assigned_agent_id=test_agent_id, owner_agent_id=test_agent_id,
            ))
            await s.commit()

        print(f"• Setup: agent={test_agent_id}, task={test_task_id}")

        # Real HTTP call — no mock
        markdown = (
            "# E2E Integration Test\n\n"
            "Vollstaendige Verkettung: Backend → Sidecar → PDF → Deliverable → Disk.\n\n"
            "## Table\n\n"
            "| Layer | Status |\n|---|---|\n"
            "| Backend | Live |\n| Sidecar | Live |\n"
        )
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                f"{BACKEND_URL}/api/v1/agent/boards/{test_board_id}/tasks/{test_task_id}/pdf",
                json={
                    "title": "E2E Integration",
                    "markdown": markdown,
                    "filename_prefix": f"e2e-{uuid.uuid4().hex[:6]}",
                },
                headers={"Authorization": f"Bearer {token_raw}"},
            )
        assert resp.status_code == 200, f"HTTP {resp.status_code}: {resp.text}"
        data = resp.json()
        print(f"• HTTP 200: deliverable_id={data['deliverable_id']}")

        # DB-Persistenz
        async with AsyncSession(engine, expire_on_commit=False) as s:
            d = await s.get(TaskDeliverable, uuid.UUID(data["deliverable_id"]))
            assert d is not None, "Deliverable fehlt in DB"
            assert d.deliverable_type == "file"
            assert d.task_id == test_task_id
            assert d.agent_id == test_agent_id
            print(f"• DB row: type={d.deliverable_type}, path={d.path}")

        # Disk + Magic-Bytes
        assert os.path.isfile(data["path"]), f"PDF fehlt: {data['path']}"
        file_size = os.path.getsize(data["path"])
        assert file_size > 1000, f"PDF zu klein: {file_size} bytes"
        with open(data["path"], "rb") as f:
            magic = f.read(5)
        assert magic == b"%PDF-", f"Nicht PDF: {magic!r}"
        print(f"• PDF on disk: {file_size} bytes, magic %PDF- OK")

        print("\n🎉 E2E PASS — Backend→Sidecar→PDF→Deliverable→Disk verkettet korrekt")
        return 0

    finally:
        # Cleanup (reverse FK order)
        async with AsyncSession(engine, expire_on_commit=False) as s:
            delivs = (await s.exec(
                select(TaskDeliverable).where(TaskDeliverable.task_id == test_task_id)
            )).all()
            for d in list(delivs):
                await s.delete(d)
            t = await s.get(Task, test_task_id)
            if t:
                await s.delete(t)
            await s.commit()
        async with AsyncSession(engine, expire_on_commit=False) as s:
            a = await s.get(Agent, test_agent_id)
            if a:
                await s.delete(a)
            await s.commit()
        async with AsyncSession(engine, expire_on_commit=False) as s:
            b = await s.get(Board, test_board_id)
            if b:
                await s.delete(b)
            await s.commit()
        print("• Cleanup done")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
