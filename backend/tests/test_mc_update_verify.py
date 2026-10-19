"""Target integrity for the self-update engine (ADR-094 decision 5).

``scripts/mc_update_verify.py`` answers one question before the engine
changes anything on a machine: *is this release one we published, signed and
built for this machine?* It is stdlib-only (it runs on the host, next to the
engine) and fail-closed — every doubt is a ``VerifyError``, never a fallback.

The checks, in the order the engine runs them:

  before the pull
    1. the tag is a published, non-draft, non-prerelease GitHub release
       (API only — no fallback to git tags; API down = red),
    2. the tag's commit (from the local checkout after ``fetch``),
    3. every MC image is signed by this repository's ``release.yml`` on
       exactly this tag and commit — cosign keyless, run in a cosign
       container pinned by digest (no host install),
    4. the signed index has an image for this machine's architecture,
  after the pull
    5. the local image's ``RepoDigests`` contain the signed digest,
    6. its ``org.opencontainers.image.revision`` label is the tag's commit,
    7. its architecture is this machine's,
  after ``up``
    8. each running MC container runs exactly that image.

The tests drive the module through injected ``http_get`` / ``run`` fakes and,
end to end, through the CLI with a fake ``docker`` in PATH and a ``file://``
release API. No network, no real Docker, no real release.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "scripts" / "mc_update_verify.py"
GATE_CHECK = ROOT / "scripts" / "release_gate_check.py"
RELEASE_YML = ROOT / ".github" / "workflows" / "release.yml"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, path
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def mv():
    return _load(MODULE_PATH, "mc_update_verify")


TAG = "v1.2.3"
VERSION = "1.2.3"
COMMIT = "a" * 40
OTHER_COMMIT = "b" * 40
DIGEST = {"mc-backend": "sha256:" + "1" * 64, "mc-frontend": "sha256:" + "2" * 64}
IMAGE_ID = {"mc-backend": "sha256:" + "d" * 64, "mc-frontend": "sha256:" + "e" * 64}
IDENTITY = (
    "https://github.com/argyelan-ai/mission-control/.github/workflows/release.yml"
    "@refs/tags/v1.2.3"
)


def _release(**over):
    rel = {
        "tag_name": TAG,
        "draft": False,
        "prerelease": False,
        "published_at": "2026-10-19T08:00:00Z",
    }
    rel.update(over)
    return rel


class FakeHttp:
    """``http_get(url) -> bytes``; raises like the real one."""

    def __init__(self, answers):
        self.answers = answers  # url-suffix -> bytes | Exception
        self.calls = []

    def __call__(self, url):
        self.calls.append(url)
        for suffix, ans in self.answers.items():
            if url.endswith(suffix):
                if isinstance(ans, BaseException):
                    raise ans
                return ans
        raise AssertionError("unexpected URL " + url)


def _cosign_json(image, digest, fmt="v3"):
    if fmt == "v3":
        crit = {
            "identity": {"docker-reference": "ghcr.io/argyelan-ai/%s:%s" % (image, VERSION)},
            "image": {"docker-manifest-digest": digest},
            "type": "https://sigstore.dev/cosign/sign/v1",
        }
        return json.dumps([{"critical": crit, "optional": {}}])
    crit = {
        "identity": {"docker-reference": "ghcr.io/argyelan-ai/" + image},
        "image": {"docker-manifest-digest": digest},
        "type": "cosign container image signature",
    }
    return json.dumps([{"critical": crit, "optional": {"1.3.6.1.4.1.57264.1.3": COMMIT}}])


def _index(platforms):
    return json.dumps(
        {
            "mediaType": "application/vnd.oci.image.index.v1+json",
            "manifests": [
                {"digest": "sha256:" + str(i) * 64, "platform": {"architecture": a, "os": o}}
                for i, (o, a) in enumerate(platforms)
            ],
        }
    )


class FakeDocker:
    """``run(argv, timeout) -> (rc, stdout, stderr)`` that answers like docker."""

    def __init__(self, arch="arm64", platforms=None, cosign=None, inspect=None, containers=None):
        self.arch = arch
        self.platforms = platforms or [("linux", "amd64"), ("linux", "arm64"), ("unknown", "unknown")]
        self.cosign = cosign or {}  # image -> (rc, stdout)
        self.inspect = inspect or {}  # image -> dict override
        self.containers = containers or {}
        self.calls = []

    def __call__(self, argv, timeout):
        self.calls.append(list(argv))
        assert argv[0] == "docker", argv
        if argv[1:3] == ["version", "--format"]:
            return 0, self.arch + "\n", ""
        if argv[1] == "run":
            ref = argv[-1]
            image = ref.split("/")[-1].split(":")[0].split("@")[0]
            rc, out = self.cosign.get(image, (0, _cosign_json(image, DIGEST[image])))
            return rc, out, "" if rc == 0 else "Error: no signatures found"
        if argv[1:3] == ["manifest", "inspect"]:
            return 0, _index(self.platforms), ""
        if argv[1:3] == ["image", "inspect"]:
            ref = argv[-1]
            image = ref.split("/")[-1].split(":")[0]
            data = {
                "Id": IMAGE_ID[image],
                "RepoDigests": ["ghcr.io/argyelan-ai/%s@%s" % (image, DIGEST[image])],
                "Architecture": self.arch,
                "Os": "linux",
                "Config": {"Labels": {"org.opencontainers.image.revision": COMMIT}},
            }
            data.update(self.inspect.get(image, {}))
            return 0, json.dumps(data), ""
        if argv[1] == "inspect":
            return 0, self.containers.get(argv[-1], "") + "\n", ""
        raise AssertionError("unexpected docker call %r" % (argv,))

    def called(self, *words):
        return [c for c in self.calls if all(w in c for w in words)]


@pytest.fixture()
def repo(tmp_path):
    """A git checkout with an annotated release tag."""
    r = tmp_path / "repo"
    r.mkdir()
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@example.invalid",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@example.invalid")

    def git(*a):
        return subprocess.run(["git", "-C", str(r), *a], check=True, env=env,
                              capture_output=True, text=True).stdout.strip()

    git("init", "-q")
    (r / "f").write_text("x")
    git("add", "f")
    git("commit", "-qm", "c")
    git("tag", "-a", TAG, "-m", TAG)
    return r, git("rev-parse", "HEAD")


def _http_ok():
    return FakeHttp({"/releases/tags/" + TAG: json.dumps(_release()).encode()})


# ---------------------------------------------------------------- pinning ---

def test_cosign_image_is_pinned_by_digest(mv):
    assert re.fullmatch(r"ghcr\.io/sigstore/cosign/cosign:v3\.1\.3@sha256:[0-9a-f]{64}", mv.COSIGN_IMAGE)
    assert mv.COSIGN_VERSION == "v3.1.3"


def test_trust_defaults_are_this_repository(mv):
    t = mv.Trust()
    assert t.repo == "argyelan-ai/mission-control"
    assert t.registry == "ghcr.io/argyelan-ai"
    assert t.images == ("mc-backend", "mc-frontend")
    assert t.identity(TAG) == IDENTITY


# ---------------------------------------------------------------- release ---

def test_published_stable_release_passes(mv):
    rel = mv.check_release(TAG, http_get=_http_ok())
    assert rel["tag_name"] == TAG


@pytest.mark.parametrize(
    "over, code",
    [
        ({"draft": True}, "release_not_published"),
        ({"published_at": None}, "release_not_published"),
        ({"prerelease": True}, "release_prerelease"),
        ({"tag_name": "v1.2.4"}, "release_mismatch"),
    ],
)
def test_release_that_is_not_a_published_stable_one_is_refused(mv, over, code):
    http = FakeHttp({"/releases/tags/" + TAG: json.dumps(_release(**over)).encode()})
    with pytest.raises(mv.VerifyError) as e:
        mv.check_release(TAG, http_get=http)
    assert e.value.code == code


def test_tag_without_release_is_refused(mv):
    """Sabotage (b): a tag that was never published as a release."""
    http = FakeHttp({"/releases/tags/" + TAG: mv.HttpError(404)})
    with pytest.raises(mv.VerifyError) as e:
        mv.check_release(TAG, http_get=http)
    assert e.value.code == "release_not_found"


@pytest.mark.parametrize(
    "answer",
    [OSError("network is unreachable"), TimeoutError("timed out"), b"<html>not json", b"[]"],
)
def test_api_unreachable_or_garbage_is_red_not_a_fallback(mv, answer):
    """Sabotage (c): GitHub API gone → red, and nothing else is consulted."""
    http = FakeHttp({"/releases/tags/" + TAG: answer})
    with pytest.raises(mv.VerifyError) as e:
        mv.check_release(TAG, http_get=http)
    assert e.value.code == "release_api_unavailable"


@pytest.mark.parametrize("status", [403, 429, 500, 502])
def test_rate_limit_and_server_errors_are_api_unavailable(mv, status):
    http = FakeHttp({"/releases/tags/" + TAG: mv.HttpError(status)})
    with pytest.raises(mv.VerifyError) as e:
        mv.check_release(TAG, http_get=http)
    assert e.value.code == "release_api_unavailable"


def test_latest_release_reads_only_the_api(mv):
    http = FakeHttp({"/releases/latest": json.dumps(_release()).encode()})
    assert mv.latest_release_tag(http_get=http) == TAG
    down = FakeHttp({"/releases/latest": OSError("down")})
    with pytest.raises(mv.VerifyError) as e:
        mv.latest_release_tag(http_get=down)
    assert e.value.code == "release_api_unavailable"


@pytest.mark.parametrize("bad", ["v1.2", "1.2.3", "v1.2.3-rc1", "v1.2.3;rm", "v1.2.3\n", ""])
def test_only_strict_stable_tags_are_accepted(mv, bad):
    with pytest.raises(mv.VerifyError) as e:
        mv.check_release(bad, http_get=FakeHttp({}))
    assert e.value.code == "invalid_tag"


@pytest.mark.parametrize(
    "current, target, ok",
    [("0.9.3", TAG, True), ("v1.2.2", TAG, True), ("1.2.3", TAG, False),
     ("1.10.0", TAG, False), ("edge", TAG, False), ("", TAG, False)],
)
def test_only_forward(mv, current, target, ok):
    if ok:
        mv.check_forward(current, target)
    else:
        with pytest.raises(mv.VerifyError) as e:
            mv.check_forward(current, target)
        assert e.value.code == "not_forward"


# ------------------------------------------------------------ tag commit ---

def test_tag_commit_resolves_annotated_tag(mv, repo):
    path, head = repo
    assert mv.tag_commit(path, TAG) == head


def test_missing_tag_is_refused(mv, repo):
    path, _ = repo
    with pytest.raises(mv.VerifyError) as e:
        mv.tag_commit(path, "v9.9.9")
    assert e.value.code == "tag_missing"


# -------------------------------------------------------------- signature ---

def test_cosign_argv_binds_repo_workflow_tag_and_commit(mv):
    argv = mv.cosign_verify_argv("mc-backend", TAG, COMMIT)
    assert argv[:4] == ["docker", "run", "--rm", mv.COSIGN_IMAGE]
    assert argv[4] == "verify"
    flags = dict(zip(argv[5:-1:2], argv[6:-1:2]))
    assert flags == {
        "--certificate-identity": IDENTITY,
        "--certificate-oidc-issuer": "https://token.actions.githubusercontent.com",
        "--certificate-github-workflow-repository": "argyelan-ai/mission-control",
        "--certificate-github-workflow-ref": "refs/tags/v1.2.3",
        "--certificate-github-workflow-sha": COMMIT,
        "--certificate-github-workflow-trigger": "push",
    }
    assert argv[-1] == "ghcr.io/argyelan-ai/mc-backend:1.2.3"
    assert not any("regexp" in a for a in argv), "an identity regexp would accept any workflow"


@pytest.mark.parametrize("fmt", ["v3", "legacy"])
def test_signed_digest_is_read_from_cosign_output(mv, fmt):
    out = _cosign_json("mc-backend", DIGEST["mc-backend"], fmt)
    assert mv.parse_cosign_output(out, "ghcr.io/argyelan-ai/mc-backend") == DIGEST["mc-backend"]


@pytest.mark.parametrize(
    "out",
    [
        "",
        "[]",
        "not json",
        json.dumps([{"critical": {"identity": {"docker-reference": "ghcr.io/evil/mc-backend"},
                                  "image": {"docker-manifest-digest": DIGEST["mc-backend"]}}}]),
        json.dumps([{"critical": {"identity": {"docker-reference": "ghcr.io/argyelan-ai/mc-backend"},
                                  "image": {"docker-manifest-digest": "sha256:short"}}}]),
        # two signatures for two different digests: ambiguous → refuse
        json.dumps(json.loads(_cosign_json("mc-backend", DIGEST["mc-backend"]))
                   + json.loads(_cosign_json("mc-backend", "sha256:" + "9" * 64))),
    ],
)
def test_unusable_cosign_output_is_refused(mv, out):
    with pytest.raises(mv.VerifyError) as e:
        mv.parse_cosign_output(out, "ghcr.io/argyelan-ai/mc-backend")
    assert e.value.code == "signature_invalid"


@pytest.mark.parametrize("rc", [1, 10, 11, 12])
def test_unsigned_or_retagged_image_stops_before_any_pull(mv, repo, rc):
    """Sabotage (a): cosign refuses (10 = no signature, 12 = other tag/commit)."""
    path, head = repo
    docker = FakeDocker(cosign={"mc-frontend": (rc, "")})
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_release(TAG, repo_path=path, run=docker, http_get=_http_ok())
    assert e.value.code == "signature_invalid"
    assert "mc-frontend" in str(e.value)
    assert not docker.called("pull")
    assert not docker.called("manifest")


def test_release_check_runs_before_any_docker_call(mv, repo):
    path, _ = repo
    docker = FakeDocker()
    http = FakeHttp({"/releases/tags/" + TAG: mv.HttpError(404)})
    with pytest.raises(mv.VerifyError):
        mv.verify_release(TAG, repo_path=path, run=docker, http_get=http)
    assert docker.calls == []


# ------------------------------------------------------------ architecture ---

@pytest.mark.parametrize("raw, norm", [("arm64", "arm64"), ("aarch64", "arm64"),
                                       ("x86_64", "amd64"), ("amd64", "amd64")])
def test_host_arch_is_normalised(mv, raw, norm):
    assert mv.host_arch(FakeDocker(arch=raw)) == norm


def test_release_without_arm64_is_red_on_a_pi(mv, repo):
    """Sabotage (d): the signed index has no arm64 image."""
    path, _ = repo
    docker = FakeDocker(arch="aarch64", platforms=[("linux", "amd64"), ("unknown", "unknown")])
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_release(TAG, repo_path=path, run=docker, http_get=_http_ok())
    assert e.value.code == "arch_missing"
    # the platform list is read from the SIGNED digest, not from the movable tag
    m = docker.called("manifest", "inspect")
    assert m and m[0][-1] == "ghcr.io/argyelan-ai/mc-backend@" + DIGEST["mc-backend"]


def test_full_pre_pull_verification(mv, repo):
    path, head = repo
    docker = FakeDocker(cosign={
        "mc-backend": (0, _cosign_json("mc-backend", DIGEST["mc-backend"])),
        "mc-frontend": (0, _cosign_json("mc-frontend", DIGEST["mc-frontend"])),
    })
    v = mv.verify_release(TAG, repo_path=path, run=docker, http_get=_http_ok())
    assert v.tag == TAG and v.version == VERSION and v.commit == head and v.arch == "arm64"
    assert {i.name: i.digest for i in v.images} == DIGEST
    # cosign was asked about the tag's real commit, for both images
    runs = docker.called("run")
    assert len(runs) == 2 and all(head in r for r in runs)


# ------------------------------------------------------------- after pull ---

def _verified(mv, commit=COMMIT, arch="arm64"):
    return mv.Verification(
        tag=TAG, version=VERSION, commit=commit, arch=arch,
        images=tuple(mv.SignedImage(name=n, ref="ghcr.io/argyelan-ai/%s:%s" % (n, VERSION),
                                    digest=d) for n, d in DIGEST.items()),
    )


def test_pulled_images_match_the_signed_build(mv):
    ids = mv.verify_pulled(_verified(mv), run=FakeDocker())
    assert ids == IMAGE_ID


def test_retagged_image_after_pull_is_refused(mv):
    """The tag moved between verify and pull: RepoDigests no longer match."""
    docker = FakeDocker(inspect={"mc-backend": {
        "RepoDigests": ["ghcr.io/argyelan-ai/mc-backend@sha256:" + "f" * 64]}})
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_pulled(_verified(mv), run=docker)
    assert e.value.code == "digest_mismatch"


def test_same_digest_from_another_registry_does_not_count(mv):
    docker = FakeDocker(inspect={"mc-backend": {
        "RepoDigests": ["ghcr.io/evil/mc-backend@" + DIGEST["mc-backend"]]}})
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_pulled(_verified(mv), run=docker)
    assert e.value.code == "digest_mismatch"


@pytest.mark.parametrize("labels", [{"org.opencontainers.image.revision": OTHER_COMMIT}, {}, None])
def test_revision_label_must_be_the_tag_commit(mv, labels):
    docker = FakeDocker(inspect={"mc-frontend": {"Config": {"Labels": labels}}})
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_pulled(_verified(mv), run=docker)
    assert e.value.code == "revision_mismatch"


def test_pulled_image_for_the_wrong_architecture_is_refused(mv):
    docker = FakeDocker(inspect={"mc-backend": {"Architecture": "amd64"}})
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_pulled(_verified(mv), run=docker)
    assert e.value.code == "arch_missing"


def test_running_containers_must_run_the_verified_image(mv):
    v = _verified(mv)
    good = FakeDocker(containers={"mc-backend-1": IMAGE_ID["mc-backend"],
                                  "mc-worker-1": IMAGE_ID["mc-backend"],
                                  "mc-frontend-1": IMAGE_ID["mc-frontend"]})
    want = {"mc-backend-1": "mc-backend", "mc-worker-1": "mc-backend", "mc-frontend-1": "mc-frontend"}
    mv.verify_containers(IMAGE_ID, want, run=good)
    bad = FakeDocker(containers={"mc-backend-1": IMAGE_ID["mc-backend"],
                                 "mc-worker-1": "sha256:" + "0" * 64,
                                 "mc-frontend-1": IMAGE_ID["mc-frontend"]})
    with pytest.raises(mv.VerifyError) as e:
        mv.verify_containers(IMAGE_ID, want, run=bad)
    assert e.value.code == "container_image_mismatch"
    assert "mc-worker-1" in str(e.value)


def test_docker_missing_is_an_error_not_a_crash(mv):
    def run(argv, timeout):
        raise FileNotFoundError("docker")

    with pytest.raises(mv.VerifyError) as e:
        mv.host_arch(run)
    assert e.value.code == "docker_unavailable"


# -------------------------------------------- CLI end to end (fake docker) ---

FAKE_DOCKER = r"""#!/usr/bin/env python3
import json, os, sys
a = sys.argv[1:]
open(os.environ["FAKE_LOG"], "a").write(json.dumps(a) + "\n")
st = json.load(open(os.environ["FAKE_STATE"]))
if a[:2] == ["version", "--format"]:
    print(st["arch"]); sys.exit(0)
if a[0] == "run":
    image = a[-1].split("/")[-1].split(":")[0]
    if image in st.get("unsigned", []):
        sys.stderr.write("Error: no signatures found\n"); sys.exit(10)
    if st["commit"] not in a:
        sys.stderr.write("expected GitHub Workflow SHA not found\n"); sys.exit(12)
    print(json.dumps([{"critical": {"identity": {"docker-reference": "ghcr.io/argyelan-ai/" + image},
          "image": {"docker-manifest-digest": st["digest"][image]}}}])); sys.exit(0)
if a[:2] == ["manifest", "inspect"]:
    print(json.dumps({"manifests": [{"platform": {"os": "linux", "architecture": p}} for p in st["platforms"]]})); sys.exit(0)
sys.stderr.write("unexpected: %r\n" % (a,)); sys.exit(99)
"""


def _cli(tmp_path, repo_path, commit, release, unsigned=(), platforms=("amd64", "arm64")):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    d = bindir / "docker"
    d.write_text(FAKE_DOCKER)
    d.chmod(0o755)
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"arch": "aarch64", "commit": commit, "digest": DIGEST,
                                 "unsigned": list(unsigned), "platforms": list(platforms)}))
    api = tmp_path / "api" / "repos" / "argyelan-ai" / "mission-control" / "releases" / "tags"
    api.mkdir(parents=True, exist_ok=True)
    if release is not None:
        (api / TAG).write_text(json.dumps(release))
    log = tmp_path / "docker.log"
    env = dict(os.environ, PATH="%s:%s" % (bindir, os.environ["PATH"]),
               FAKE_STATE=str(state), FAKE_LOG=str(log))
    p = subprocess.run(
        [sys.executable, str(MODULE_PATH), "release", TAG, "--repo-path", str(repo_path),
         "--api-base", (tmp_path / "api").as_uri()],
        env=env, capture_output=True, text=True, timeout=60,
    )
    calls = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
    return p, calls


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_cli_release_ok(tmp_path, repo):
    path, head = repo
    p, calls = _cli(tmp_path, path, head, _release())
    assert p.returncode == 0, p.stderr
    out = json.loads(p.stdout)
    assert out["ok"] is True and out["commit"] == head and out["arch"] == "arm64"
    assert out["images"]["mc-backend"]["digest"] == DIGEST["mc-backend"]
    assert any(c[0] == "run" and c[2].startswith("ghcr.io/sigstore/cosign/cosign:v3.1.3@sha256:") for c in calls)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    "case, code",
    [("unsigned", "signature_invalid"), ("no_release", "release_not_found"),
     ("prerelease", "release_prerelease"), ("no_arm64", "arch_missing")],
)
def test_cli_refuses_with_exit_1_and_a_code(tmp_path, repo, case, code):
    path, head = repo
    kw = {"unsigned": (), "platforms": ("amd64", "arm64")}
    rel = _release()
    if case == "unsigned":
        kw["unsigned"] = ("mc-backend",)
    elif case == "no_release":
        rel = None
    elif case == "prerelease":
        rel = _release(prerelease=True)
    elif case == "no_arm64":
        kw["platforms"] = ("amd64",)
    p, calls = _cli(tmp_path, path, head, rel, **kw)
    assert p.returncode == 1, (p.stdout, p.stderr)
    out = json.loads(p.stdout)
    assert out == {"ok": False, "code": code, "message": out["message"]}
    assert not any(c[0] == "pull" for c in calls)


# ------------------------------------------------- release.yml: the gate ---

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load(RELEASE_YML.read_text())


def _steps_text(job):
    return "\n".join((s.get("run") or "") + " " + json.dumps(s.get("with") or {}) + " "
                     + json.dumps(s.get("env") or {}) + " " + (s.get("uses") or "")
                     for s in job["steps"])


def test_signing_job_sits_between_verify_and_promote_behind_the_release_environment(wf):
    jobs = wf["jobs"]
    sign = jobs["sign"]
    assert sign["needs"] == "images" or sign["needs"] == ["images"] or "images" in sign["needs"]
    env = sign["environment"]
    assert (env if isinstance(env, str) else env["name"]) == "release"
    assert "github.event_name == 'push'" in sign["if"]
    assert "startsWith(github.ref, 'refs/tags/v')" in sign["if"]
    promote = jobs["promote"]
    needs = promote["needs"] if isinstance(promote["needs"], list) else [promote["needs"]]
    assert "sign" in needs


def test_only_the_signing_job_gets_an_oidc_token(wf):
    for name, job in wf["jobs"].items():
        perms = job.get("permissions") or {}
        if name == "sign":
            assert perms.get("id-token") == "write"
        else:
            assert "id-token" not in perms, name
    assert "id-token" not in (wf.get("permissions") or {})


def test_signing_job_checks_the_gate_first_and_signs_by_digest(wf):
    steps = wf["jobs"]["sign"]["steps"]
    texts = [(s.get("run") or "") for s in steps]
    gate = next(i for i, t in enumerate(texts) if "release_gate_check.py" in t)
    sign = next(i for i, t in enumerate(texts) if "cosign sign" in t)
    assert gate < sign
    assert re.search(r'cosign sign --yes "?\$\{?ref\}?"?', texts[sign]) or "@${digest}" in texts[sign] \
        or "@$digest" in texts[sign]
    installer = next(s for s in steps if (s.get("uses") or "").startswith("sigstore/cosign-installer@"))
    assert re.fullmatch(r"sigstore/cosign-installer@[0-9a-f]{40}", installer["uses"])
    assert installer["with"]["cosign-release"] == "v3.1.3"


def test_promote_retags_the_signed_digest_and_proves_the_engine_accepts_it(wf):
    text = _steps_text(wf["jobs"]["promote"])
    assert "needs.sign.outputs.digests" in text
    assert "mc_update_verify.py signature" in text


def test_cosign_versions_agree(mv, wf):
    steps = wf["jobs"]["sign"]["steps"]
    installer = next(s for s in steps if (s.get("uses") or "").startswith("sigstore/cosign-installer@"))
    assert installer["with"]["cosign-release"] == mv.COSIGN_VERSION


# ---------------------------------------------- release_gate_check.py ---

@pytest.fixture(scope="module")
def gate():
    return _load(GATE_CHECK, "release_gate_check")


@pytest.mark.parametrize(
    "env, ok",
    [
        ({"name": "release", "protection_rules": [
            {"type": "required_reviewers", "reviewers": [{"type": "User", "reviewer": {"login": "x"}}]}]}, True),
        ({"name": "release", "protection_rules": []}, False),
        ({"name": "release", "protection_rules": [{"type": "wait_timer", "wait_timer": 5}]}, False),
        ({"name": "release", "protection_rules": [{"type": "required_reviewers", "reviewers": []}]}, False),
        (None, False),
    ],
)
def test_gate_check_requires_reviewers(gate, env, ok):
    problems = gate.problems(env)
    assert (problems == []) is ok, problems


def test_gate_check_cli_exit_codes(tmp_path):
    good = tmp_path / "good.json"
    good.write_text(json.dumps({"name": "release", "protection_rules": [
        {"type": "required_reviewers", "reviewers": [{"type": "User"}]}]}))
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"name": "release", "protection_rules": []}))
    r = subprocess.run([sys.executable, str(GATE_CHECK), "--from-file", str(good)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run([sys.executable, str(GATE_CHECK), "--from-file", str(bad)], capture_output=True, text=True)
    assert r.returncode == 1
    assert "required reviewer" in r.stdout + r.stderr
    r = subprocess.run([sys.executable, str(GATE_CHECK), "--from-file", str(tmp_path / "missing.json")],
                       capture_output=True, text=True)
    assert r.returncode == 1
