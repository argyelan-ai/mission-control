"""Regression tests for template_seeder — prevents drift between
code defaults (DEFAULT_SCOPES) and template specs (BUILTIN_TEMPLATES).

Background (2026-04-23): After the tester credentials:read fix, the
template DB was out of sync with the new code defaults — new testers
created via template-instantiate would still get the old scopes. The
seeder now updates automatically on backend startup, but this test
ensures the spec definitions don't drift from DEFAULT_SCOPES.
"""
import pytest

from app.services.template_seeder import BUILTIN_TEMPLATES
from app.scopes import DEFAULT_SCOPES, AgentRole


def test_all_builtin_templates_use_default_scopes():
    """Every builtin template must source its scopes from DEFAULT_SCOPES.

    If someone hardcodes inline ['tasks:read', ...] in the future instead
    of calling get_default_scopes(role), the DB will no longer drift
    automatically with code changes after the next seeder run.
    """
    for spec in BUILTIN_TEMPLATES:
        role_name = spec["role"]
        try:
            role = AgentRole(role_name)
        except ValueError:
            pytest.fail(f"Template {spec['name']!r} hat unbekanntes role={role_name!r}")

        expected = sorted(DEFAULT_SCOPES[role])
        actual = sorted(spec["scopes"])
        assert actual == expected, (
            f"Template {spec['name']!r} (role={role_name}) hat scopes "
            f"die von DEFAULT_SCOPES abweichen.\n"
            f"  Spec   : {actual}\n"
            f"  Default: {expected}\n"
            f"Fix: scopes=get_default_scopes({role_name!r}) in template_seeder.py"
        )


def test_credentials_read_scope_in_expected_roles():
    """Sanity: credentials:read must be present in exactly the roles that
    work with vault credentials (login tests, deploy secrets, etc.).
    Side issue #2 (2026-04-23): tester was added because mc verify
    --login-as needs vault resolve."""
    roles_with_cred_read = {
        role for role, scopes in DEFAULT_SCOPES.items()
        if "credentials:read" in scopes
    }
    expected = {
        AgentRole.LEAD,
        AgentRole.DEVELOPER,
        AgentRole.DEPLOYER,
        AgentRole.TESTER,
        AgentRole.ORCHESTRATOR,
        AgentRole.RELAY,  # ALL_SCOPES — gateway/relay runtime has full access
    }
    assert roles_with_cred_read == expected, (
        f"credentials:read Verteilung weicht ab.\n"
        f"  Aktuell : {sorted(r.value for r in roles_with_cred_read)}\n"
        f"  Erwartet: {sorted(r.value for r in expected)}\n"
        f"Wenn Aenderung gewuenscht: hier den expected-Set anpassen."
    )
