"""422 responses without the client's input.

FastAPI's default handler answers a RequestValidationError with pydantic's
error list unchanged. Every entry carries ``input`` — the rejected value, and
for a missing field or a model-level validator the WHOLE request body. So
``POST /auth/register`` with a too-short password returned that password in
plain text, and the same held for credentials (``data.password``), secrets
(``value``), GitHub/skill tokens and every other body. From the response it
travels on into proxy logs, browser devtools and the frontend toast
(``lib/api.ts`` puts the raw body into the Error message).

Decision: strip ``input`` for ALL fields, not just names that look secret.
A name list can never be complete (``value``, ``data``, ``auth``, a
model-level error whose ``loc`` is just ``["body"]``), and nothing reads
``input``: the frontend shows ``detail`` as text and never looks at
``detail[].input``. The client already has what it sent.

``type``, ``loc`` and ``msg`` stay, so does the status code. ``ctx`` keeps
only the constraint values that come from the schema (``min_length``,
``pattern`` …); ``ctx.error`` is dropped — ``msg`` already holds its sentence.
As a last guard, a string from ``input`` that a custom validator copied into
``msg`` is cut out of it.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Any

from fastapi import Request, WebSocket
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError, WebSocketRequestValidationError
from fastapi.responses import JSONResponse
from starlette.status import WS_1008_POLICY_VIOLATION

REDACTED = "[redacted]"

# ctx keys that describe the SCHEMA, never the input. Anything else
# (``error`` = the raised exception, unknown keys of custom errors) goes.
_SAFE_CTX_KEYS = frozenset({
    "min_length", "max_length", "ge", "gt", "le", "lt", "multiple_of",
    "max_digits", "decimal_places", "whole_digits", "pattern", "expected",
    "allowed_schemes", "max_length_bytes",
})

# Shorter fragments would cut ordinary words out of messages; pydantic's own
# messages never contain the input, only custom validator sentences might.
_MIN_SCRUB_LEN = 4


def _string_leaves(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _string_leaves(v)
    elif isinstance(value, (list, tuple, set)):
        for v in value:
            yield from _string_leaves(v)


def _safe_ctx(ctx: Any) -> dict[str, Any] | None:
    if not isinstance(ctx, dict):
        return None
    safe = {
        k: v for k, v in ctx.items()
        if k in _SAFE_CTX_KEYS and isinstance(v, (int, float, str, bool))
    }
    return safe or None


def sanitize_validation_errors(errors: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """Copy of pydantic/FastAPI errors without anything the client sent."""
    out: list[dict[str, Any]] = []
    for err in errors:
        clean: dict[str, Any] = {
            k: v for k, v in err.items() if k not in ("input", "ctx")
        }
        ctx = _safe_ctx(err.get("ctx"))
        if ctx:
            clean["ctx"] = ctx
        msg = clean.get("msg")
        if isinstance(msg, str):
            # Longest first, so a fragment of a longer echoed value can't
            # leave the rest of it behind.
            for frag in sorted(set(_string_leaves(err.get("input"))), key=len, reverse=True):
                if len(frag) >= _MIN_SCRUB_LEN and frag in msg:
                    msg = msg.replace(frag, REDACTED)
            clean["msg"] = msg
        out.append(clean)
    return jsonable_encoder(out)


async def request_validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"detail": sanitize_validation_errors(exc.errors())},
    )


async def websocket_request_validation_exception_handler(
    websocket: WebSocket, exc: WebSocketRequestValidationError
) -> None:
    # FastAPI's default puts the error list (with input, e.g. a token query
    # param) into the close reason. Keep only the field names.
    locs = ", ".join(
        ".".join(str(p) for p in e.get("loc", ())) for e in exc.errors()
    )
    await websocket.close(
        code=WS_1008_POLICY_VIOLATION, reason=f"invalid request: {locs}"[:120]
    )


def install_validation_error_handlers(app) -> None:
    app.add_exception_handler(RequestValidationError, request_validation_exception_handler)
    app.add_exception_handler(
        WebSocketRequestValidationError, websocket_request_validation_exception_handler
    )
