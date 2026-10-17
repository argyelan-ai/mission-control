export APP_MISSION_CONTROL_DB_PASSWORD="$(derive_entropy "mission-control-db-password")"
export APP_MISSION_CONTROL_REDIS_PASSWORD="$(derive_entropy "mission-control-redis-password")"
export APP_MISSION_CONTROL_JWT_SECRET="$(derive_entropy "mission-control-jwt-secret")"

# The backend expects a Fernet key: 32 bytes as URL-safe base64 (44 chars).
# derive_entropy yields a hex string; its chars are all valid URL-safe
# base64 symbols, so 43 chars + '=' padding decode to exactly 32 bytes
# (~172 bits of entropy). Verified against cryptography.fernet.Fernet.
mission_control_fernet_hex="$(derive_entropy "mission-control-fernet-key")"
export APP_MISSION_CONTROL_FERNET_KEY="${mission_control_fernet_hex:0:43}="

# Bearer secret for GET /api/v1/internal/bootstrap (backend/app/config.py
# INTERNAL_BOOTSTRAP_SECRET, PR #404). Since 0.3.1 validate_boot_secrets()
# refuses to boot in production without it — any stable string works, no
# Fernet encoding needed here.
export APP_MISSION_CONTROL_BOOTSTRAP_SECRET="$(derive_entropy "mission-control-bootstrap-secret")"
