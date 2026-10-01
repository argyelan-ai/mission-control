"""The detach wrapper returns at once even when the launch command runs in the
foreground (live 01.10.2026: a recipe whose start.sh stays in the foreground
held the SSH call until it timed out — MC reported an empty "SSH-Fehler").

Runs the real wrapper through bash with captured stdout, the way an SSH exec
channel sees it: the call must end long before the launch command does.
"""

import shutil
import subprocess
import time

import pytest

from app.services.runtime_manager import detached_launch


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
@pytest.mark.parametrize("launch", [
    "exec sleep 3 < /dev/null",                              # foreground start.sh
    "nohup sleep 3 > /dev/null 2>&1 < /dev/null &",          # self-backgrounding template
])
def test_detached_launch_returns_before_the_launch_command_ends(tmp_path, monkeypatch, launch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd = detached_launch(launch, "~/.cache/mc/runtime-launch-test.log")
    started = time.monotonic()
    proc = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.5, f"wrapper held stdout for {elapsed:.1f}s"


def test_detached_launch_quotes_the_command_and_writes_the_log():
    cmd = detached_launch("cd ~/x && ./start.sh 'a b'", "~/.cache/mc/runtime-launch-r1.log")
    assert "nohup bash -lc 'cd ~/x && ./start.sh '\"'\"'a b'\"'\"''" in cmd
    assert "> ~/.cache/mc/runtime-launch-r1.log 2>&1 < /dev/null & }" in cmd
