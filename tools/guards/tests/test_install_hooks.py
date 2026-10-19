"""Tests for tools/guards/install-hooks.py — stdlib only (CI runs it with plain python3).

Run: python3 -m unittest tools/guards/tests/test_install_hooks.py
"""
import importlib.util
import io
import json
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
SPEC = importlib.util.spec_from_file_location("install_hooks", os.path.join(HERE, "..", "install-hooks.py"))
ih = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ih)

FOREIGN_PRE = {"matcher": "Bash", "hooks": [{"type": "command", "command": "python3 /opt/leak-scan.py"}]}
FOREIGN_POST = {"matcher": "Edit|Write", "hooks": [{"type": "command", "command": "bash fmt.sh"}]}


class InstallHooks(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = os.path.join(self.tmp.name, "repo")
        os.makedirs(self.repo)
        subprocess.run(["git", "init", "-q", self.repo], check=True)
        self.settings = os.path.join(self.tmp.name, "settings.json")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *extra, settings=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = ih.main(["--repo", self.repo, "--settings", settings or self.settings, *extra])
        return rc, out.getvalue(), err.getvalue()

    def write(self, data):
        with open(self.settings, "w") as f:
            json.dump(data, f)

    def text(self):
        with open(self.settings) as f:
            return f.read()

    def read(self):
        with open(self.settings) as f:
            return json.load(f)

    def ours(self, data, event):
        return [h for e in data["hooks"].get(event, []) for h in e["hooks"] if "tools/guards" in h["command"]]

    def test_dry_run_writes_nothing(self):
        self.write({"hooks": {"PreToolUse": [FOREIGN_PRE]}})
        before = self.text()
        rc, out, _ = self.run_main()
        self.assertEqual(rc, 0)
        self.assertIn("would", out)
        self.assertEqual(self.text(), before)

    def test_apply_adds_pre_and_post_and_keeps_foreign_hooks(self):
        self.write({"model": "x", "hooks": {"PreToolUse": [FOREIGN_PRE], "PostToolUse": [FOREIGN_POST]}})
        rc, _, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        d = self.read()
        self.assertEqual(d["model"], "x")
        self.assertIn(FOREIGN_PRE, d["hooks"]["PreToolUse"])
        self.assertIn(FOREIGN_POST, d["hooks"]["PostToolUse"])
        pre, post = self.ours(d, "PreToolUse"), self.ours(d, "PostToolUse")
        self.assertEqual(len(pre), 1)
        self.assertEqual(len(post), 1)
        self.assertIn("bash $d/bash-hook.sh", pre[0]["command"])
        self.assertIn("bash $d/post-push-hook.sh", post[0]["command"])
        self.assertIn(f"r={self.repo};", pre[0]["command"])
        # a backup of the old file exists
        self.assertTrue(any(n.startswith("settings.json.bak-guards-") for n in os.listdir(self.tmp.name)))

    def test_second_apply_is_a_no_op(self):
        self.write({})
        self.run_main("--apply")
        first = self.text()
        rc, out, _ = self.run_main("--apply")
        self.assertEqual(rc, 0)
        self.assertIn("nothing to change", out)
        self.assertEqual(self.text(), first)

    def test_existing_identical_entry_is_recognised(self):
        # the shape already written by hand into a project settings file
        entry = {"matcher": "Bash", "hooks": [{"type": "command", "timeout": 60,
                                               "command": ih.launcher(self.repo, "bash-hook.sh")}]}
        self.write({"hooks": {"PreToolUse": [FOREIGN_PRE, entry]}})
        self.run_main("--apply")
        d = self.read()
        self.assertEqual(len(self.ours(d, "PreToolUse")), 1)
        self.assertEqual(len(self.ours(d, "PostToolUse")), 1)

    def test_outdated_entry_is_updated_not_duplicated(self):
        old = {"matcher": "Bash", "hooks": [{"type": "command", "command": "bash /old/tools/guards/bash-hook.sh"}]}
        self.write({"hooks": {"PreToolUse": [old]}})
        self.run_main("--apply")
        pre = self.ours(self.read(), "PreToolUse")
        self.assertEqual(len(pre), 1)
        self.assertEqual(pre[0]["command"], ih.launcher(self.repo, "bash-hook.sh"))

    def test_check_reports_missing(self):
        self.write({})
        rc, _, _ = self.run_main("--check")
        self.assertEqual(rc, 1)
        self.run_main("--apply")
        rc, _, _ = self.run_main("--check")
        self.assertEqual(rc, 0)

    def test_broken_json_is_never_overwritten(self):
        with open(self.settings, "w") as f:
            f.write("{ not json")
        rc, _, err = self.run_main("--apply")
        self.assertEqual(rc, 2)
        self.assertIn("nothing written", err)
        self.assertEqual(self.text(), "{ not json")

    def test_missing_file_is_created(self):
        path = os.path.join(self.tmp.name, "sub", "settings.json")
        rc, _, _ = self.run_main("--apply", settings=path)
        self.assertEqual(rc, 0)
        with open(path) as f:
            self.assertEqual(len(self.ours(json.load(f), "PostToolUse")), 1)

    def test_launcher_is_a_noop_until_the_script_is_on_origin_main(self):
        # empty repo: no origin/main → the hook must exit 0 and never block
        cmd = ih.launcher(self.repo, "post-push-hook.sh")
        r = subprocess.run(["bash", "-c", cmd.removeprefix("bash -c ").strip("'")],
                           input=b"{}", capture_output=True, env={**os.environ, "HOME": self.tmp.name})
        self.assertEqual(r.returncode, 0)

    def test_unsafe_repo_path_is_refused(self):
        rc = None
        err = io.StringIO()
        with redirect_stderr(err), redirect_stdout(io.StringIO()):
            rc = ih.main(["--repo", "/tmp/a b", "--settings", self.settings])
        self.assertEqual(rc, 2)


if __name__ == "__main__":
    unittest.main()
