"""A6/A7 — host-side files that must stay in line with the backend."""
from __future__ import annotations

import importlib.machinery
import importlib.util
import plistlib
from pathlib import Path

from tests.heads_host_helpers import MC_HEAD, REPO_ROOT


def _load_mc_head():
    loader = importlib.machinery.SourceFileLoader("mc_head_script", str(MC_HEAD))
    spec = importlib.util.spec_from_loader("mc_head_script", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def test_omp_profile_matches_backend_renderer(tmp_path):
    """mc-head renders omp's models.yml on the host; it must be the same file
    the backend renders for host omp agents (host_provisioning)."""
    from app.services.host_provisioning import render_omp_host_models_yml

    mc_head = _load_mc_head()
    spec = {"base_url": "http://127.0.0.1:8000/v1", "model": "GLM-5.3-Flash-EXL3"}
    host_file = mc_head.render_omp_profile(tmp_path / "a", spec, {})
    backend_file = render_omp_host_models_yml(
        "head",
        {"OPENAI_BASE_URL": spec["base_url"], "OPENAI_MODEL": spec["model"]},
        home=tmp_path / "b",
    )
    assert Path(backend_file).parent.parent.name == f"mc-{'head'}" == mc_head.OMP_PROFILE
    assert host_file.read_text() == Path(backend_file).read_text()


def test_launchd_template_watches_spool_and_runs_installed_copy():
    raw = (REPO_ROOT / "scripts" / "head" / "com.mc.head-starter.plist.template").read_bytes()
    plist = plistlib.loads(raw.replace(b"__MC_HOME__", b"/x/.mc").replace(b"__HOME__", b"/x"))
    assert plist["WatchPaths"] == ["/x/.mc/heads/spool"]
    assert plist["StartInterval"] == 30
    assert plist["ProgramArguments"] == ["/usr/bin/python3", "/x/.mc/bin/mc-head", "watch"]


def test_procedure_template_in_backend_is_the_docs_source():
    """Single source: docs/specs/head-launcher-AGENTS.md. The backend image
    only contains backend/, so it ships a copy — byte-identical."""
    docs = (REPO_ROOT / "docs" / "specs" / "head-launcher-AGENTS.md").read_text()
    shipped = (REPO_ROOT / "backend" / "templates" / "heads" / "head-AGENTS.md").read_text()
    assert shipped == docs
