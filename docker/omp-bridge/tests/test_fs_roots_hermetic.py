"""The omp-bridge suite behaves the same inside an agent container as on
the host (conftest.py).

The fs jail serves extra roots that exist only in a container: the
deliverables mount and, behind the /.dockerenv marker, the container's
/tmp. A test that builds a real ACPClient — the replay tests, through
fake_acp_server.InProcessFake — would then really serve them: the
18.10.1 fixture sends fs/write_text_file /tmp/ws/demo.py, and in a
container that file got written. conftest.py switches both off for every
test; a test that wants a root sets it itself (test_fs_jail.py).
"""
from __future__ import annotations

import os

import acp_client


def test_suite_clients_get_no_container_roots(monkeypatch, tmp_path):
    """As inside an agent container — the marker is there, the deliverables
    folder and the scratch folder exist: a client built by a test still
    serves its session cwd only, and writes nothing into either folder."""
    cwd = tmp_path / "cwd"
    deliverables = tmp_path / "deliverables"
    scratch = tmp_path / "tmp"
    for folder in (cwd, deliverables, scratch):
        folder.mkdir()
    monkeypatch.setattr(acp_client, "CONTAINER_MARKER", os.path.abspath(__file__))
    monkeypatch.setattr(acp_client, "DEFAULT_DELIVERABLES_DIR", str(deliverables))
    monkeypatch.setattr(acp_client, "DEFAULT_SCRATCH_DIRS", (str(scratch),))

    assert acp_client.deliverables_roots() == ()
    assert acp_client.scratch_roots() == ()
    client = acp_client.ACPClient(command=["never-spawned"])
    client._fs_jail = os.path.realpath(cwd)  # as new_session() sets it
    sent: list = []
    client._send_raw = sent.append  # type: ignore[method-assign]
    for folder in (deliverables, scratch):
        sent.clear()
        client._dispatch({"jsonrpc": "2.0", "id": 7, "method": "fs/write_text_file",
                          "params": {"path": str(folder / "demo.py"), "content": "x"}})

        assert sent and sent[0].get("error", {}).get("code") == -32002, (folder, sent)
        assert not (folder / "demo.py").exists(), folder
