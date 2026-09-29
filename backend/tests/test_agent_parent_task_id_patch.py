"""PATCH `parent_task_id` — attaching a parent after creation (Task f8c9cdb9).

Point 5 of the card, taken from its Context prose: "Attaching a parent
afterwards is impossible on the agent route: `PATCH {"parent_task_id": ...}`
returns 422 `extra_forbidden`."

Root cause: `AgentTaskUpdate` declares `model_config = ConfigDict(extra="forbid")`
and simply did not list the field, so the PATCH body was rejected by the schema
before the handler ever ran — an orphaned card could never be re-attached. That
is the cleanup path for exactly the orphans this task is about.

The tests drive the real HTTP route against the real app and read the result
back from the DB, because "the field is now declared" is not the observable
contract — the observable contract is that the row's `parent_task_id` moved, or
that it refused with the right status and left the row alone.
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from sqlmodel.ext.asyncio.session import AsyncSession

from tests.conftest import test_engine

_BROADCAST_PATCH = patch("app.services.activity.broadcast", new_callable=AsyncMock)


async def _mk_board(name):
    from app.models.board import Board

    board_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Board(id=board_id, name=name, slug=f"ptid-{uuid.uuid4().hex[:8]}"))
        await s.commit()
    return board_id


async def _mk_agent(board_id, name, *, lead=False, scopes=None):
    from app.auth import generate_agent_token
    from app.models.agent import Agent

    agent_id = uuid.uuid4()
    token, token_hash = generate_agent_token()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Agent(
            id=agent_id, name=name, role="orchestrator" if lead else "developer",
            board_id=board_id, agent_token_hash=token_hash, is_board_lead=lead,
            provision_status="provisioned",
            scopes=scopes or ["tasks:read", "tasks:write", "tasks:create"],
        ))
        await s.commit()
    return agent_id, token


async def _mk_task(board_id, title, *, status="in_progress", assignee=None, parent=None):
    from app.models.task import Task

    task_id = uuid.uuid4()
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        s.add(Task(
            id=task_id, board_id=board_id, title=title, status=status,
            assigned_agent_id=assignee, parent_task_id=parent,
        ))
        await s.commit()
    return task_id


async def _read_parent(task_id):
    """Fresh DB read — never the identity-map copy the request left behind."""
    from app.models.task import Task

    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        return task.parent_task_id, task.status


@pytest.mark.asyncio
async def test_lead_can_attach_parent_to_orphan_card(client, fake_redis):
    """The card's exact defect: 422 extra_forbidden → 200 + parent in the DB.

    This is also the repair path for the orphans this task is about (8d039889,
    b7d29be3, 70d6b417) — before the fix there was no way to hang them under a
    parent through the agent route at all.
    """
    board = await _mk_board("Attach")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    parent_id = await _mk_task(board, "Parent", status="in_progress", assignee=lead_id)
    orphan_id = await _mk_task(board, "Orphan", status="inbox", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{orphan_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(parent_id)},
        )

    assert resp.status_code == 200, resp.text
    parent, status = await _read_parent(orphan_id)
    assert parent == parent_id, "parent_task_id muss in der DB stehen"
    assert status == "inbox", (
        "das Anhaengen eines Parents darf den Status der Karte nicht anfassen"
    )


@pytest.mark.asyncio
async def test_extra_forbidden_is_gone_for_parent_task_id(client, fake_redis):
    """Guards against the field being dropped from the schema again."""
    board = await _mk_board("NoExtra")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    parent_id = await _mk_task(board, "Parent", assignee=lead_id)
    orphan_id = await _mk_task(board, "Orphan", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{orphan_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(parent_id)},
        )

    if resp.status_code == 422:
        detail = resp.json().get("detail")
        types = {d.get("type") for d in detail} if isinstance(detail, list) else set()
        assert "extra_forbidden" not in types, (
            f"parent_task_id ist wieder aus AgentTaskUpdate verschwunden: {detail}"
        )


@pytest.mark.asyncio
async def test_unknown_field_still_rejected(client, fake_redis):
    """The fail-closed policy stays: declaring one more field must not turn
    the schema permissive (that is the whole reason the 422 existed)."""
    board = await _mk_board("StillClosed")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    task_id = await _mk_task(board, "T", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"grandparent_task_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 422, resp.text


@pytest.mark.asyncio
async def test_parent_must_exist(client, fake_redis):
    board = await _mk_board("MissingParent")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    task_id = await _mk_task(board, "T", assignee=lead_id)
    bogus = uuid.uuid4()

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(bogus)},
        )

    assert resp.status_code == 404, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent is None


@pytest.mark.asyncio
async def test_parent_must_be_on_same_board(client, fake_redis):
    """Cross-board grafting would leak a card into another board's hierarchy."""
    board = await _mk_board("Home")
    other = await _mk_board("Foreign")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    task_id = await _mk_task(board, "T", assignee=lead_id)
    foreign_parent = await _mk_task(other, "Foreign parent")

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(foreign_parent)},
        )

    assert resp.status_code == 403, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent is None


@pytest.mark.parametrize("closed_status", ["done", "archived", "failed"])
@pytest.mark.asyncio
async def test_closed_parent_refuses(client, fake_redis, closed_status):
    """A closed card cannot take new children — its callback/resume chain is
    already terminal."""
    board = await _mk_board(f"Closed-{closed_status}")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    parent_id = await _mk_task(board, "Closed parent", status=closed_status, assignee=lead_id)
    task_id = await _mk_task(board, "T", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(parent_id)},
        )

    assert resp.status_code == 409, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent is None


@pytest.mark.asyncio
async def test_non_lead_cannot_graft_onto_a_foreign_card(client, fake_redis):
    """The role check: a worker may not hang its card under somebody else's."""
    board = await _mk_board("ForeignGraft")
    owner_id, _ = await _mk_agent(board, "Owner")
    worker_id, worker_token = await _mk_agent(board, "Worker")
    foreign_parent = await _mk_task(board, "Owner's card", assignee=owner_id)
    task_id = await _mk_task(board, "Worker card", assignee=worker_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json={"parent_task_id": str(foreign_parent)},
        )

    assert resp.status_code == 409, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent is None
    assert "Board-Lead" in resp.json()["detail"], (
        "die Ablehnung muss die Ausnahme benennen, sonst ist 'nicht deine Arbeit' "
        "von 'grundsaetzlich verboten' nicht zu unterscheiden"
    )


@pytest.mark.asyncio
async def test_lead_may_graft_onto_a_foreign_card(client, fake_redis):
    """The other half of the role check: a Board Lead is the one role allowed
    to hang a card under somebody else's (that is how an orphan gets repaired
    under the lead's own structure). Without this case the guard is
    indistinguishable from a blanket ownership ban — dropping `not
    agent.is_board_lead` from it leaves every other test green."""
    board = await _mk_board("LeadForeignGraft")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    owner_id, _ = await _mk_agent(board, "Owner")
    foreign_parent = await _mk_task(board, "Owner's card", assignee=owner_id)
    task_id = await _mk_task(board, "Lead card", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(foreign_parent)},
        )

    assert resp.status_code == 200, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent == foreign_parent, (
        "ein Board Lead muss auf eine fremde Karte pfropfen duerfen — genau das "
        "ist der Reparaturweg fuer die Waisenkarten dieser Task"
    )


@pytest.mark.asyncio
async def test_non_lead_may_attach_to_own_card(client, fake_redis):
    """Counter-case to the guard above: the ownership exception must work, or
    the check is just a blanket ban for workers."""
    board = await _mk_board("OwnGraft")
    worker_id, worker_token = await _mk_agent(board, "Worker")
    own_parent = await _mk_task(board, "Own card", assignee=worker_id)
    task_id = await _mk_task(board, "Worker card", assignee=worker_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {worker_token}"},
            json={"parent_task_id": str(own_parent)},
        )

    assert resp.status_code == 200, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent == own_parent


@pytest.mark.asyncio
async def test_self_parent_refused(client, fake_redis):
    board = await _mk_board("SelfParent")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    task_id = await _mk_task(board, "T", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(task_id)},
        )

    assert resp.status_code == 409, resp.text
    parent, _ = await _read_parent(task_id)
    assert parent is None, "eine Karte darf nicht ihr eigener Parent sein"


@pytest.mark.asyncio
async def test_ancestor_cycle_refused(client, fake_redis):
    """A → B → C, then attach A under C. Every hierarchy walk in the codebase
    (hierarchy endpoint, callback/resume chain) assumes this cannot happen."""
    board = await _mk_board("Cycle")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)

    a = await _mk_task(board, "A", assignee=lead_id)
    b = await _mk_task(board, "B", assignee=lead_id, parent=a)
    c = await _mk_task(board, "C", assignee=lead_id, parent=b)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{a}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(c)},
        )

    assert resp.status_code == 409, resp.text
    assert "Zyklus" in resp.json()["detail"]
    parent, _ = await _read_parent(a)
    assert parent is None, "der Zyklus darf nicht geschrieben worden sein"


@pytest.mark.asyncio
async def test_deep_ancestor_chain_refused_no_depth_bound(client, fake_redis):
    """A chain DEEPER than the old 64-step cap must still be refused (task b410d705).

    The cap made this guard unsound instead of safe: a 70-link chain was walked
    only to step 64, the loop fell through, and the attach was WRITTEN — the
    cycle landed in the DB (200, observed on the pre-fix head). Depth is the
    wrong axis anyway: the walk carries `_seen`, every node has exactly one
    parent pointer, so a revisit can only mean the chain loops and the walk
    terminates on its own. This test fails the moment a depth bound is put
    back in front of the set (it is the reintroduction detector — the whole
    point of the change, so it is not just "a test for the fix").
    """
    board = await _mk_board("DeepChain")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)

    root = await _mk_task(board, "L0", assignee=lead_id)
    node = root
    for level in range(1, 70):
        node = await _mk_task(board, f"L{level}", assignee=lead_id, parent=node)
    deepest = node

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{root}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(deepest)},
        )

    assert resp.status_code == 409, (
        f"70er-Kette muss abgelehnt werden (Status {resp.status_code}) — "
        f"ein Depth-Cap laesst sie durch und schreibt den Zyklus"
    )
    assert "Vorfahre" in resp.json()["detail"]
    parent, _ = await _read_parent(root)
    assert parent is None, "der Zyklus darf nicht geschrieben worden sein"


@pytest.mark.asyncio
async def test_preexisting_cycle_in_chain_refused_with_repair_hint(client, fake_redis):
    """Damaged data must raise, not silent-pass (task b410d705).

    Pre-fix the walk hit the second visit, `break`-ed, and then wrote the
    attach — 200 with the row changed. A chain that already loops is exactly
    where silent passage is worst: the new edge grafts a third card onto the
    loop, and every other hierarchy walk in the codebase assumes no loop
    exists. The refusal has to name the repair, not just say no.
    """
    board = await _mk_board("PreCycle")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)

    t = await _mk_task(board, "T", assignee=lead_id)
    p = await _mk_task(board, "P", assignee=lead_id)
    q = await _mk_task(board, "Q", assignee=lead_id, parent=p)
    # Fabricated damaged state that no endpoint would produce: P -> Q -> P.
    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        p_row = await s.get(Task, p)
        p_row.parent_task_id = q
        s.add(p_row)
        await s.commit()

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{t}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": str(p)},
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert "Zyklus" in detail, "der Befund muss den Zyklus benennen"
    assert "repariert" in detail, "die Meldung muss die Reparatur nennen"
    parent, _ = await _read_parent(t)
    assert parent is None, "in kaputte Daten darf nicht hineingeschrieben werden"


@pytest.mark.asyncio
async def test_detach_via_explicit_null_makes_the_card_a_root_again(client, fake_redis):
    """`{"parent_task_id": null}` must detach, not silently no-op.

    The handler reads `model_dump(exclude_none=True)`, so an explicit null never
    reaches `updates` on its own — that would be the same
    "200-but-nothing-happened" class this field's sibling bugs were about.
    """
    board = await _mk_board("Detach")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    parent_id = await _mk_task(board, "Parent", assignee=lead_id)
    child_id = await _mk_task(board, "Child", assignee=lead_id, parent=parent_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{child_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"parent_task_id": None},
        )

    assert resp.status_code == 200, resp.text
    parent, _ = await _read_parent(child_id)
    assert parent is None, "explicit null muss den Parent loesen"


@pytest.mark.asyncio
async def test_rejected_attach_leaves_other_fields_unapplied(client, fake_redis):
    """A refused parent must abort the whole PATCH, not half-apply it.

    Validation runs before the generic setattr loop precisely so a later guard
    cannot leave the field applied — and conversely, a title sent alongside a
    bad parent must not land either.
    """
    board = await _mk_board("Atomic")
    lead_id, lead_token = await _mk_agent(board, "Boss", lead=True)
    task_id = await _mk_task(board, "Original title", assignee=lead_id)

    with _BROADCAST_PATCH:
        resp = await client.patch(
            f"/api/v1/agent/boards/{board}/tasks/{task_id}",
            headers={"Authorization": f"Bearer {lead_token}"},
            json={"title": "Should not land", "parent_task_id": str(uuid.uuid4())},
        )

    assert resp.status_code == 404, resp.text

    from app.models.task import Task
    async with AsyncSession(test_engine, expire_on_commit=False) as s:
        task = await s.get(Task, task_id)
        assert task.title == "Original title", "abgelehnter PATCH darf nichts schreiben"
        assert task.parent_task_id is None
