#!/usr/bin/env python3
"""Stop a release when the `release` environment has no required reviewer.

GitHub creates an environment that a workflow names but that does not exist
yet — without any protection. A missing or unprotected `release` environment
would therefore let `release.yml` sign and promote without anyone's approval.
The signing job runs this check as its first step: no required reviewer, no
signature (ADR-094 decision 5, docs/setup/releasing.md).

    release_gate_check.py                      # asks the GitHub API (GH_TOKEN, GITHUB_REPOSITORY)
    release_gate_check.py --from-file env.json # tests

Exit 0 = gated, 1 = not gated (the reason is printed). Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from typing import List, Optional

ENVIRONMENT = "release"


def problems(env: Optional[dict]) -> List[str]:
    """Why ``env`` (the API's environment object) is not a gate; [] = gated."""
    if not isinstance(env, dict):
        return ["environment %r does not exist" % ENVIRONMENT]
    found = []
    for rule in env.get("protection_rules") or []:
        if isinstance(rule, dict) and rule.get("type") == "required_reviewers":
            found.extend(rule.get("reviewers") or [])
    if not found:
        return ["environment %r has no required reviewer — anyone who can push a tag would release unapproved"
                % ENVIRONMENT]
    return []


def fetch(repo: str, token: str, api: str = "https://api.github.com") -> Optional[dict]:
    req = urllib.request.Request(
        "%s/repos/%s/environments/%s" % (api.rstrip("/"), repo, ENVIRONMENT),
        headers={"Accept": "application/vnd.github+json", "User-Agent": "mission-control-release-gate",
                 **({"Authorization": "Bearer " + token} if token else {})},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:  # noqa: S310 (fixed https base)
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--from-file", help="read the environment JSON from a file instead of the API")
    args = ap.parse_args(argv)
    try:
        if args.from_file:
            try:
                with open(args.from_file) as fh:
                    env = json.load(fh)
            except FileNotFoundError:
                env = None
        else:
            env = fetch(os.environ["GITHUB_REPOSITORY"], os.environ.get("GH_TOKEN", ""))
    except Exception as e:  # unreadable = not proven gated = stop
        print("::error::cannot read the release environment: %s" % e)
        return 1
    found = problems(env)
    for p in found:
        print("::error::release gate missing: %s. See docs/setup/releasing.md." % p)
    if not found:
        print("release gate ok: environment %r requires a reviewer" % ENVIRONMENT)
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main())
