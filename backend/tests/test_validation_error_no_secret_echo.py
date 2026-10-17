"""A 422 must never echo what the client sent.

FastAPI's default RequestValidationError handler returns pydantic's error list
as-is — and every entry carries ``input``: the rejected value, or for a
missing field / a model-level validator the WHOLE request body. A register
call with a too-short password came back with that password in plain text,
from where it lands in proxy logs, browser devtools and the frontend toast
(lib/api.ts puts the raw body into the Error message).

app/validation_errors.py strips ``input`` (and unsafe ``ctx``) from every
error, for every route. These tests search the WHOLE response body text for
the secret, not one field — a leak through any key counts.
"""
import pytest
from httpx import AsyncClient

SECRET = "Zq9-leak-probe-7Hx"


def _assert_no_echo(resp, *secrets: str) -> None:
    assert resp.status_code == 422, resp.text
    text = resp.text
    for s in secrets:
        assert s not in text, f"422 body echoes a secret: {text}"
    detail = resp.json()["detail"]
    assert isinstance(detail, list) and detail, "422 shape changed"
    for err in detail:
        # Status, location and message stay — only the echo goes.
        assert "input" not in err
        assert err["loc"] and err["msg"] and err["type"]


@pytest.mark.asyncio
async def test_register_short_password_not_echoed(client: AsyncClient):
    short = "Zq9x"  # below min_length=6
    resp = await client.post(
        "/api/v1/auth/register",
        json={"email": "a@example.com", "name": "A", "password": short},
    )
    _assert_no_echo(resp, short)
    err = resp.json()["detail"][0]
    assert err["loc"] == ["body", "password"]
    assert err["type"] == "string_too_short"
    # The limit itself is not secret — the UI may keep showing it.
    assert err["ctx"] == {"min_length": 6}


@pytest.mark.asyncio
async def test_register_missing_field_does_not_echo_whole_body(client: AsyncClient):
    # A missing field puts the ENTIRE body into `input` — password included.
    resp = await client.post(
        "/api/v1/auth/register",
        json={"name": "A", "password": SECRET},
    )
    _assert_no_echo(resp, SECRET)
    assert resp.json()["detail"][0]["loc"] == ["body", "email"]


@pytest.mark.asyncio
async def test_login_missing_field_not_echoed(client: AsyncClient):
    resp = await client.post("/api/v1/auth/login", json={"password": SECRET})
    _assert_no_echo(resp, SECRET)


@pytest.mark.asyncio
async def test_credential_create_validator_does_not_echo_data(auth_client: AsyncClient):
    # login without url fails the model validator → input is the whole body,
    # i.e. the stored username/password pair.
    resp = await auth_client.post(
        "/api/v1/credentials",
        json={
            "name": "probe",
            "credential_type": "login",
            "data": {"username": "probe-user", "password": SECRET},
        },
    )
    _assert_no_echo(resp, SECRET)
    # The human sentence of the validator stays readable.
    assert "url" in resp.json()["detail"][0]["msg"]


@pytest.mark.asyncio
async def test_secret_create_does_not_echo_value(auth_client: AsyncClient):
    # Missing key → the body (with the API key value) would be the input.
    resp = await auth_client.post("/api/v1/secrets", json={"value": SECRET})
    _assert_no_echo(resp, SECRET)

    # Invalid key pattern → still no value anywhere.
    resp = await auth_client.post(
        "/api/v1/secrets", json={"key": "Bad-Key!", "value": SECRET}
    )
    _assert_no_echo(resp, SECRET, "Bad-Key!")
    assert resp.json()["detail"][0]["ctx"] == {"pattern": "^[a-z0-9_]+$"}


@pytest.mark.asyncio
async def test_secret_update_wrong_type_not_echoed(auth_client: AsyncClient):
    resp = await auth_client.patch(
        "/api/v1/secrets/some_key", json={"value": {"nested": SECRET}}
    )
    _assert_no_echo(resp, SECRET)


@pytest.mark.asyncio
async def test_invalid_json_body_not_echoed(client: AsyncClient):
    resp = await client.post(
        "/api/v1/auth/login",
        content='{"email": "a@example.com", "password": "' + SECRET,
        headers={"Content-Type": "application/json"},
    )
    _assert_no_echo(resp, SECRET)


def test_sanitize_scrubs_value_echoed_inside_custom_message():
    """A custom validator that puts the value into its message must not leak
    it through `msg` or `ctx.error` either."""
    from app.validation_errors import sanitize_validation_errors

    out = sanitize_validation_errors(
        [
            {
                "type": "value_error",
                "loc": ("body", "token"),
                "msg": f"Value error, token {SECRET} is not valid",
                "input": SECRET,
                "ctx": {"error": ValueError(f"token {SECRET} is not valid")},
            }
        ]
    )
    assert SECRET not in repr(out)
    assert out[0]["loc"] == ["body", "token"]
    assert out[0]["msg"].startswith("Value error, token ")
    assert "ctx" not in out[0]  # nothing safe left in it
