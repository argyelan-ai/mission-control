"""omp-Agenten nutzen den gemeinsamen Agenten-Browser (cdp-browser).

omps eigener Chrome-Start scheitert auf arm64 ("Chrome for Testing does not
provide linux/arm64"). Darum haengt omp sich ueber ein lokales socat-Relay
(docker/omp-bridge/cdp-relay.sh, 127.0.0.1:9222) an den gemeinsamen Chromium —
Chromium lehnt Dienstnamen im Host-Header ab, eine IP (127.0.0.1) nimmt es an.

Geprueft wird hier:
  * render-omp-config.sh setzt ``browser.cdpUrl`` auf das Relay, wenn der
    Dienstname aufloest — und setzt es zurueck, wenn nicht (fremde Installation
    ohne Browser-Profil) oder wenn das Relay abgeschaltet ist (``off``).
  * cdp-relay.sh startet nichts, wenn abgeschaltet, und bricht nie ab.
  * Entrypoint + Dockerfile verdrahten beides wirklich (socat im Image,
    Skript kopiert, Aufruf beim Start und im Watchdog).

``omp`` gibt es auf dem Test-Rechner nicht: ein Fake-``omp`` auf PATH
protokolliert jeden Aufruf in eine Datei.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
OMP_DIR = REPO_ROOT / "docker" / "omp-bridge"
RENDER_SCRIPT = OMP_DIR / "render-omp-config.sh"
RELAY_SCRIPT = OMP_DIR / "cdp-relay.sh"


def _fake_bin(tmp_path: Path, name: str, log: Path) -> Path:
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    exe = bindir / name
    exe.write_text(f'#!/usr/bin/env bash\necho "{name} $*" >> "{log}"\nexit 0\n')
    exe.chmod(0o755)
    return bindir


def _scrubbed_env(tmp_path: Path) -> dict:
    # Nie Laufzeit-Variablen erben (Vorfall 07.09.: in einem omp-Container zeigt
    # OMP_ENV_FILE auf die ECHTE omp.env des Agenten).
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR"))
    }
    env.update(
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
        OMP_ENV_FILE=str(home / ".omp" / "omp.env"),
        OPENAI_BASE_URL="http://192.0.2.20:8000/v1",
        OPENAI_MODEL="org/test-model",
    )
    return env


def _render(tmp_path: Path, **extra: str) -> tuple[list[str], str]:
    log = tmp_path / "omp-calls.log"
    bindir = _fake_bin(tmp_path, "omp", log)
    env = _scrubbed_env(tmp_path)
    env["PATH"] = f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}"
    env.update(extra)
    result = subprocess.run(
        [str(RENDER_SCRIPT), "--no-bootstrap"],
        env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    calls = log.read_text().splitlines() if log.exists() else []
    return [c for c in calls if "browser" in c], result.stdout


def test_default_target_is_the_gateway_port_when_unset(tmp_path):
    """render-omp-config.sh defaults `OMP_BROWSER_CDP_TARGET` to
    `cdp-browser:9300` (cdp-gateway, bauplan.md PR B1) when the env var is
    not set at all — every other test here sets it explicitly, so nothing
    guarded this default. `cdp-browser` does not resolve on the test runner
    (no docker network), so the script falls into its own "unresolvable"
    branch — which is exactly what makes the DEFAULT visible: the branch's
    message echoes back the target string it computed internally. Reverting
    the default to the pre-gateway `:9223` keeps this test green (same
    message shape), so the assertion pins the exact default value, not just
    that *some* fallback message appeared."""
    calls, out = _render(tmp_path)
    assert calls == ["omp config reset browser.cdpUrl"]
    assert "kein gemeinsamer Agenten-Browser erreichbar (cdp-browser:9300)" in out


def test_resolvable_target_points_omp_at_the_local_relay(tmp_path):
    # `localhost` loest ueberall auf — steht hier fuer den Compose-Dienstnamen.
    calls, out = _render(tmp_path, OMP_BROWSER_CDP_TARGET="localhost:9223")
    assert calls == ["omp config set browser.cdpUrl http://127.0.0.1:9222"]
    assert "gemeinsamer Agenten-Browser" in out


def test_relay_port_is_configurable(tmp_path):
    calls, _ = _render(
        tmp_path, OMP_BROWSER_CDP_TARGET="localhost:9223", OMP_BROWSER_CDP_PORT="9444",
    )
    assert calls == ["omp config set browser.cdpUrl http://127.0.0.1:9444"]


def test_unresolvable_target_resets_instead_of_pointing_at_a_dead_relay(tmp_path):
    """Sabotage-Probe: ohne laufenden cdp-browser (Browser-Profil aus) darf omp
    NICHT auf das Relay zeigen — sonst scheitert jeder Browser-Aufruf, auch auf
    Maschinen, wo omps eigener Chrome funktionieren wuerde. `.invalid` loest
    per RFC 6761 nie auf."""
    calls, out = _render(tmp_path, OMP_BROWSER_CDP_TARGET="cdp-browser.invalid:9223")
    assert calls == ["omp config reset browser.cdpUrl"]
    assert "kein gemeinsamer Agenten-Browser" in out


@pytest.mark.parametrize("value", ["off", ""])
def test_switched_off_resets(tmp_path, value):
    calls, _ = _render(tmp_path, OMP_BROWSER_CDP_TARGET=value)
    assert calls == ["omp config reset browser.cdpUrl"]


def test_relay_off_starts_nothing(tmp_path):
    log = tmp_path / "socat.log"
    bindir = _fake_bin(tmp_path, "socat", log)
    env = _scrubbed_env(tmp_path)
    env["PATH"] = f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["OMP_BROWSER_CDP_TARGET"] = "off"
    r = subprocess.run([str(RELAY_SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0
    assert not log.exists()


def test_relay_starts_socat_on_loopback_with_fork(tmp_path):
    log = tmp_path / "socat.log"
    bindir = _fake_bin(tmp_path, "socat", log)
    env = _scrubbed_env(tmp_path)
    env["PATH"] = f"{bindir}:{env.get('PATH', '/usr/bin:/bin')}"
    env["OMP_BROWSER_CDP_TARGET"] = "cdp-browser:9223"
    env["OMP_BROWSER_CDP_PORT"] = "9555"  # nichts auf dem Testrechner kollidiert
    r = subprocess.run([str(RELAY_SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    # der Fake laeuft im Hintergrund — kurz auf seinen Eintrag warten
    for _ in range(50):
        if log.exists() and log.read_text().strip():
            break
        subprocess.run(["sleep", "0.1"])
    line = log.read_text().strip()
    # bind=127.0.0.1: das Relay ist nur im Container erreichbar, nie im Netz.
    # fork: jede Verbindung loest den Dienstnamen frisch auf.
    assert line == "socat TCP-LISTEN:9555,bind=127.0.0.1,fork,reuseaddr TCP:cdp-browser:9223"


def test_relay_never_fails_without_socat(tmp_path):
    env = _scrubbed_env(tmp_path)
    env["PATH"] = "/usr/bin:/bin"  # kein socat (macOS/CI ohne Paket)
    env["OMP_BROWSER_CDP_TARGET"] = "cdp-browser:9223"
    env["OMP_BROWSER_CDP_PORT"] = "9556"
    r = subprocess.run([str(RELAY_SCRIPT)], env=env, capture_output=True, text=True, timeout=10)
    if "socat" in subprocess.run(["bash", "-c", "command -v socat || true"],
                                 env=env, capture_output=True, text=True).stdout:
        pytest.skip("socat liegt systemweit unter /usr/bin — Fall nicht nachstellbar")
    assert r.returncode == 0
    assert "socat fehlt" in r.stderr


def test_image_and_entrypoint_wire_the_relay():
    dockerfile = (OMP_DIR / "Dockerfile").read_text()
    entrypoint = (OMP_DIR / "entrypoint.sh").read_text()
    assert "        socat \\" in dockerfile
    assert "COPY cdp-relay.sh         /opt/omp-bridge/cdp-relay.sh" in dockerfile
    assert "/opt/omp-bridge/cdp-relay.sh \\" in dockerfile  # chmod +x
    # einmal beim Start (vor den Fenstern), einmal im Watchdog
    assert entrypoint.count("/opt/omp-bridge/cdp-relay.sh") == 2
    assert entrypoint.index("/opt/omp-bridge/cdp-relay.sh || true") < entrypoint.index("\nstart_native\n")
    assert os.access(RELAY_SCRIPT, os.X_OK)
