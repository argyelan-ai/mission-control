#!/usr/bin/env python3
"""Target integrity for the self-update engine (ADR-094 decision 5).

Answers one question before the engine changes anything on a machine:
*is this release one we published, signed and built for this machine?*

Fail-closed by design: every doubt is a ``VerifyError`` with a stable
``code`` (the UI translates the code, the message is for the log). There is
no fallback — not to git tags when the GitHub API is down, not to "unsigned
is fine" when cosign cannot run.

Order, as the engine calls it:

  ``verify_release(tag)``   before the pull — nothing on the machine changes
      1. the tag is a published, non-draft, non-prerelease GitHub release,
      2. the tag's commit, read from the local checkout (after ``fetch``),
      3. every MC image is signed by this repository's ``release.yml`` on
         exactly this tag and commit (cosign keyless, in a cosign container
         pinned by digest — no host install),
      4. the signed index carries an image for this machine's architecture.
  ``verify_pulled(v)``      after ``docker compose pull backend frontend``
      5. the local image's ``RepoDigests`` contain the signed digest,
      6. its ``org.opencontainers.image.revision`` label is the tag commit,
      7. its architecture is this machine's.
  ``verify_containers(...)`` after ``up``: each MC container runs that image.

What the signature proves, literally: the image was built by this
repository's CI, by workflow ``release.yml``, on a push of this tag at this
commit. It protects against a compromised registry, re-tagging and a swapped
image. It does not protect against a release approved on purpose that is
malicious or broken, nor against a compromised maintainer account.

Standard library only; Python 3.9+ (macOS system python). The engine imports
it; the CLI exists for the release workflow and for checking by hand:

    mc_update_verify.py release   vX.Y.Z [--repo-path DIR]   # steps 1-4
    mc_update_verify.py signature vX.Y.Z --commit SHA         # step 3 only
    mc_update_verify.py latest                                # newest stable tag

Output is one JSON object on stdout; exit 0 = verified, 1 = refused.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Sequence, Tuple

# The cosign that verifies. Pinned by the multi-arch index digest: the git
# checkout is the trust anchor for the verifier itself. Spike S1 ran this
# version natively on arm64. Bump both lines together with the
# `cosign-release` input in .github/workflows/release.yml (a test holds
# them equal).
COSIGN_VERSION = "v3.1.3"
COSIGN_IMAGE = (
    "ghcr.io/sigstore/cosign/cosign:v3.1.3"
    "@sha256:9e5c2f2edc34351160407ca3416c61855bdf9403c3c5936e0f0be7fc261611b8"
)

OIDC_ISSUER = "https://token.actions.githubusercontent.com"
REVISION_LABEL = "org.opencontainers.image.revision"

STABLE_TAG = re.compile(r"^v(\d+)\.(\d+)\.(\d+)$")
VERSION_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")

# Timeouts in seconds. cosign pulls its ~200 MB image on first use and
# fetches the Sigstore trust root; a Pi on SD card needed ~20 s in S1.
HTTP_TIMEOUT = 15
COSIGN_TIMEOUT = 300
DOCKER_TIMEOUT = 60

ARCH_ALIASES = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}


class VerifyError(Exception):
    """A refused target. ``code`` is stable; ``str()`` is for the log."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class HttpError(Exception):
    """An HTTP answer that is not 2xx (the default getter raises it)."""

    def __init__(self, status: int):
        super().__init__("HTTP %d" % status)
        self.status = status


Runner = Callable[[Sequence[str], int], Tuple[int, str, str]]
HttpGet = Callable[[str], bytes]


@dataclass(frozen=True)
class Trust:
    """Who may have built an image. Defaults = this repository.

    Overridden only for an acceptance run from a fork (ADR-094 §7); the
    engine never reads these from ``.env``.
    """

    repo: str = "argyelan-ai/mission-control"
    registry: str = "ghcr.io/argyelan-ai"
    workflow: str = ".github/workflows/release.yml"
    images: Tuple[str, ...] = ("mc-backend", "mc-frontend")
    api_base: str = "https://api.github.com"

    def identity(self, tag: str) -> str:
        # Fulcio's SAN for a GitHub Actions run is the job's workflow ref.
        return "https://github.com/%s/%s@refs/tags/%s" % (self.repo, self.workflow, tag)


@dataclass(frozen=True)
class SignedImage:
    name: str  # mc-backend
    ref: str  # ghcr.io/argyelan-ai/mc-backend:1.2.3 (what compose pulls)
    digest: str  # sha256:… of the signed index


@dataclass(frozen=True)
class Verification:
    tag: str
    version: str
    commit: str
    arch: str
    images: Tuple[SignedImage, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict:
        return {
            "ok": True,
            "tag": self.tag,
            "version": self.version,
            "commit": self.commit,
            "arch": self.arch,
            "images": {i.name: {"ref": i.ref, "digest": i.digest} for i in self.images},
        }


# ------------------------------------------------------------------ plumbing


def default_http_get(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "mission-control-update-verify",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:  # noqa: S310 (fixed https/file base)
            status = getattr(resp, "status", None) or 200
            if status >= 300:
                raise HttpError(status)
            return resp.read()
    except urllib.error.HTTPError as e:
        raise HttpError(e.code) from None
    except urllib.error.URLError as e:
        # file:// "API" in tests: a missing file is a 404, like GitHub.
        if isinstance(e.reason, FileNotFoundError):
            raise HttpError(404) from None
        raise OSError(str(e.reason)) from None


def default_run(argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    p = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout)
    return p.returncode, p.stdout, p.stderr


def _call(run: Runner, argv: Sequence[str], timeout: int) -> Tuple[int, str, str]:
    try:
        return run(argv, timeout)
    except subprocess.TimeoutExpired:
        raise VerifyError("docker_unavailable", "%s timed out after %ss" % (argv[0], timeout)) from None
    except OSError as e:
        raise VerifyError("docker_unavailable", "cannot run %s: %s" % (argv[0], e)) from None


def _last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1][:300] if lines else ""


def _check_tag(tag: str) -> Tuple[int, int, int]:
    m = STABLE_TAG.match(tag or "") if isinstance(tag, str) and "\n" not in tag else None
    if not m:
        raise VerifyError("invalid_tag", "not a stable release tag (vX.Y.Z): %r" % (tag,))
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


# ------------------------------------------------------------------- release


def _get_json(url: str, http_get: HttpGet) -> object:
    try:
        body = http_get(url)
    except HttpError:
        raise
    except Exception as e:  # network, DNS, TLS, timeout — all the same: red
        raise VerifyError(
            "release_api_unavailable",
            "release list not available (%s); try again later" % (e,),
        ) from None
    try:
        return json.loads(body)
    except (ValueError, TypeError):
        raise VerifyError("release_api_unavailable", "release API answered something that is not JSON") from None


def _api_error(e: HttpError, what: str) -> VerifyError:
    if e.status == 404:
        return VerifyError("release_not_found", "%s is not a published GitHub release" % what)
    return VerifyError(
        "release_api_unavailable",
        "release list not available (GitHub answered %d); try again later" % e.status,
    )


def _check_release_json(rel: object, tag: str) -> dict:
    if not isinstance(rel, dict) or "tag_name" not in rel:
        raise VerifyError("release_api_unavailable", "release API answered an unexpected shape")
    if rel.get("tag_name") != tag:
        raise VerifyError("release_mismatch", "API returned release %r for %s" % (rel.get("tag_name"), tag))
    if rel.get("draft") is not False or not rel.get("published_at"):
        raise VerifyError("release_not_published", "%s is a draft, not a published release" % tag)
    if rel.get("prerelease") is not False:
        raise VerifyError("release_prerelease", "%s is a pre-release; only stable releases are offered" % tag)
    return rel


def check_release(tag: str, *, http_get: HttpGet = default_http_get, trust: Trust = Trust()) -> dict:
    """Step 1: ``tag`` is a published, stable release. API only, no fallback."""
    _check_tag(tag)
    url = "%s/repos/%s/releases/tags/%s" % (trust.api_base.rstrip("/"), trust.repo, tag)
    try:
        rel = _get_json(url, http_get)
    except HttpError as e:
        raise _api_error(e, tag) from None
    return _check_release_json(rel, tag)


def latest_release_tag(*, http_get: HttpGet = default_http_get, trust: Trust = Trust()) -> str:
    """The newest published stable release (GitHub's ``releases/latest``)."""
    url = "%s/repos/%s/releases/latest" % (trust.api_base.rstrip("/"), trust.repo)
    try:
        rel = _get_json(url, http_get)
    except HttpError as e:
        raise _api_error(e, "latest") from None
    tag = rel.get("tag_name") if isinstance(rel, dict) else None
    _check_tag(tag)  # type: ignore[arg-type]
    return _check_release_json(rel, tag)["tag_name"]  # type: ignore[arg-type]


def check_forward(current: str, target_tag: str) -> None:
    """Only forward: the target must be strictly newer than what runs."""
    target = _check_tag(target_tag)
    m = VERSION_RE.match(current or "")
    if not m:
        raise VerifyError("not_forward", "installed version %r is not a release; update in the terminal" % (current,))
    cur = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if target <= cur:
        raise VerifyError("not_forward", "%s is not newer than the installed %s" % (target_tag, current))


def tag_commit(repo_path, tag: str, *, run: Runner = default_run) -> str:
    """Step 2: the commit the tag points to, from the local checkout."""
    _check_tag(tag)
    rc, out, err = _call(run, ["git", "-C", str(repo_path), "rev-parse", "--verify", "--quiet",
                               "refs/tags/%s^{commit}" % tag], DOCKER_TIMEOUT)
    sha = out.strip()
    if rc != 0 or not SHA1.match(sha):
        raise VerifyError("tag_missing", "tag %s is not in the local checkout (fetch first)" % tag)
    return sha


# ----------------------------------------------------------------- signature


def cosign_verify_argv(image: str, tag: str, commit: str, trust: Trust = Trust()) -> List[str]:
    """``docker run`` of the pinned cosign, bound to repo, workflow, tag, commit.

    No ``--certificate-identity-regexp``: a pattern would let any workflow or
    any ref of the repository pass.
    """
    _check_tag(tag)
    if not SHA1.match(commit or ""):
        raise VerifyError("tag_missing", "not a commit id: %r" % (commit,))
    return [
        "docker", "run", "--rm", COSIGN_IMAGE, "verify",
        "--certificate-identity", trust.identity(tag),
        "--certificate-oidc-issuer", OIDC_ISSUER,
        "--certificate-github-workflow-repository", trust.repo,
        "--certificate-github-workflow-ref", "refs/tags/" + tag,
        "--certificate-github-workflow-sha", commit,
        "--certificate-github-workflow-trigger", "push",
        "%s/%s:%s" % (trust.registry, image, tag[1:]),
    ]


def _strip_ref(ref: str) -> str:
    """``host/ns/name[:tag][@digest]`` → ``host/ns/name``."""
    ref = ref.split("@", 1)[0]
    last = ref.rsplit("/", 1)
    if ":" in last[-1]:
        ref = ref.rsplit(":", 1)[0]
    return ref


def parse_cosign_output(stdout: str, repository: str) -> str:
    """The one digest cosign verified, for exactly ``repository``.

    Handles cosign v3's bundle output and the older payload output (both
    carry ``critical.image.docker-manifest-digest``). More than one digest is
    ambiguous and refused.
    """
    try:
        payloads = json.loads(stdout)
    except (ValueError, TypeError):
        raise VerifyError("signature_invalid", "cosign printed no verification result") from None
    if not isinstance(payloads, list) or not payloads:
        raise VerifyError("signature_invalid", "cosign verified no signature")
    digests = set()
    for p in payloads:
        crit = p.get("critical") if isinstance(p, dict) else None
        try:
            ref = crit["identity"]["docker-reference"]
            digest = crit["image"]["docker-manifest-digest"]
        except (TypeError, KeyError):
            raise VerifyError("signature_invalid", "cosign result has no image digest") from None
        if _strip_ref(str(ref)) != repository:
            raise VerifyError("signature_invalid", "signature is for %s, not %s" % (ref, repository))
        if not DIGEST_RE.match(str(digest)):
            raise VerifyError("signature_invalid", "signature names no valid digest: %r" % (digest,))
        digests.add(digest)
    if len(digests) != 1:
        raise VerifyError("signature_invalid", "signatures name %d different digests" % len(digests))
    return digests.pop()


def verify_signature(image: str, tag: str, commit: str, *, run: Runner = default_run,
                     trust: Trust = Trust()) -> SignedImage:
    """Step 3 for one image: returns the signed digest."""
    argv = cosign_verify_argv(image, tag, commit, trust)
    rc, out, err = _call(run, argv, COSIGN_TIMEOUT)
    if rc != 0:
        raise VerifyError(
            "signature_invalid",
            "%s:%s is not signed by %s for %s (cosign exit %d: %s)"
            % (image, tag[1:], trust.workflow, tag, rc, _last_line(err) or "no detail"),
        )
    digest = parse_cosign_output(out, "%s/%s" % (trust.registry, image))
    return SignedImage(name=image, ref="%s/%s:%s" % (trust.registry, image, tag[1:]), digest=digest)


# -------------------------------------------------------------- architecture


def normalise_arch(raw: str) -> str:
    return ARCH_ALIASES.get((raw or "").strip().lower(), (raw or "").strip().lower())


def host_arch(run: Runner = default_run) -> str:
    """This machine's Docker architecture (``arm64`` / ``amd64``)."""
    rc, out, err = _call(run, ["docker", "version", "--format", "{{.Server.Arch}}"], DOCKER_TIMEOUT)
    arch = normalise_arch(out)
    if rc != 0 or not arch:
        raise VerifyError("docker_unavailable", "cannot read the Docker architecture: %s" % _last_line(err))
    return arch


def check_platform(signed: SignedImage, arch: str, *, run: Runner = default_run) -> None:
    """Step 4: the SIGNED index (by digest, not the movable tag) has ``arch``."""
    repo = signed.ref.rsplit(":", 1)[0]
    rc, out, err = _call(run, ["docker", "manifest", "inspect", "%s@%s" % (repo, signed.digest)], DOCKER_TIMEOUT)
    if rc != 0:
        raise VerifyError("arch_missing", "cannot read the image index of %s: %s" % (signed.name, _last_line(err)))
    try:
        index = json.loads(out)
        platforms = {
            (m["platform"].get("os"), normalise_arch(m["platform"].get("architecture", "")))
            for m in index.get("manifests", [])
            if isinstance(m, dict) and isinstance(m.get("platform"), dict)
        }
    except (ValueError, AttributeError, TypeError):
        raise VerifyError("arch_missing", "image index of %s is unreadable" % signed.name) from None
    if ("linux", arch) not in platforms:
        have = ", ".join(sorted("%s/%s" % p for p in platforms if p[0] == "linux")) or "none"
        raise VerifyError("arch_missing", "%s has no linux/%s image (has: %s)" % (signed.name, arch, have))


# --------------------------------------------------------------- whole steps


def verify_release(tag: str, *, repo_path, run: Runner = default_run, http_get: HttpGet = default_http_get,
                   trust: Trust = Trust(), arch: Optional[str] = None) -> Verification:
    """Steps 1-4. Runs before the pull; changes nothing on the machine."""
    check_release(tag, http_get=http_get, trust=trust)
    commit = tag_commit(repo_path, tag)
    signed = tuple(verify_signature(img, tag, commit, run=run, trust=trust) for img in trust.images)
    arch = arch or host_arch(run)
    for s in signed:
        check_platform(s, arch, run=run)
    return Verification(tag=tag, version=tag[1:], commit=commit, arch=arch, images=signed)


def verify_pulled(v: Verification, *, run: Runner = default_run) -> Dict[str, str]:
    """Steps 5-7 after the pull. Returns ``{image name: local image id}``."""
    ids = {}
    for img in v.images:
        rc, out, err = _call(run, ["docker", "image", "inspect", "--format", "{{json .}}", img.ref], DOCKER_TIMEOUT)
        if rc != 0:
            raise VerifyError("digest_mismatch", "%s is not on this machine: %s" % (img.ref, _last_line(err)))
        try:
            data = json.loads(out)
            if isinstance(data, list):
                data = data[0]
        except (ValueError, IndexError):
            raise VerifyError("digest_mismatch", "cannot read %s" % img.ref) from None
        repo = img.ref.rsplit(":", 1)[0]
        wanted = "%s@%s" % (repo, img.digest)
        if wanted not in (data.get("RepoDigests") or []):
            raise VerifyError(
                "digest_mismatch",
                "%s is not the signed build %s (has %s)" % (img.ref, img.digest, ", ".join(data.get("RepoDigests") or []) or "none"),
            )
        labels = (data.get("Config") or {}).get("Labels") or {}
        if labels.get(REVISION_LABEL) != v.commit:
            raise VerifyError(
                "revision_mismatch",
                "%s was built from %s, not from %s (%s)" % (img.ref, labels.get(REVISION_LABEL) or "an unknown commit", v.commit, v.tag),
            )
        if normalise_arch(data.get("Architecture", "")) != v.arch or data.get("Os") != "linux":
            raise VerifyError(
                "arch_missing",
                "%s is %s/%s, this machine needs linux/%s" % (img.ref, data.get("Os"), data.get("Architecture"), v.arch),
            )
        image_id = data.get("Id") or ""
        if not DIGEST_RE.match(image_id):
            raise VerifyError("digest_mismatch", "%s has no image id" % img.ref)
        ids[img.name] = image_id
    return ids


def verify_containers(image_ids: Dict[str, str], containers: Dict[str, str], *, run: Runner = default_run) -> None:
    """After ``up``: each container (name → image name) runs the verified image."""
    wrong = []
    for container, image in sorted(containers.items()):
        rc, out, err = _call(run, ["docker", "inspect", "--format", "{{.Image}}", container], DOCKER_TIMEOUT)
        if rc != 0 or out.strip() != image_ids.get(image):
            wrong.append(container)
    if wrong:
        raise VerifyError("container_image_mismatch", "not running the verified image: " + ", ".join(wrong))


# ----------------------------------------------------------------------- CLI


def _trust_from(args) -> Trust:
    kw = {}
    if args.repo:
        kw["repo"] = args.repo
    if args.registry:
        kw["registry"] = args.registry
    if args.api_base:
        kw["api_base"] = args.api_base
    if args.images:
        kw["images"] = tuple(args.images.split(","))
    return Trust(**kw)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=["release", "signature", "latest"])
    ap.add_argument("tag", nargs="?")
    ap.add_argument("--repo-path", default=".", help="MC checkout (release: reads the tag commit)")
    ap.add_argument("--commit", help="signature: the tag's commit")
    ap.add_argument("--repo", help="owner/name (acceptance from a fork only)")
    ap.add_argument("--registry", help="image registry prefix (acceptance from a fork only)")
    ap.add_argument("--images", help="comma-separated image names (default: mc-backend,mc-frontend)")
    ap.add_argument("--api-base", help="GitHub API base URL (tests: file://)")
    args = ap.parse_args(argv)
    trust = _trust_from(args)
    try:
        if args.command == "latest":
            result = {"ok": True, "tag": latest_release_tag(trust=trust)}
        elif args.command == "release":
            result = verify_release(args.tag or "", repo_path=args.repo_path, trust=trust).as_dict()
        else:
            if not args.commit:
                raise VerifyError("tag_missing", "signature needs --commit")
            signed = [verify_signature(img, args.tag or "", args.commit, trust=trust) for img in trust.images]
            result = {"ok": True, "tag": args.tag, "commit": args.commit,
                      "images": {s.name: {"ref": s.ref, "digest": s.digest} for s in signed}}
    except VerifyError as e:
        print(json.dumps({"ok": False, "code": e.code, "message": str(e)}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
