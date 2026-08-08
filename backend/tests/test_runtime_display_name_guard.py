"""A runtime's display name must not claim a model it does not serve (PR9).

``display_name_drift`` has existed since #183 but was only ever exercised by
the migration that introduced it — nothing in the running system called it.
Meanwhile ``qwen-general`` sat in the fleet as "Spark vLLM (Laguna/Qwen —
switchable)" while its ``model_identifier`` said
``deepseek-v4-flash-0731-spark``. The row was right and the label was a lie,
which is worse than an obviously empty field: nobody double-checks a name that
looks specific, so the wrong model name is what people reason from.

Contract these tests pin down:

* The finding rides along on every runtime payload (list AND write response),
  so the UI can surface it without a second round trip.
* A drifting name is a WARNING, never a rejection. Renaming and repointing are
  two legitimate steps, and a hard block would make the intermediate state
  unreachable — which is how guards get disabled instead of obeyed.
* display_name is only ever the box/purpose name. The live model is shown from
  ``model_identifier``, so a name WITHOUT a version claim is always honest.
"""
from unittest.mock import patch

import pytest

from app.models.runtime import Runtime


async def _stub_state(*_args, **_kwargs):
    return {"state": "ready", "http_reachable": True, "container_status": None}


async def _mk_rt(session, *, slug, display_name, model):
    rt = Runtime(
        slug=slug, display_name=display_name, runtime_type="vllm_docker",
        endpoint="http://192.0.2.10:8000/v1", model_identifier=model, enabled=True,
    )
    session.add(rt)
    await session.commit()
    await session.refresh(rt)
    return rt


@pytest.mark.asyncio
async def test_list_flags_a_name_that_claims_the_wrong_version(
    async_session, auth_client
):
    await _mk_rt(
        async_session, slug="lying-rt",
        display_name="Claude Opus 4.7 (Anthropic)", model="claude-opus-4-8",
    )
    with patch(
        "app.services.runtime_manager.get_runtime_state", side_effect=_stub_state
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    assert resp.status_code == 200, resp.text
    row = next(r for r in resp.json()["runtimes"] if r["slug"] == "lying-rt")
    assert row["display_name_drift"] == ["4.7"]


@pytest.mark.asyncio
async def test_list_leaves_an_honest_name_unflagged(async_session, auth_client):
    """The name Mark settled on for qwen-general: a box/purpose label with no
    version claim at all. Always honest, whatever the engine switches to."""
    await _mk_rt(
        async_session, slug="honest-rt",
        display_name="Spark vLLM (switchbar)",
        model="deepseek-v4-flash-0731-spark",
    )
    with patch(
        "app.services.runtime_manager.get_runtime_state", side_effect=_stub_state
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    row = next(r for r in resp.json()["runtimes"] if r["slug"] == "honest-rt")
    assert row["display_name_drift"] == []


@pytest.mark.asyncio
async def test_patch_that_renames_into_a_lie_warns_but_succeeds(
    async_session, auth_client
):
    rt = await _mk_rt(
        async_session, slug="rename-rt",
        display_name="Spark vLLM", model="deepseek-v4-flash-0731-spark",
    )
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={"display_name": "Spark vLLM (Laguna 2.1)"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["display_name"] == "Spark vLLM (Laguna 2.1)", "must not be rejected"
    assert body["display_name_drift"] == ["2.1"]


@pytest.mark.asyncio
async def test_patch_that_repoints_the_model_warns_about_the_stale_name(
    async_session, auth_client
):
    """The 08.08. shape in miniature: the model moves, the name stays behind.
    The warning must fire on a model_identifier edit too, not only on a rename.
    """
    rt = await _mk_rt(
        async_session, slug="repoint-rt",
        display_name="Claude Opus 4.8 (Anthropic)", model="claude-opus-4-8",
    )
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={"model_identifier": "claude-opus-5"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name_drift"] == ["4.8"]


@pytest.mark.asyncio
async def test_patch_that_fixes_the_name_clears_the_finding(
    async_session, auth_client
):
    rt = await _mk_rt(
        async_session, slug="fixed-rt",
        display_name="Claude Opus 4.8 (Anthropic)", model="claude-opus-5",
    )
    resp = await auth_client.patch(
        f"/api/v1/runtimes/db/{rt.slug}",
        json={"display_name": "Claude Opus 5 (Anthropic)"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name_drift"] == []


@pytest.mark.asyncio
async def test_create_response_carries_the_finding(auth_client):
    resp = await auth_client.post(
        "/api/v1/runtimes/db",
        json={
            "slug": "born-lying-rt",
            "display_name": "Qwen 3.6 (Spark)",
            "runtime_type": "vllm_docker",
            "endpoint": "http://192.0.2.10:8000/v1",
            "model_identifier": "deepseek-v4-flash-0731-spark",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["display_name_drift"] == ["3.6"]


@pytest.mark.asyncio
async def test_patch_logs_the_warning_for_writes_that_have_no_ui(
    async_session, auth_client, caplog
):
    """The response field only helps a caller that reads it. A PATCH from a
    script or another service has no UI to show a chip in, so the finding also
    has to land somewhere an operator can find it afterwards."""
    rt = await _mk_rt(
        async_session, slug="logged-rt",
        display_name="Claude Opus 4.8 (Anthropic)", model="claude-opus-4-8",
    )
    with caplog.at_level("WARNING", logger="mc.runtimes"):
        resp = await auth_client.patch(
            f"/api/v1/runtimes/db/{rt.slug}",
            json={"model_identifier": "claude-opus-5"},
        )
    assert resp.status_code == 200, resp.text
    warnings = [r.getMessage() for r in caplog.records if r.name == "mc.runtimes"]
    assert any("logged-rt" in m and "4.8" in m for m in warnings), warnings


@pytest.mark.asyncio
async def test_patch_of_an_honest_row_logs_nothing(async_session, auth_client, caplog):
    """A guard that cries on every write gets ignored."""
    rt = await _mk_rt(
        async_session, slug="quiet-rt",
        display_name="Spark vLLM (switchbar)", model="deepseek-v4-flash-0731-spark",
    )
    with caplog.at_level("WARNING", logger="mc.runtimes"):
        resp = await auth_client.patch(
            f"/api/v1/runtimes/db/{rt.slug}",
            json={"display_name": "Spark vLLM (switchbar, DGX)"},
        )
    assert resp.status_code == 200, resp.text
    assert not [r for r in caplog.records if r.name == "mc.runtimes"]


@pytest.mark.asyncio
async def test_row_without_a_model_is_never_flagged(async_session, auth_client):
    """Nothing to check against. Flagging here would only punish rows whose
    model the probe has not filled in yet."""
    await _mk_rt(
        async_session, slug="no-model-rt",
        display_name="LM Studio 3.4 (DGX)", model=None,
    )
    with patch(
        "app.services.runtime_manager.get_runtime_state", side_effect=_stub_state
    ):
        resp = await auth_client.get("/api/v1/runtimes")

    row = next(r for r in resp.json()["runtimes"] if r["slug"] == "no-model-rt")
    assert row["display_name_drift"] == []
