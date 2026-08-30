"""Pure-function tests for services/host_onboarding.py (Fleet & Rezepte v2,
Phase 2 — Auto-Onboarding): the idempotent authorized_keys writer and the
per-address rate limiter. No network, no DB — see test_host_onboarding.py
for the SSH-mocked end-to-end run and the security (password-never-
persisted) tests.
"""
import pytest

from app.services import host_onboarding as onboarding


# ── upsert_authorized_keys ───────────────────────────────────────────────────


class TestUpsertAuthorizedKeys:
    def test_empty_existing_content(self):
        result = onboarding.upsert_authorized_keys(None, "ssh-ed25519 AAAA", "gx10")
        assert result == "ssh-ed25519 AAAA mc-fleet gx10\n"

    def test_appends_without_touching_unrelated_lines(self):
        existing = "ssh-rsa BBBB someone@laptop\n"
        result = onboarding.upsert_authorized_keys(existing, "ssh-ed25519 AAAA", "gx10")
        lines = result.splitlines()
        assert "ssh-rsa BBBB someone@laptop" in lines
        assert "ssh-ed25519 AAAA mc-fleet gx10" in lines
        assert len(lines) == 2

    def test_reonboard_replaces_own_previous_line_not_duplicates(self):
        existing = "ssh-ed25519 OLDKEY mc-fleet gx10\n"
        result = onboarding.upsert_authorized_keys(existing, "ssh-ed25519 NEWKEY", "gx10")
        lines = result.splitlines()
        assert lines == ["ssh-ed25519 NEWKEY mc-fleet gx10"]
        assert "OLDKEY" not in result

    def test_only_touches_the_matching_hosts_marker(self):
        existing = "ssh-ed25519 KEYA mc-fleet host-a\nssh-ed25519 KEYB mc-fleet host-b\n"
        result = onboarding.upsert_authorized_keys(existing, "ssh-ed25519 KEYA-NEW", "host-a")
        lines = result.splitlines()
        assert "ssh-ed25519 KEYB mc-fleet host-b" in lines
        assert "ssh-ed25519 KEYA-NEW mc-fleet host-a" in lines
        assert "ssh-ed25519 KEYA mc-fleet host-a" not in lines
        assert len(lines) == 2

    def test_result_is_newline_terminated(self):
        result = onboarding.upsert_authorized_keys("", "ssh-ed25519 AAAA", "gx10")
        assert result.endswith("\n")
        assert not result.endswith("\n\n")

    def test_ignores_blank_lines_in_existing_content(self):
        existing = "\n\nssh-rsa BBBB x\n\n"
        result = onboarding.upsert_authorized_keys(existing, "ssh-ed25519 AAAA", "gx10")
        assert result.count("\n\n") == 0  # no blank lines carried through


# ── rate limiting ────────────────────────────────────────────────────────────


class TestRateLimit:
    def setup_method(self):
        onboarding._auth_failures.clear()

    def test_passes_under_threshold(self):
        onboarding.check_rate_limit("192.0.2.10")  # no failures yet — must not raise
        onboarding._record_auth_failure("192.0.2.10")
        onboarding._record_auth_failure("192.0.2.10")
        onboarding.check_rate_limit("192.0.2.10")  # 2 failures — still under the cap of 3

    def test_raises_after_max_failures(self):
        for _ in range(3):
            onboarding._record_auth_failure("192.0.2.10")
        with pytest.raises(onboarding.RateLimitExceeded):
            onboarding.check_rate_limit("192.0.2.10")

    def test_addresses_are_independent(self):
        for _ in range(3):
            onboarding._record_auth_failure("192.0.2.10")
        onboarding.check_rate_limit("192.0.2.20")  # different address — unaffected

    def test_old_failures_outside_window_do_not_count(self, monkeypatch):
        import time as time_module

        real_time = time_module.time
        monkeypatch.setattr(onboarding.time, "time", lambda: real_time() - 700)  # 11+ min ago
        for _ in range(3):
            onboarding._record_auth_failure("192.0.2.10")

        monkeypatch.setattr(onboarding.time, "time", real_time)  # back to "now"
        onboarding.check_rate_limit("192.0.2.10")  # those 3 failures have aged out

    def test_clear_resets(self):
        for _ in range(3):
            onboarding._record_auth_failure("192.0.2.10")
        onboarding._clear_auth_failures("192.0.2.10")
        onboarding.check_rate_limit("192.0.2.10")  # no longer locked out
