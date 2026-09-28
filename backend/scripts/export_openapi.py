"""Write the public API schema to ``backend/openapi.json`` (Roter Faden E3a).

The coherence tool ``kz`` reads the endpoints for the product map from this
file (adapter ``fastapi``). It is generated, committed and kept fresh by
``tests/test_openapi_export.py``.

    cd backend && python scripts/export_openapi.py          # write
    cd backend && python scripts/export_openapi.py --check  # exit 1 when stale

No DB or Redis is touched: importing ``app.main`` only builds the app, the
lifespan never runs. Output is deterministic: sorted keys, the version is the
code default (not a ``.env`` override), and routes of private overlay
verticals (gitignored, see ``.gitignore``) are left out so a local checkout
with the overlay and public CI produce the same file.

Only the route surface is kept (path, method, operationId, summary, tags) -
no component schemas, parameters or descriptions. That is all ``kz`` reads,
it keeps the diff small, it cannot leak a settings default, and it does not
change when CI installs a newer FastAPI/Pydantic than the lock file.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
OUTPUT = BACKEND / "openapi.json"

# Mirrors the allowlist in .gitignore (`backend/app/verticals/*` is ignored,
# only these are un-ignored). tests/test_openapi_export.py keeps both in sync.
PUBLIC_VERTICALS = frozenset({"bench_studio"})


def _is_private_route(route) -> bool:
    module = getattr(getattr(route, "endpoint", None), "__module__", "") or ""
    parts = module.split(".")
    return len(parts) > 2 and parts[:2] == ["app", "verticals"] and parts[2] not in PUBLIC_VERTICALS


OPERATION_KEYS = ("operationId", "summary", "tags")


def build_schema() -> dict:
    if str(BACKEND) not in sys.path:
        sys.path.insert(0, str(BACKEND))
    from fastapi.openapi.utils import get_openapi

    from app.config import Settings
    from app.main import app

    full = get_openapi(
        title=app.title,
        version=Settings.model_fields["app_version"].default,
        description=app.description,
        routes=[r for r in app.routes if not _is_private_route(r)],
    )
    paths = {
        path: {method: {k: op[k] for k in OPERATION_KEYS if k in op} for method, op in ops.items()}
        for path, ops in full["paths"].items()
    }
    return {"openapi": full["openapi"], "info": full["info"], "paths": paths}


def render(schema: dict) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="exit 1 when openapi.json is stale")
    args = parser.parse_args(argv)
    text = render(build_schema())
    if args.check:
        current = OUTPUT.read_text() if OUTPUT.exists() else ""
        if current != text:
            print("backend/openapi.json is stale - run: cd backend && python scripts/export_openapi.py")
            return 1
        return 0
    OUTPUT.write_text(text)
    print(f"wrote {OUTPUT.relative_to(BACKEND.parent)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
