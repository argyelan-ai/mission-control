"""node-agent: running containers/processes from an allowlist (ADR-096 §3.2, PR 4).

The agent reads which engine containers/processes are alive, but ONLY for
the names MC lists in ``desired_state.watch`` — never a full inventory of
the box. Read-only: one ``GET /containers/json`` on the Docker socket, or
``/proc`` for process anchors.

Fallback rule under test: when the service user may not read Docker, a
container anchor is ``unknown`` — never ``running``. Process anchors still
work, because they only need ``/proc``.

Fake Docker = a real unix-socket HTTP server in a thread that answers with a
canned ``/containers/json`` body. Fake ``/proc`` = a temp directory.
"""
from __future__ import annotations

import importlib.util
import json
import os
import socket
import tempfile
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = REPO_ROOT / "scripts" / "node-agent" / "mc-node-agent.py"


def _load_agent():
    spec = importlib.util.spec_from_file_location("mc_node_agent_anchors_test", MODULE_PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def agent():
    return _load_agent()


# ── Fake Docker ───────────────────────────────────────────────────────────────


def _container(name: str, state: str = "running") -> dict:
    return {"Id": "x" * 12, "Names": [f"/{name}"], "State": state, "Image": "engine:latest"}


class FakeDocker:
    """A unix-socket server that answers every request with one canned body.

    Short socket path on purpose: macOS caps AF_UNIX paths at 104 bytes and a
    pytest tmp_path is often longer than that.
    """

    def __init__(self, containers: list[dict], chunked: bool = False, status: int = 200):
        self.dir = tempfile.mkdtemp(prefix="mcfd")
        self.path = os.path.join(self.dir, "d.sock")
        self.requests: list[bytes] = []
        self._body = json.dumps(containers).encode()
        self._chunked = chunked
        self._status = status
        self._srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._srv.bind(self.path)
        self._srv.listen(4)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while True:
            try:
                conn, _ = self._srv.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                self.requests.append(data)
                head = f"HTTP/1.1 {self._status} OK\r\nContent-Type: application/json\r\n"
                if self._chunked:
                    half = len(self._body) // 2
                    parts = [self._body[:half], self._body[half:]]
                    body = b"".join(f"{len(p):x}\r\n".encode() + p + b"\r\n" for p in parts) + b"0\r\n\r\n"
                    conn.sendall((head + "Transfer-Encoding: chunked\r\n\r\n").encode() + body)
                else:
                    conn.sendall((head + f"Content-Length: {len(self._body)}\r\n\r\n").encode() + self._body)

    def close(self):
        self._srv.close()
        try:
            os.unlink(self.path)
            os.rmdir(self.dir)
        except OSError:
            pass


@pytest.fixture
def fake_docker():
    made: list[FakeDocker] = []

    def make(containers, **kw):
        fd = FakeDocker(containers, **kw)
        made.append(fd)
        return fd

    yield make
    for fd in made:
        fd.close()


def _fake_proc(root: Path, procs: dict[int, list[str]], comms: dict[int, str] | None = None) -> Path:
    """procs: pid → argv. Writes <root>/<pid>/cmdline (NUL-separated) + comm."""
    comms = comms or {}
    for pid, argv in procs.items():
        d = root / str(pid)
        d.mkdir(parents=True)
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        comm = comms.get(pid, os.path.basename(argv[0])[:15] if argv else "")
        (d / "comm").write_text(comm + "\n")
    return root


# ── parse_watch: the allowlist is strict ─────────────────────────────────────


class TestParseWatch:
    def test_valid_entries_pass(self, agent):
        watch, errors = agent.parse_watch(
            {"watch": [{"kind": "container", "name": "glm-head"}, {"kind": "process", "name": "llama-server"}]}
        )
        assert watch == [
            {"kind": "container", "name": "glm-head"},
            {"kind": "process", "name": "llama-server"},
        ]
        assert errors == []

    def test_no_watch_key_means_no_anchors(self, agent):
        assert agent.parse_watch({"gpu_mode": "eco"}) == ([], [])
        assert agent.parse_watch(None) == ([], [])

    @pytest.mark.parametrize(
        "entry",
        [
            {"kind": "container", "name": "x; rm -rf /"},
            {"kind": "container", "name": "../etc"},
            {"kind": "container", "name": "-flag"},
            {"kind": "container", "name": "a" * 65},
            {"kind": "container", "name": ""},
            {"kind": "container", "name": 7},
            {"kind": "shell", "name": "ok"},
            {"name": "ok"},
            "glm-head",
        ],
    )
    def test_bad_entries_are_dropped_with_an_error(self, agent, entry):
        watch, errors = agent.parse_watch({"watch": [entry]})
        assert watch == []
        assert len(errors) == 1

    def test_list_is_capped(self, agent):
        many = [{"kind": "process", "name": f"p{i}"} for i in range(agent.WATCH_MAX + 5)]
        watch, errors = agent.parse_watch({"watch": many})
        assert len(watch) == agent.WATCH_MAX
        assert errors

    def test_duplicates_collapse(self, agent):
        watch, _ = agent.parse_watch({"watch": [{"kind": "process", "name": "p"}] * 3})
        assert watch == [{"kind": "process", "name": "p"}]

    def test_non_list_watch_is_an_error(self, agent):
        assert agent.parse_watch({"watch": "glm-head"})[0] == []
        assert agent.parse_watch({"watch": "glm-head"})[1]


# ── Docker read (fake socket) ────────────────────────────────────────────────


class TestReadDocker:
    def test_lists_running_names(self, agent, fake_docker):
        fd = fake_docker([_container("glm-head"), _container("other")])
        status, names = agent.read_docker_running(["glm-head"], socket_path=fd.path)
        assert status == "ok"
        assert "glm-head" in names

    def test_request_is_a_read_only_get_with_name_filter(self, agent, fake_docker):
        fd = fake_docker([])
        agent.read_docker_running(["glm-head"], socket_path=fd.path)
        line = fd.requests[0].split(b"\r\n", 1)[0].decode()
        assert line.startswith("GET /containers/json?filters=")
        assert "glm-head" in line
        assert "POST" not in line and "DELETE" not in line

    def test_chunked_answer_is_read(self, agent, fake_docker):
        fd = fake_docker([_container("glm-head"), _container("b" * 40)], chunked=True)
        status, names = agent.read_docker_running(["glm-head"], socket_path=fd.path)
        assert status == "ok"
        assert "glm-head" in names

    def test_exited_container_is_not_running(self, agent, fake_docker):
        fd = fake_docker([_container("glm-head", state="exited")])
        status, names = agent.read_docker_running(["glm-head"], socket_path=fd.path)
        assert status == "ok"
        assert "glm-head" not in names

    def test_no_socket_is_unavailable(self, agent, tmp_path):
        status, names = agent.read_docker_running(["glm-head"], socket_path=str(tmp_path / "nope.sock"))
        assert status == "unavailable"
        assert names == set()

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_permission_denied_is_no_access(self, agent, fake_docker):
        fd = fake_docker([_container("glm-head")])
        os.chmod(fd.path, 0)
        status, names = agent.read_docker_running(["glm-head"], socket_path=fd.path)
        assert status == "no_access"
        assert names == set()

    def test_http_error_is_unavailable(self, agent, fake_docker):
        fd = fake_docker([_container("glm-head")], status=500)
        status, names = agent.read_docker_running(["glm-head"], socket_path=fd.path)
        assert status == "unavailable"
        assert names == set()


# ── Process anchors (/proc) ──────────────────────────────────────────────────


class TestProcessRunning:
    def test_binary_name_matches(self, agent, tmp_path):
        root = _fake_proc(tmp_path, {101: ["/opt/llama/llama-server", "--port", "8000"]})
        assert agent.process_running("llama-server", proc_root=str(root))

    def test_python_entry_script_matches(self, agent, tmp_path):
        root = _fake_proc(tmp_path, {101: ["/usr/bin/python3", "/usr/local/bin/vllm", "serve"]})
        assert agent.process_running("vllm", proc_root=str(root))

    def test_python_dash_m_module_matches(self, agent, tmp_path):
        root = _fake_proc(tmp_path, {101: ["python3", "-m", "sglang.launch_server", "--tp", "2"]})
        assert agent.process_running("sglang.launch_server", proc_root=str(root))

    def test_name_as_an_argument_is_not_a_match(self, agent, tmp_path):
        # `grep vllm`, `tail -f vllm`, `docker logs vllm` must never read as "running".
        root = _fake_proc(
            tmp_path,
            {
                101: ["grep", "vllm"],
                102: ["tail", "-f", "/var/log/vllm"],
                103: ["docker", "logs", "vllm"],
                104: ["/usr/bin/python3", "/opt/tools/check.py", "vllm"],
            },
        )
        assert not agent.process_running("vllm", proc_root=str(root))

    def test_substring_is_not_a_match(self, agent, tmp_path):
        root = _fake_proc(tmp_path, {101: ["/opt/llama/llama-server-old"]})
        assert not agent.process_running("llama-server", proc_root=str(root))

    def test_missing_proc_is_false(self, agent, tmp_path):
        assert not agent.process_running("x", proc_root=str(tmp_path / "missing"))

    # ── engine command lines seen in the wild (critic finding 1) ────────────

    @pytest.mark.parametrize(
        "argv",
        [
            ["python3", "-u", "/usr/local/bin/vllm", "serve"],
            ["python3", "-X", "utf8", "/usr/local/bin/vllm", "serve"],
            ["python3", "-Xutf8", "-W", "ignore", "/usr/local/bin/vllm", "serve"],
            ["python3", "-uB", "/usr/local/bin/vllm", "serve"],
            ["python3", "--", "/usr/local/bin/vllm", "serve"],
            ["vllm serve /model --port 8000"],  # setproctitle: one string, spaces
            ["uv", "run", "vllm", "serve", "/model"],
            ["uv", "run", "--with", "flashinfer", "vllm", "serve"],
            ["uvx", "vllm", "serve"],
        ],
    )
    def test_real_engine_command_lines_match(self, agent, tmp_path, argv):
        root = _fake_proc(tmp_path, {101: argv})
        assert agent.process_running("vllm", proc_root=str(root)), argv

    @pytest.mark.parametrize(
        "argv",
        [
            ["python3", "-X", "utf8", "-m", "sglang.launch_server", "--tp", "2"],
            ["python3", "-u", "-m", "sglang.launch_server"],
            ["python3", "-um", "sglang.launch_server"],
        ],
    )
    def test_dash_m_after_interpreter_flags_matches(self, agent, tmp_path, argv):
        root = _fake_proc(tmp_path, {101: argv})
        assert agent.process_running("sglang.launch_server", proc_root=str(root)), argv

    def test_kernel_name_comm_matches_like_pgrep_x(self, agent, tmp_path):
        # argv rewritten by the engine, but the kernel name is the program.
        root = _fake_proc(tmp_path, {101: ["worker-main", "--lanes", "4"]}, comms={101: "tf-dsv41-lanes"})
        assert agent.process_running("tf-dsv41-lanes", proc_root=str(root))

    def test_long_name_never_matches_by_truncated_comm(self, agent, tmp_path):
        # comm holds 15 chars; pgrep -x never matches a longer name by it.
        root = _fake_proc(tmp_path, {101: ["other"]}, comms={101: "tensorfold-nati"})
        assert not agent.process_running("tensorfold-native-x", proc_root=str(root))

    @pytest.mark.parametrize(
        "argv",
        [
            ["python3", "-c", "import vllm", "vllm"],
            ["python3", "-c", "vllm"],
            ["python3", "-uc", "vllm"],
            ["python3", "-X", "vllm", "/opt/tools/check.py"],
            ["python3", "-W", "vllm", "/opt/tools/check.py"],
            ["uv", "run", "pytest", "vllm"],
            ["uv", "pip", "install", "vllm"],
            ["uv", "run", "--with", "vllm", "python3", "check.py"],
            ["bash", "-lc", "vllm serve /model"],
            ["sudo vllm serve"],
        ],
    )
    def test_name_in_arguments_still_never_matches(self, agent, tmp_path, argv):
        root = _fake_proc(tmp_path, {101: argv}, comms={101: os.path.basename(argv[0].split()[0])})
        assert not agent.process_running("vllm", proc_root=str(root)), argv

    def test_proc_is_read_once_per_round(self, agent, tmp_path, monkeypatch):
        """collect_anchors snapshots /proc once, not once per process anchor."""
        root = _fake_proc(tmp_path / "proc", {7: ["/opt/llama/llama-server"], 8: ["/opt/x/engine-b"]})
        real_listdir = os.listdir
        calls = {"n": 0}

        def counting_listdir(path):
            if str(path) == str(root):
                calls["n"] += 1
            return real_listdir(path)

        monkeypatch.setattr(agent.os, "listdir", counting_listdir)
        out = agent.collect_anchors(
            [
                {"kind": "process", "name": "llama-server"},
                {"kind": "process", "name": "engine-b"},
                {"kind": "process", "name": "engine-c"},
            ],
            socket_path="/definitely/not/here.sock",
            proc_root=str(root),
        )
        assert [a["state"] for a in out["anchors"]] == ["running", "running", "stopped"]
        assert calls["n"] == 1


class TestGenericNamesRejected:
    """A name that is always running on any box (shell, interpreter, daemon)
    would read "running" forever — a false "running" (critic finding 3)."""

    @pytest.mark.parametrize(
        "name", ["bash", "sh", "python", "python3", "node", "docker", "dockerd", "containerd", "sshd", "systemd"]
    )
    def test_denylisted(self, agent, name):
        assert not agent.valid_anchor_name(name)
        watch, errors = agent.parse_watch({"watch": [{"kind": "process", "name": name}]})
        assert watch == [] and errors

    @pytest.mark.parametrize("name", ["python3.12", "Bash", "DOCKERD"])
    def test_denylist_is_case_and_version_aware(self, agent, name):
        assert not agent.valid_anchor_name(name)

    def test_engine_names_still_pass(self, agent):
        for name in ("vllm", "llama-server", "sglang.launch_server", "glm53-flash-tf", "python-engine"):
            assert agent.valid_anchor_name(name), name


# ── collect_anchors: the allowlist decides what is reported ──────────────────


class TestCollectAnchors:
    def test_only_listed_containers_are_reported(self, agent, fake_docker, tmp_path):
        fd = fake_docker([_container("glm-head"), _container("secret-db"), _container("glm-headless")])
        out = agent.collect_anchors(
            [{"kind": "container", "name": "glm-head"}],
            socket_path=fd.path,
            proc_root=str(tmp_path),
        )
        assert out["docker"] == "ok"
        assert out["anchors"] == [{"kind": "container", "name": "glm-head", "state": "running"}]
        dumped = json.dumps(out)
        assert "secret-db" not in dumped
        assert "glm-headless" not in dumped

    def test_listed_but_absent_container_is_stopped(self, agent, fake_docker, tmp_path):
        fd = fake_docker([_container("glm-headless")])  # superset of the name — must not count
        out = agent.collect_anchors(
            [{"kind": "container", "name": "glm-head"}], socket_path=fd.path, proc_root=str(tmp_path)
        )
        assert out["anchors"] == [{"kind": "container", "name": "glm-head", "state": "stopped"}]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
    def test_no_docker_access_container_unknown_process_still_read(self, agent, fake_docker, tmp_path):
        fd = fake_docker([_container("glm-head")])
        os.chmod(fd.path, 0)
        root = _fake_proc(tmp_path / "proc", {7: ["/opt/llama/llama-server"]})
        out = agent.collect_anchors(
            [{"kind": "container", "name": "glm-head"}, {"kind": "process", "name": "llama-server"}],
            socket_path=fd.path,
            proc_root=str(root),
        )
        assert out["docker"] == "no_access"
        assert out["anchors"] == [
            {"kind": "container", "name": "glm-head", "state": "unknown", "reason": "docker_no_access"},
            {"kind": "process", "name": "llama-server", "state": "running"},
        ]

    def test_docker_missing_container_unknown(self, agent, tmp_path):
        out = agent.collect_anchors(
            [{"kind": "container", "name": "glm-head"}],
            socket_path=str(tmp_path / "nope.sock"),
            proc_root=str(tmp_path),
        )
        assert out["docker"] == "unavailable"
        assert out["anchors"][0]["state"] == "unknown"
        assert out["anchors"][0]["reason"] == "docker_unavailable"

    def test_process_only_never_touches_docker(self, agent, tmp_path):
        root = _fake_proc(tmp_path / "proc", {7: ["/opt/llama/llama-server"]})
        out = agent.collect_anchors(
            [{"kind": "process", "name": "llama-server"}],
            socket_path="/definitely/not/here.sock",
            proc_root=str(root),
        )
        assert "docker" not in out
        assert out["anchors"] == [{"kind": "process", "name": "llama-server", "state": "running"}]

    def test_empty_watch_reports_nothing(self, agent):
        assert agent.collect_anchors([]) == {}


# ── run_loop: watch from the answer, anchors in the next heartbeat ───────────


class TestRunLoopAnchors:
    def _drive(self, agent, monkeypatch, responses, anchors_fn):
        monkeypatch.setattr(agent, "_run_nvidia_smi", lambda: "")
        monkeypatch.setattr(agent, "read_oom_guard", lambda: "missing")
        monkeypatch.setattr(agent, "collect_telemetry", lambda **_k: {"ts": "now"})
        monkeypatch.setattr(agent, "scan_model_inventory", lambda paths: [])
        monkeypatch.setattr(agent, "collect_device_state", lambda **_k: {"gpu_mode": "eco"})
        monkeypatch.setattr(agent, "collect_anchors", anchors_fn)
        cmds: list = []
        monkeypatch.setattr(agent, "_run_set_cmd", lambda argv: cmds.append(argv))
        seq = list(responses)
        sends: list = []

        def fake_send(mc_url, token, telemetry, inventory=None, device_state=None):
            sends.append(device_state)
            return seq.pop(0) if seq else {}

        monkeypatch.setattr(agent, "send_heartbeat", fake_send)
        n = {"i": 0}

        def fake_sleep(_s):
            n["i"] += 1
            if n["i"] >= len(responses):
                raise KeyboardInterrupt()

        monkeypatch.setattr(agent.time, "sleep", fake_sleep)
        agent.run_loop("http://mc.test", "tok")
        return sends, cmds

    def test_watch_is_applied_in_the_next_heartbeat(self, agent, monkeypatch):
        seen: list = []

        def fake_anchors(watch, **_k):
            seen.append(list(watch))
            return {"docker": "ok", "anchors": [{"kind": "container", "name": "glm-head", "state": "running"}]} if watch else {}

        watch = [{"kind": "container", "name": "glm-head"}]
        sends, cmds = self._drive(
            agent, monkeypatch,
            [{"desired_state": {"watch": watch}}, {"desired_state": {"watch": watch}}],
            fake_anchors,
        )
        assert sends[0] == {"gpu_mode": "eco"}  # first round: nothing known yet
        assert sends[1]["anchors"][0]["state"] == "running"
        assert sends[1]["docker"] == "ok"
        assert cmds == []  # watch is read-only: never a set command

    def test_watch_removed_stops_reporting(self, agent, monkeypatch):
        def fake_anchors(watch, **_k):
            return {"anchors": [{"kind": "process", "name": "p", "state": "running"}]} if watch else {}

        sends, _ = self._drive(
            agent, monkeypatch,
            [{"desired_state": {"watch": [{"kind": "process", "name": "p"}]}}, {"ok": True}, {"ok": True}],
            fake_anchors,
        )
        assert "anchors" in sends[1]
        assert "anchors" not in sends[2]

    def test_broken_anchor_read_never_kills_the_heartbeat(self, agent, monkeypatch):
        def boom(watch, **_k):
            if watch:
                raise RuntimeError("kaputt")
            return {}

        sends, _ = self._drive(
            agent, monkeypatch,
            [{"desired_state": {"watch": [{"kind": "process", "name": "p"}]}}, {"ok": True}],
            boom,
        )
        assert len(sends) == 2
        assert sends[1] == {"gpu_mode": "eco"}


def test_plan_ignores_watch(agent):
    """watch is not a control field: it must never turn into a command."""
    plans, errors = agent.plan_desired_state({"watch": [{"kind": "process", "name": "p"}]}, {})
    assert plans == []
    assert errors == []
