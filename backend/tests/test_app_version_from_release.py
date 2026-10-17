"""The version MC reports comes from the release tag, not a constant.

Until 0.3.1 `config.app_version` was a hard-coded "0.1.1": every install —
including fresh 0.2.0 ones — reported 0.1.1 and a permanent "update available"
banner. Now release.yml passes the tag as APP_VERSION, the backend image turns
it into an env var, and local/edge builds say "dev" (never flagged).
"""
from pathlib import Path

from app.config import Settings
from app.services.update_check import is_newer

REPO = Path(__file__).resolve().parents[2]


def test_version_comes_from_the_environment(monkeypatch):
    monkeypatch.setenv("APP_VERSION", "v0.3.1")
    assert Settings().app_version == "v0.3.1"


def test_local_builds_are_dev_and_never_flagged(monkeypatch):
    monkeypatch.delenv("APP_VERSION", raising=False)
    current = Settings().app_version
    assert current == "dev"
    assert is_newer("v9.9.9", current) is False


def test_release_build_passes_the_tag_and_the_image_reads_it():
    workflow = (REPO / ".github/workflows/release.yml").read_text()
    assert "APP_VERSION=${{ startsWith(github.ref, 'refs/tags/v') && github.ref_name" in workflow
    dockerfile = (REPO / "backend/Dockerfile").read_text()
    release_stage = dockerfile.split("FROM base AS release", 1)[1]
    assert "ARG APP_VERSION" in release_stage
    assert "ENV APP_VERSION=${APP_VERSION}" in release_stage


def test_a_release_is_not_newer_than_itself():
    assert is_newer("v0.3.1", "v0.3.1") is False
    assert is_newer("v0.3.2", "v0.3.1") is True
