"""Runtime Watcher (ADR-053) — settings, keys, drift detection, propagation gates."""
import pytest

from app.config import settings
from app.redis_client import RedisKeys


def test_watcher_settings_and_keys_exist():
    assert settings.runtime_watcher_enabled is True
    assert settings.runtime_watcher_interval == 90
    assert RedisKeys.runtime_watcher_lock() == "mc:runtime-watcher:lock"
    assert RedisKeys.runtime_live("qwen-general") == "mc:runtime-live:qwen-general"
    assert RedisKeys.runtime_drift_candidate("x") == "mc:runtime-drift:x"
    assert RedisKeys.agent_switch_progress("abc") == "mc:agent:abc:runtime-switch-progress"
    assert RedisKeys.agent_model_sync_fails("abc") == "mc:agent:abc:model-sync-fails"
