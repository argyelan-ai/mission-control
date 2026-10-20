"""Live test of tools/guards/post-push-hook.sh against real git (no network).

The fixtures test the decision with a captured proof. This test runs the hook the
way Claude Code does — hook JSON on stdin, block mode — against a real bare
remote that is one commit behind HEAD. It covers the command shapes that slipped
through before: the incident command without remote or refspec, `-c k=v` before
push, a variable path, an upstream that is not `origin`, an unknown remote.

Run: python3 -m unittest tools/guards/tests/test_post_push_live.py
"""
import json
import os
import subprocess
import tempfile
import unittest

HOOK = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "post-push-hook.sh")


def git(*args, cwd=None):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


class PostPushLive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        t = self.tmp.name
        self.home = os.path.join(t, "home")
        os.makedirs(self.home)
        self.log = os.path.join(t, "hook-log.jsonl")
        self.env = {**os.environ, "HOME": self.home, "GUARD_HOOK_LOG": self.log,
                    "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        self.env.pop("GUARD_HOOK_MODE", None)
        self.remote = os.path.join(t, "remote.git")
        self.fork = os.path.join(t, "fork.git")
        self.wt = os.path.join(t, "wt")
        self.elsewhere = os.path.join(t, "elsewhere")
        os.makedirs(self.elsewhere)
        for bare in (self.remote, self.fork):
            subprocess.run(["git", "init", "-q", "--bare", bare], check=True)
        subprocess.run(["git", "init", "-q", "-b", "feat/x", self.wt], check=True)
        for k, v in (("user.email", "t@example.invalid"), ("user.name", "t")):
            git("config", k, v, cwd=self.wt)
        git("remote", "add", "origin", self.remote, cwd=self.wt)
        git("remote", "add", "fork", self.fork, cwd=self.wt)
        git("commit", "-q", "--allow-empty", "-m", "one", cwd=self.wt)
        git("push", "-q", "origin", "feat/x", cwd=self.wt)
        git("push", "-q", "fork", "feat/x", cwd=self.wt)
        # the push that "failed": HEAD is one commit ahead of both remotes
        git("commit", "-q", "--allow-empty", "-m", "two", cwd=self.wt)

    def tearDown(self):
        self.tmp.cleanup()

    def hook(self, command, cwd=None, mode="block"):
        data = {"session_id": "t", "cwd": cwd or self.wt, "hook_event_name": "PostToolUse",
                "tool_name": "Bash", "tool_input": {"command": command}}
        env = {**self.env, "GUARD_HOOK_MODE": mode}
        r = subprocess.run(["bash", HOOK], input=json.dumps(data), env=env,
                           capture_output=True, text=True, timeout=60)
        return r.returncode, r.stderr

    def log_lines(self):
        if not os.path.exists(self.log):
            return []
        with open(self.log) as f:
            return [json.loads(x) for x in f if x.strip()]

    # --- the shapes that were missed -------------------------------------------
    def test_incident_command_without_remote(self):
        rc, err = self.hook("git push --force-with-lease 2>&1 | tail -1")
        self.assertEqual(rc, 2, err)
        self.assertIn("NOT PROVEN", err)

    def test_redirect_is_not_a_refspec(self):
        rc, err = self.hook("git push origin 2>&1 | tail -1")
        self.assertEqual(rc, 2, err)

    def test_config_option_before_push(self):
        rc, err = self.hook(f"git -c push.default=current -C {self.wt} push origin feat/x 2>&1 | tail -1",
                            cwd=self.elsewhere)
        self.assertEqual(rc, 2, err)

    def test_variable_path_assigned_in_command(self):
        rc, err = self.hook(f'WT={self.wt}; git -C "$WT" push origin feat/x 2>&1 | tail -1', cwd=self.elsewhere)
        self.assertEqual(rc, 2, err)
        self.assertIn("NOT PROVEN", err)

    def test_variable_path_unknown_is_a_finding_not_a_skip(self):
        rc, err = self.hook('cd "$WT" && git push', cwd=self.elsewhere)
        self.assertEqual(rc, 2, err)
        self.assertIn("not resolvable", err)

    def test_upstream_remote_is_used_when_no_remote_is_named(self):
        git("branch", "-q", "--set-upstream-to=fork/feat/x", cwd=self.wt)
        git("push", "-q", "origin", "feat/x", cwd=self.wt)  # origin is now current, fork is stale
        rc, err = self.hook("git push 2>&1 | tail -1")
        self.assertEqual(rc, 2, err)
        self.assertIn("fork", err)

    def test_unknown_remote_is_not_provable(self):
        rc, err = self.hook("git push nosuch feat/x")
        self.assertEqual(rc, 2, err)
        self.assertIn("not provable", err)

    # --- no false alarm after a push that arrived ------------------------------
    def test_push_that_arrived_is_green(self):
        git("push", "-q", "origin", "feat/x", cwd=self.wt)
        for cmd in ("git push --force-with-lease 2>&1 | tail -1",
                    "git push origin 2>&1 | tail -1",
                    f"git -c color.ui=never -C {self.wt} push origin HEAD"):
            rc, err = self.hook(cmd)
            self.assertEqual(rc, 0, f"{cmd}: {err}")
        self.assertEqual(self.log_lines(), [])

    # --- shadow data: skips are logged, not silent -----------------------------
    def test_skipped_push_is_logged_in_shadow(self):
        rc, _ = self.hook("git push --dry-run origin feat/x", mode="shadow")
        self.assertEqual(rc, 0)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertEqual(lines[0]["findings"], [])
        self.assertTrue(any("--dry-run" in s for s in lines[0]["skipped"]), lines[0])

    def test_finding_is_logged_in_shadow_and_not_blocking(self):
        rc, _ = self.hook("git push --force-with-lease 2>&1 | tail -1", mode="shadow")
        self.assertEqual(rc, 0)
        lines = self.log_lines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(lines[0]["findings"])


if __name__ == "__main__":
    unittest.main()
