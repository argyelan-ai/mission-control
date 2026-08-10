"""A runtime endpoint ending in "/v1" must not be probed at "/v1/v1/models".

Live symptom (Spark, 10.08.2026): the DeepSeek engine logged a steady stream of
`GET /v1/v1/models HTTP/1.1" 404 Not Found` while serving perfectly well. The
registry stores OpenAI-compatible endpoints *with* the version segment, the
default healthcheck path is "/v1/models", and `_probe_http` concatenated the
two. The 404 made a healthy runtime report as `stopped`.

A guard existed — but only inside the ssh_process branch, so every other
runtime type kept the bug. These tests pin the normalization at the single
place all branches go through.
"""

import pytest

from app.services.runtime_manager import join_probe_url


@pytest.mark.parametrize(
    ("endpoint", "path", "expected"),
    [
        # The live case: endpoint carries /v1, healthcheck repeats it.
        ("http://100.67.20.66:8000/v1", "/v1/models", "http://100.67.20.66:8000/v1/models"),
        # Trailing slash on the endpoint must not change the outcome.
        ("http://box:8000/v1/", "/v1/models", "http://box:8000/v1/models"),
        # Bare base URL — the version segment has to be kept.
        ("http://box:8000", "/v1/models", "http://box:8000/v1/models"),
        # Already-normalized config style stays untouched.
        ("http://box:8000/v1", "/models", "http://box:8000/v1/models"),
        # Non-versioned healthcheck paths pass through unharmed.
        ("http://box:8000/v1", "/health", "http://box:8000/v1/health"),
        ("http://box:8000", "/", "http://box:8000/"),
        # "/v1" alone against a "/v1" endpoint still has to reach the models list.
        ("http://box:8000/v1", "/v1", "http://box:8000/v1/models"),
        # A path segment that merely starts with the letters "v1" is not a version.
        ("http://box:8000/v1", "/v1beta/models", "http://box:8000/v1/v1beta/models"),
    ],
)
def test_join_probe_url_never_doubles_the_version_segment(endpoint, path, expected):
    assert join_probe_url(endpoint, path) == expected


@pytest.mark.parametrize("empty", [None, ""])
def test_missing_healthcheck_path_falls_back_to_models(empty):
    """A runtime row without a healthcheck path is probed at the models list."""
    assert join_probe_url("http://box:8000/v1", empty) == "http://box:8000/v1/models"
    assert join_probe_url("http://box:8000", empty) == "http://box:8000/v1/models"


def test_relative_healthcheck_path_gets_a_separator():
    """Registry rows written by hand may omit the leading slash."""
    assert join_probe_url("http://box:8000", "health") == "http://box:8000/health"


@pytest.mark.asyncio
async def test_probe_http_requests_the_normalized_url(monkeypatch):
    """The regression that mattered: _probe_http must not build a 404 URL."""
    from app.services import runtime_manager

    seen: list[str] = []

    class _Resp:
        status_code = 200

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            seen.append(url)
            return _Resp()

    monkeypatch.setattr(runtime_manager.httpx, "AsyncClient", _Client)

    assert await runtime_manager._probe_http("http://100.67.20.66:8000/v1", "/v1/models") is True
    assert seen == ["http://100.67.20.66:8000/v1/models"]
