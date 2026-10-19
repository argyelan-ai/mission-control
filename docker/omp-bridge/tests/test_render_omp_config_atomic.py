#!/usr/bin/env python3
"""render-omp-config.sh writes omp.env and models.yml atomically (fix 19.10.2026).

``cat > omp.env`` truncates the file first and fills it afterwards. A reader in
that gap (``acp_model.read_env_file`` on every ACP session, ``launch-omp.sh``
sourcing the file) sees a half file — and a missing ``OMP_MODEL_SELECTOR``
silently falls back to the stale value of the process env (the incident of
19.10.2026). ``chmod 600`` also came only AFTER the write, so a fresh file
with the API key lay there world-readable for a moment.

Now: a temp file in the SAME directory, ``chmod`` before ``mv``, one rename.
If the target is bind-mounted as a single file, ``mv`` would break the mount
— then the script falls back to writing in place and says so.

No docker, no network: fake ``omp``/``tmux`` shadow the real ones, a fake
``mv`` records the mode of the file it is about to move.

  pytest docker/omp-bridge/tests/test_render_omp_config_atomic.py -v
"""
from __future__ import annotations

import os
import stat
import subprocess
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "render-omp-config.sh"


def _bindir(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    if bindir.exists():
        return bindir
    bindir.mkdir()
    for name in ("omp", "tmux"):
        fake = bindir / name
        fake.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake.chmod(0o755)
    real_mv = subprocess.run(["sh", "-c", "command -v mv"], capture_output=True,
                             text=True).stdout.strip() or "/bin/mv"
    log = tmp_path / "mv.log"
    mv = bindir / "mv"
    # Records "<octal mode of the source> <target>" before the real move.
    mv.write_text(
        "#!/bin/sh\n"
        "python3 -c 'import os,sys; a=[x for x in sys.argv[1:] if not x.startswith(\"-\")]; "
        "print(oct(os.stat(a[-2]).st_mode & 0o777), a[-1])' \"$@\" "
        f'>> "{log}" 2>/dev/null || true\n'
        f'exec {real_mv} "$@"\n',
        encoding="utf-8",
    )
    mv.chmod(0o755)
    return bindir


def _render(tmp_path: Path, *, model: str = "Model-A", extra: dict | None = None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR", "AGENT_NAME"))
    }
    env.update(
        PATH=f"{_bindir(tmp_path)}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
        OMP_ENV_FILE=str(home / ".omp" / "omp.env"),
        OPENAI_BASE_URL="http://192.0.2.20:8000/v1",
        OPENAI_MODEL=model,
        OPENAI_API_KEY="test-key-not-secret",
        OMP_BROWSER_CDP_TARGET="off",
        OMP_RENDER_MOUNTINFO=str(tmp_path / "no-mountinfo"),
    )
    env.update(extra or {})
    result = subprocess.run(
        ["bash", str(SCRIPT), "--no-bootstrap"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    return result, home / ".omp" / "omp.env", (
        home / ".omp" / "profiles" / "mc-agent" / "agent" / "models.yml"
    )


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def test_both_files_are_written_by_rename_with_mode_600_before_the_move(tmp_path):
    _, env_file, models = _render(tmp_path)
    log = (tmp_path / "mv.log").read_text(encoding="utf-8").splitlines()
    moved = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in log}
    # Both targets arrive by rename, and the temp file already had 0600.
    assert moved.get(str(env_file)) == "0o600", log
    assert moved.get(str(models)) == "0o600", log
    assert _mode(env_file) == 0o600
    assert _mode(models) == 0o600
    assert "OMP_MODEL_SELECTOR=mc-openai/Model-A" in env_file.read_text(encoding="utf-8")


def test_a_rerender_replaces_the_inode_and_leaves_no_temp_files(tmp_path):
    _, env_file, models = _render(tmp_path, model="Model-A")
    first = (env_file.stat().st_ino, models.stat().st_ino)
    _render(tmp_path, model="Model-B")
    assert (env_file.stat().st_ino, models.stat().st_ino) != first
    assert "OPENAI_MODEL=Model-B" in env_file.read_text(encoding="utf-8")
    leftovers = [p.name for p in env_file.parent.iterdir() if p.name.startswith("omp.env.")]
    leftovers += [p.name for p in models.parent.iterdir() if p.name.startswith("models.yml.")]
    assert leftovers == []


def test_a_concurrent_reader_never_sees_a_half_file(tmp_path):
    """The incident itself: a reader between truncate and write got no
    OMP_MODEL_SELECTOR. With a rename every read is a whole file."""
    _, env_file, _ = _render(tmp_path, model="Model-A")
    bad: list[str] = []
    stop = threading.Event()

    def reader() -> None:
        while not stop.is_set():
            try:
                text = env_file.read_text(encoding="utf-8")
            except FileNotFoundError:
                bad.append("<missing>")
                continue
            if "OMP_MODEL_SELECTOR=mc-openai/Model-" not in text:
                bad.append(text[:40])

    t = threading.Thread(target=reader)
    t.start()
    try:
        for i in range(12):
            _render(tmp_path, model=f"Model-{i % 2}")
    finally:
        stop.set()
        t.join()
    assert bad == []


def test_a_single_file_bind_mount_is_written_in_place_with_a_warning(tmp_path):
    """mv over a single-file bind mount would detach it. The script detects
    the mount point (mountinfo) and keeps the inode."""
    _, env_file, _ = _render(tmp_path, model="Model-A")
    inode = env_file.stat().st_ino
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        f"36 35 0:30 / {env_file} rw,relatime - ext4 /dev/sda1 rw\n", encoding="utf-8"
    )
    result, env_file, _ = _render(
        tmp_path, model="Model-B", extra={"OMP_RENDER_MOUNTINFO": str(mountinfo)}
    )
    assert env_file.stat().st_ino == inode
    assert "OPENAI_MODEL=Model-B" in env_file.read_text(encoding="utf-8")
    assert "gemountet" in result.stderr
    assert _mode(env_file) == 0o600
