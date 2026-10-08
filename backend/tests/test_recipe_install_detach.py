"""The install starts detached and the SSH call returns at once (live
08.10.2026: ``a && b && nohup … &`` backgrounded the whole ``&&`` list as a
subshell that kept the SSH session's stdout open, the call ran into its
timeout, MC marked the install "failed" with an empty message — while the
install itself kept running on the box).

Runs the real command through bash with captured stdout, the way an SSH exec
channel sees it.
"""

import shutil
import subprocess
import time

import pytest

from app.services.recipe_install import install_detach_command


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_detach_returns_before_the_install_ends_and_prints_the_pid(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd = install_detach_command("sleep 3; echo done", "~/.cache/mc/install-test.log")
    started = time.monotonic()
    proc = subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=10)
    elapsed = time.monotonic() - started
    assert proc.returncode == 0, proc.stderr
    assert elapsed < 1.5, f"detach held stdout for {elapsed:.1f}s"
    assert proc.stdout.decode().strip().splitlines()[-1].isdigit()


@pytest.mark.skipif(shutil.which("bash") is None, reason="needs bash")
def test_install_detach_still_writes_the_log_and_the_exit_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    cmd = install_detach_command("echo hello", "~/.cache/mc/install-test.log")
    subprocess.run(["bash", "-c", cmd], stdout=subprocess.PIPE, timeout=10)
    log = tmp_path / ".cache" / "mc" / "install-test.log"
    for _ in range(50):
        if log.exists() and "MC_EXIT:" in log.read_text():
            break
        time.sleep(0.1)
    text = log.read_text()
    assert "hello" in text
    assert "MC_EXIT:0" in text


def test_a_failed_install_never_reports_an_empty_reason():
    from app.services.recipe_install import failure_reason

    assert failure_reason(TimeoutError()) == "TimeoutError"
    assert failure_reason(RuntimeError("boom")) == "boom"
