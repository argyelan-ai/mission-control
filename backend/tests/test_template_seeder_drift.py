"""Regression-Tests fuer template_seeder — verhindert Drift zwischen
Code-Defaults (DEFAULT_SCOPES) und Template-Specs (BUILTIN_TEMPLATES).

Hintergrund (2026-04-23): Nach dem Tester-credentials:read-Fix war die
Template-DB nicht in sync mit den neuen Code-Defaults — neue Tester via
Template-Instantiate haetten weiterhin die alten Scopes bekommen. Der
Seeder updated jetzt automatisch beim Backend-Startup, aber dieser Test
sichert ab dass die Spec-Definitionen nicht von DEFAULT_SCOPES driften.
"""
import pytest

from app.services.template_seeder import BUILTIN_TEMPLATES
from app.scopes import DEFAULT_SCOPES, AgentRole


def test_all_builtin_templates_use_default_scopes():
    """Jedes Builtin-Template muss seine Scopes aus DEFAULT_SCOPES beziehen.

    Wenn jemand future inline ['tasks:read', ...] hardcodet statt
    get_default_scopes(role) zu rufen, drifted die DB nach dem naechsten
    Seeder-Run nicht mehr automatisch mit Code-Aenderungen mit.
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
    """Sanity: credentials:read muss in genau den Rollen sein die mit
    Vault-Credentials arbeiten (Login-Tests, Deploy-Secrets etc.).
    Side-Issue #2 (2026-04-23): Tester wurde nachgezogen weil mc verify
    --login-as Vault-Resolve braucht."""
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
