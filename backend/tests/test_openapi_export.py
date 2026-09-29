"""backend/openapi.json stays fresh (Roter Faden E3a).

`kz` reads the endpoints for the product map from the committed file; when a
route is added or removed and the file is not regenerated, the map silently
goes stale. Fix a red test with: cd backend && python scripts/export_openapi.py
"""

import importlib.util
import re
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
REPO_ROOT = BACKEND.parent


def _load_exporter():
    spec = importlib.util.spec_from_file_location(
        "export_openapi", BACKEND / "scripts" / "export_openapi.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_committed_openapi_matches_app():
    exporter = _load_exporter()
    committed = (BACKEND / "openapi.json").read_text()
    assert committed == exporter.render(exporter.build_schema()), (
        "backend/openapi.json is stale - run: cd backend && python scripts/export_openapi.py"
    )


def test_openapi_export_is_deterministic():
    exporter = _load_exporter()
    assert exporter.render(exporter.build_schema()) == exporter.render(exporter.build_schema())


def test_public_verticals_match_gitignore_allowlist():
    # A vertical un-ignored in .gitignore is public and must be exported; any
    # other one is a private overlay and must not be.
    gitignore = (REPO_ROOT / ".gitignore").read_text()
    unignored = set(re.findall(r"^!backend/app/verticals/(\w+)/$", gitignore, re.M))
    assert unignored == set(_load_exporter().PUBLIC_VERTICALS)


def test_private_vertical_routes_are_dropped():
    exporter = _load_exporter()

    class _Route:
        def __init__(self, module):
            self.endpoint = type("E", (), {"__module__": module})

    assert exporter._is_private_route(_Route("app.verticals.some_private.router"))
    assert not exporter._is_private_route(_Route("app.verticals.bench_studio.router"))
    assert not exporter._is_private_route(_Route("app.routers.tasks"))
