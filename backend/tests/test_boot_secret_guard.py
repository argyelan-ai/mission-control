"""Boot guard: refuse to start in production with placeholder secrets.

A bare `docker compose up` without ./setup.sh (e.g. the Portainer stackfile
path) used to boot with JWT_SECRET_KEY="change-me-in-production" — anyone
could forge admin tokens. The guard fails fast with a clear remediation
message instead of running silently insecure.
"""

import pytest

from app.config import Settings, validate_boot_secrets

STRONG_JWT = "0f3a" * 16  # looks like `openssl rand -hex 32`
VALID_FERNET = "x" * 43 + "="  # any non-empty passphrase is accepted (0.1.1 derives)


def _settings(**overrides) -> Settings:
    defaults = dict(
        environment="production",
        jwt_secret_key=STRONG_JWT,
        secrets_encryption_key=VALID_FERNET,
    )
    defaults.update(overrides)
    return Settings(_env_file=None, **defaults)


def test_production_with_default_jwt_secret_refuses_boot():
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        validate_boot_secrets(_settings(jwt_secret_key="change-me-in-production"))


def test_production_with_env_example_jwt_placeholder_refuses_boot():
    # Someone copied .env.example to .env by hand without filling it in.
    with pytest.raises(RuntimeError, match="JWT_SECRET_KEY"):
        validate_boot_secrets(
            _settings(jwt_secret_key="change_me_generate_with_openssl_rand_hex_32")
        )


def test_production_with_empty_encryption_key_refuses_boot():
    with pytest.raises(RuntimeError, match="SECRETS_ENCRYPTION_KEY"):
        validate_boot_secrets(_settings(secrets_encryption_key=""))


def test_guard_error_points_to_setup_sh():
    with pytest.raises(RuntimeError, match="setup.sh"):
        validate_boot_secrets(_settings(jwt_secret_key="change-me-in-production"))


def test_production_with_real_secrets_boots():
    validate_boot_secrets(_settings())  # must not raise


def test_development_with_defaults_boots():
    # Local dev / pytest must keep working without generated secrets.
    validate_boot_secrets(
        _settings(
            environment="development",
            jwt_secret_key="change-me-in-production",
            secrets_encryption_key="",
        )
    )


def test_lifespan_wires_the_guard():
    # The guard only protects users if startup actually calls it.
    import inspect

    import app.main as main

    assert "validate_boot_secrets" in inspect.getsource(main.lifespan)
