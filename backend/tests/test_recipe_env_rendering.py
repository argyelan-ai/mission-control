"""Declarative engine tuning: recipe ``env`` → compose override (PR 8).

The four values that made DeepSeek V4 Flash fit on the Spark lived in a
hand-written ``compose.tuning.yaml`` on the box — one ``git clone`` away from
being lost, and invisible to everyone who did not SSH in. Since PR 8 they are a
column on the recipe, rendered into the ``environment:`` block of the same
``compose.override.yaml`` that already carries the container name and the
``mc.runtime.slug`` label.

Two properties are load-bearing and therefore tested hardest:

* the rendered override is **valid YAML with the right values**, not just a
  string containing the right substrings — the file is produced by ``printf``
  inside a shell command, which is a rich source of quoting bugs that only
  show up hours into a weight download,
* an existing ``compose.tuning.yaml`` **keeps working**. Mark's box has one
  right now, and a deploy that silently ignored it would undo a live-proven
  configuration.
"""
import json
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from sqlmodel import select

from app.models.local_recipe import LocalRecipe
from app.services import launch_template as lt
from app.services.local_registry import (
    RecipeSpec,
    _apply_update,
    _row_from_spec,
    seed_local_recipes,
)

SEED_FILE = Path(__file__).resolve().parents[1] / "config" / "local-recipes.json"
SPARKINFER_SLUG = "deepseek-v4-flash-sparkinfer"

SPARK_ENV = {
    "VERIFY_MODEL_CHECKSUMS": "0",
    "MODE": "mtp0",
    "GPU_MEMORY_UTILIZATION": "0.895",
    "MAX_NUM_SEQS": "2",
}


def _seed_entry(slug: str = SPARKINFER_SLUG) -> dict:
    entries = json.loads(SEED_FILE.read_text(encoding="utf-8"))
    match = [e for e in entries if e["slug"] == slug]
    assert match, f"{slug} missing from the seed file"
    return match[0]


def _render_override(command: str, *, tuning_present: bool) -> dict:
    """Actually run the ``printf`` half of a rendered command in a temp dir.

    Substring assertions would pass on a command that writes broken YAML. This
    executes the real thing and parses the result, which is the only way to
    know the file the box ends up with is the file we meant.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        (tmp_path / "compose.yaml").write_text("services:\n  deepseek-v4-flash: {}\n")
        if tuning_present:
            (tmp_path / "compose.tuning.yaml").write_text(
                "services:\n  deepseek-v4-flash:\n    shm_size: 32gb\n"
            )
        # Only the printf: there is no docker, no git and no clone target here.
        match = re.search(r"printf '.*?' > compose\.override\.yaml", command, re.S)
        assert match, "the command no longer writes the override with printf"
        printf_part = match.group(0)
        subprocess.run(
            ["bash", "-c", printf_part], cwd=tmp_path, check=True, capture_output=True
        )
        return yaml.safe_load((tmp_path / "compose.override.yaml").read_text())


# ── The renderer itself ──────────────────────────────────────────────────────


def test_env_renders_as_a_sorted_printf_ready_environment_block():
    block = lt.render_compose_env({"B_KEY": "2", "A_KEY": "1"})

    assert block == "    environment:\\n      - A_KEY=1\\n      - B_KEY=2\\n"
    # printf-ready means literal backslash-n, not real newlines: this is spliced
    # into a `printf '<format>'` and a real newline would end the argument.
    assert "\n" not in block


def test_no_env_renders_to_nothing():
    """A recipe without tuning must produce an override with NO environment
    block at all — not an empty one, which compose reads as `environment: null`."""
    assert lt.render_compose_env(None) == ""
    assert lt.render_compose_env({}) == ""


@pytest.mark.parametrize(
    "value",
    ["it's", 'say "hi"', "100%", "back\\slash", "two\nlines"],
    ids=["quote", "dquote", "percent", "backslash", "newline"],
)
def test_a_value_that_could_break_out_of_the_printf_is_rejected(value):
    """Sabotage: every one of these would either terminate the format string,
    act as a printf directive, or corrupt the YAML. Rejecting loudly beats
    discovering a mangled override on the box."""
    with pytest.raises(ValueError, match="unzulässige Zeichen"):
        lt.render_compose_env({"KEY": value})


@pytest.mark.parametrize("key", ["2FAST", "MY-KEY", "MY KEY", ""])
def test_an_invalid_env_key_is_rejected(key):
    with pytest.raises(ValueError, match="env-Schlüssel"):
        lt.render_compose_env({key: "1"})


def test_env_yaml_is_optional_while_every_other_placeholder_is_not():
    """``{env_yaml}`` may render to nothing; a missing port may not. A
    half-rendered docker command is not something to find out about remotely."""
    assert lt.render_launch_template("a {env_yaml}b", {"env_yaml": ""}) == "a b"
    with pytest.raises(ValueError, match="Kein Wert für Platzhalter: port"):
        lt.render_launch_template("serve --port {port}", {"port": None})


# ── End to end through the real seed entry ───────────────────────────────────


def test_the_sparkinfer_launch_writes_a_valid_override_with_the_tuning():
    entry = _seed_entry()
    command = lt.build_launch_command(
        engine=entry["engine"],
        model_identifier=entry["model_identifier"],
        slug=entry["slug"],
        port=8000,
        launch_template=entry["launch_template"],
        env=entry["env"],
    )

    override = _render_override(command, tuning_present=False)
    service = override["services"]["deepseek-v4-flash"]

    # The two strings the whole lifecycle depends on survive next to the env.
    assert service["container_name"] == f"mc-{SPARKINFER_SLUG}"
    assert service["labels"] == [f"mc.runtime.slug={SPARKINFER_SLUG}"]
    assert sorted(service["environment"]) == sorted(
        f"{k}={v}" for k, v in SPARK_ENV.items()
    )


def test_the_same_override_is_written_by_the_install():
    """An install that came up with different tuning than the launch would be a
    stack that only works until it is restarted."""
    entry = _seed_entry()
    command = lt.build_install_command(
        slug=entry["slug"],
        install_template=entry["install_template"],
        port=8000,
        model_identifier=entry["model_identifier"],
        env=entry["env"],
    )

    override = _render_override(command, tuning_present=False)
    assert override["services"]["deepseek-v4-flash"]["environment"]


def test_a_hand_written_compose_tuning_yaml_keeps_being_loaded():
    """Mark's box has one right now. The generated command must load it
    ADDITIONALLY — before the override, so MC's name/label/env win while
    everything else the operator tuned survives."""
    entry = _seed_entry()
    command = lt.build_launch_command(
        engine=entry["engine"],
        model_identifier=entry["model_identifier"],
        slug=entry["slug"],
        port=8000,
        launch_template=entry["launch_template"],
        env=entry["env"],
    )

    assert "[ -f compose.tuning.yaml ]" in command
    assert 'TUNING="-f compose.tuning.yaml"' in command
    assert 'ARGS="-f compose.yaml $TUNING -f compose.override.yaml"' in command

    # And the file it produces is unaffected by whether the tuning file exists.
    with_tuning = _render_override(command, tuning_present=True)
    without = _render_override(command, tuning_present=False)
    assert with_tuning == without


def test_the_tuning_detection_does_not_break_the_command_chain():
    """Sabotage: the `[ -f … ]` test returns 1 when the file is absent. Written
    with `&&` that would abort the whole launch on every box WITHOUT a tuning
    file — i.e. on every box but Mark's."""
    entry = _seed_entry()
    command = lt.build_launch_command(
        engine=entry["engine"],
        model_identifier=entry["model_identifier"],
        slug=entry["slug"],
        port=8000,
        launch_template=entry["launch_template"],
        env=entry["env"],
    )
    # Replace the compose call with an echo, then run the whole thing for real.
    harness = command.replace("docker compose $ARGS up -d", "echo LAUNCHED $ARGS")

    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "deepseek-v4-flash-0731-spark-sparkinfer"
        target.mkdir(parents=True)
        proc = subprocess.run(
            ["bash", "-c", harness.replace(lt.DEFAULT_SRC_DIR, tmp)],
            capture_output=True, text=True,
        )

    assert proc.returncode == 0, proc.stderr
    # $TUNING expands to nothing and the shell collapses the gap — the point
    # is that both files are still there and the command ran at all.
    assert "LAUNCHED -f compose.yaml -f compose.override.yaml" in proc.stdout


def test_a_recipe_without_env_still_renders_a_clean_override():
    """Every other compose recipe (and any imported one) has env = NULL."""
    entry = _seed_entry()
    command = lt.build_launch_command(
        engine=entry["engine"],
        model_identifier=entry["model_identifier"],
        slug=entry["slug"],
        port=8000,
        launch_template=entry["launch_template"],
        env=None,
    )

    override = _render_override(command, tuning_present=False)
    service = override["services"]["deepseek-v4-flash"]
    assert "environment" not in service
    assert service["container_name"] == f"mc-{SPARKINFER_SLUG}"


# ── Persistence ──────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_env_survives_the_seed_roundtrip(session):
    await seed_local_recipes(session)

    row = (
        await session.exec(select(LocalRecipe).where(LocalRecipe.slug == SPARKINFER_SLUG))
    ).first()
    assert row is not None
    assert row.env == SPARK_ENV
    assert row.gb10_validated is True


def test_a_refresh_updates_env_but_an_empty_map_becomes_null():
    """``env`` follows the ordinary update rules (unlike ``enabled``), and an
    empty map is stored as NULL so "no tuning" has exactly one representation."""
    spec = RecipeSpec(
        slug="x", display_name="X", engine="vllm_docker", model_identifier="m",
        env={"A": "1"},
    )
    row = _row_from_spec(spec)
    assert row.env == {"A": "1"}

    assert _apply_update(row, spec.model_copy(update={"env": {"A": "2"}})) is True
    assert row.env == {"A": "2"}

    assert _apply_update(row, spec.model_copy(update={"env": {}})) is True
    assert row.env is None


def test_a_registry_serving_unquoted_numbers_is_rejected_not_guessed():
    """Sabotage: how a float is spelled on the way into an environment variable
    is a decision — `0.895` and `0.8949999999` are different commands. The
    entry is skipped with a reason rather than silently deployed."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        RecipeSpec(
            slug="x", display_name="X", engine="vllm_docker", model_identifier="m",
            env={"GPU_MEMORY_UTILIZATION": 0.895},
        )
