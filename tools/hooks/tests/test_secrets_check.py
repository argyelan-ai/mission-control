"""secrets-check.sh must BLOCK (exit 2) a commit / push that would publish a
credential, and must never echo the value. Fake secrets are assembled at
runtime so this file itself never contains one (gitleaks scans the tree)."""
from conftest import commit, git, run_hook

FAKE_GH = "gh" + "p_" + "A1b2C3d4" * 5          # 40 chars after the prefix
FAKE_ANT = "sk-" + "ant-" + "x9Y8z7W6" * 4
FAKE_KEY = "-----BEGIN " + "OPENSSH PRIVATE KEY-----"


def stage(repo, rel, text):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    git(repo, "add", "-f", rel)  # -f: a global gitignore may hide .env*


def test_staged_token_blocks_commit_and_value_is_not_printed(repo):
    stage(repo, "app/config.py", f'TOKEN = "{FAKE_GH}"\n')
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m wip")
    assert r.returncode == 2, r.stderr
    assert "app/config.py: GitHub token" in r.stderr
    assert FAKE_GH not in r.stderr + r.stdout


def test_private_key_and_anthropic_key_block(repo):
    stage(repo, "k.txt", FAKE_KEY + "\n")
    stage(repo, "a.py", f"KEY='{FAKE_ANT}'\n")
    r = run_hook("secrets-check.sh", f"cd {repo} && git commit -m wip")
    assert r.returncode == 2
    assert "private key block" in r.stderr and "Anthropic key" in r.stderr


def test_clean_commit_passes(repo):
    stage(repo, "app/config.py", 'TOKEN = os.environ["GITHUB_TOKEN"]\n')
    assert run_hook("secrets-check.sh", f"git -C {repo} commit -m ok").returncode == 0


def test_staged_env_file_blocks_but_example_passes(repo):
    stage(repo, ".env.example", "A=\n")
    assert run_hook("secrets-check.sh", f"git -C {repo} commit -m ok").returncode == 0
    stage(repo, "deploy/.env", "A=1\n")
    r = run_hook("secrets-check.sh", f"git -C {repo} commit -m bad")
    assert r.returncode == 2
    assert "deploy/.env: secret-bearing file" in r.stderr


def test_push_scans_branch_diff_against_origin_main(repo):
    commit(repo, "notes.md", f"oops {FAKE_GH}\n")
    r = run_hook("secrets-check.sh", f"git -C {repo} push")
    assert r.returncode == 2
    assert "notes.md: GitHub token" in r.stderr


def test_removed_secret_line_does_not_block(repo):
    commit(repo, "notes.md", f"oops {FAKE_GH}\n")
    git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    commit(repo, "notes.md", "clean now\n")
    assert run_hook("secrets-check.sh", f"git -C {repo} push").returncode == 0


def test_other_commands_untouched(repo):
    stage(repo, "app/config.py", f'TOKEN = "{FAKE_GH}"\n')
    assert run_hook("secrets-check.sh", f"git -C {repo} status").returncode == 0
    assert run_hook("secrets-check.sh", "cat app/config.py", cwd=repo).returncode == 0
