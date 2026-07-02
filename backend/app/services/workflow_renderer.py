from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.secret import Secret
from app.services.encryption import safe_decrypt
from app.utils import utcnow

PLACEHOLDER_RE = re.compile(r"{{\s*([^}]+?)\s*}}")


class WorkflowRenderError(ValueError):
    pass


async def build_render_context(
    session: AsyncSession,
    *,
    workflow_snapshot: dict[str, Any],
    run: dict[str, Any],
    context: dict[str, Any],
) -> dict[str, Any]:
    secret_keys = _collect_secret_refs(
        [
            workflow_snapshot.get("steps", []),
            workflow_snapshot.get("delivery_config"),
        ]
    )

    secrets: dict[str, Any] = {}
    if secret_keys:
        result = await session.exec(select(Secret).where(Secret.key.in_(sorted(secret_keys))))  # type: ignore[arg-type]
        for secret in result.all():
            decrypted = safe_decrypt(secret.encrypted_value)
            if decrypted is not None:
                secrets[secret.key] = decrypted

    return {
        "steps": context.get("steps", {}),
        "run": run,
        "workflow": {
            "id": workflow_snapshot.get("id"),
            "name": workflow_snapshot.get("name"),
            "description": workflow_snapshot.get("description"),
            "board_id": workflow_snapshot.get("board_id"),
            "project_id": workflow_snapshot.get("project_id"),
            "trigger_type": workflow_snapshot.get("trigger_type"),
            "trigger_config": workflow_snapshot.get("trigger_config"),
            "max_runtime_minutes": workflow_snapshot.get("max_runtime_minutes"),
            "policy_profile": workflow_snapshot.get("policy_profile"),
        },
        "now": utcnow().isoformat(),
        "secrets": secrets,
    }


async def render_value(
    session: AsyncSession,
    value: Any,
    *,
    workflow_snapshot: dict[str, Any],
    run: dict[str, Any],
    context: dict[str, Any],
) -> Any:
    render_context = await build_render_context(
        session,
        workflow_snapshot=workflow_snapshot,
        run=run,
        context=context,
    )
    return _render_any(deepcopy(value), render_context)


def _render_any(value: Any, ctx: dict[str, Any]) -> Any:
    if isinstance(value, str):
        return _render_string(value, ctx)
    if isinstance(value, list):
        return [_render_any(item, ctx) for item in value]
    if isinstance(value, dict):
        return {k: _render_any(v, ctx) for k, v in value.items()}
    return value


def _render_string(template: str, ctx: dict[str, Any]) -> Any:
    matches = list(PLACEHOLDER_RE.finditer(template))
    if not matches:
        return template

    if len(matches) == 1 and matches[0].span() == (0, len(template)):
        return _resolve_path(matches[0].group(1), ctx)

    rendered = template
    for match in matches:
        raw = match.group(0)
        value = _resolve_path(match.group(1), ctx)
        rendered = rendered.replace(raw, "" if value is None else str(value))
    return rendered


def _resolve_path(path: str, ctx: dict[str, Any]) -> Any:
    current: Any = ctx
    for part in [segment.strip() for segment in path.split(".") if segment.strip()]:
        if isinstance(current, dict):
            if part not in current:
                raise WorkflowRenderError(f"Unknown template variable '{path}'")
            current = current[part]
            continue

        if isinstance(current, list):
            try:
                current = current[int(part)]
            except (ValueError, IndexError) as exc:
                raise WorkflowRenderError(f"Invalid list access in '{path}'") from exc
            continue

        if hasattr(current, part):
            current = getattr(current, part)
            continue

        raise WorkflowRenderError(f"Unknown template variable '{path}'")
    return current


def _collect_secret_refs(values: list[Any]) -> set[str]:
    refs: set[str] = set()
    for value in values:
        for text in _iter_strings(value):
            for match in PLACEHOLDER_RE.finditer(text):
                expr = match.group(1).strip()
                if expr.startswith("secrets."):
                    _, _, secret_key = expr.partition(".")
                    if secret_key:
                        refs.add(secret_key)
    return refs


def _iter_strings(value: Any):
    if isinstance(value, str):
        yield value
        return
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_strings(item)
