"""Tests for the portability cleanup — no machine-binding in product code."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.config import Settings, phone_test_url, settings


def test_home_host_default_is_real_home():
    # the portable default must be the running user's home, NOT a hardcoded author-specific path
    assert Settings.model_fields["home_host"].default == str(Path.home())


def test_no_hardcoded_tailscale_ip_in_app():
    app_dir = Path(__file__).resolve().parent.parent / "app"
    offenders = [str(p) for p in app_dir.rglob("*.py") if "100.100.100.100" in p.read_text(encoding="utf-8", errors="ignore")]
    assert offenders == [], f"hardcoded example Tailscale IP still present in: {offenders}"


def test_phone_test_url_uses_public_host(monkeypatch):
    monkeypatch.setattr(settings, "public_host", "10.0.0.5")
    assert phone_test_url() == "http://10.0.0.5"


def test_phone_test_url_falls_back_to_base_url(monkeypatch):
    monkeypatch.setattr(settings, "public_host", "")
    monkeypatch.setattr(settings, "mc_base_url", "http://localhost")
    assert phone_test_url() == "http://localhost"


def test_deliverable_paths_use_settings_home_host(monkeypatch):
    from app.services.deliverable_paths import accepted_path_prefixes

    monkeypatch.setattr(settings, "home_host", "/custom/home")
    prefixes = accepted_path_prefixes(uuid.uuid4())
    assert any(p.startswith("/custom/home/.mc/deliverables/") for p in prefixes)
