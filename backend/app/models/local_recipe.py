"""LocalRecipe — the curated catalogue of LOCAL models/recipes for GPU boxes.

The counterpart to the provider model catalogue (services/model_catalog.py):
that one answers "which models does Anthropic/Kimi/… offer?", this one answers
"which local models can I run on my own hardware, what fits, what is new?".

CONTRACT — this table is a SHOP WINDOW, never a statement about what runs.
``runtime.model_identifier`` remains the single truth about the live fleet.
A row here says "this recipe exists and roughly needs this much VRAM"; whether
anything is serving it is derived at read time from the runtime rows (see
``routers/local_registry._running_matcher``). No status column lives here on
purpose — a second place that claims to know what runs is a second place that
can be wrong.

Rows arrive from two directions: ``config/local-recipes.json`` (the builtin
seed, ``source_registry == "builtin"``) and remote registries configured via
``settings.local_registry_sources``. Both go through the same upsert, so an
operator's ``enabled = False`` survives every refresh (services/
local_registry.py documents that rule).

All datetimes are timezone-aware (``app.utils.utcnow``). Naive datetimes in
this codebase have repeatedly turned into 500s the moment they met an aware
value in a comparison — the columns are ``DateTime(timezone=True)`` and the
Python defaults must match them.
"""
import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, Text, text
from sqlmodel import Column, Field, SQLModel

from app.utils import utcnow

#: How the model is served. Mirrors the runtime_type vocabulary where they
#: overlap (``vllm_docker``, ``llamacpp_docker``, ``ssh_process``), plus
#: ``sparkrun`` for recipe-based launches on a DGX Spark.
ENGINES = ("sparkrun", "vllm_docker", "llamacpp_docker", "ssh_process")

#: CPU architecture of the target box. ``any`` = runs on both; used as the
#: default so a new entry without an explicit claim is never over-promised as
#: GB10-only nor filtered away on an x86 box.
ARCHS = ("arm64", "x86_64", "any")


class LocalRecipe(SQLModel, table=True):
    __tablename__ = "local_recipes"

    id: uuid.UUID = Field(default_factory=uuid.uuid4, primary_key=True)
    slug: str = Field(index=True, unique=True, max_length=64)
    display_name: str = Field(max_length=128)
    description: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    engine: str = Field(max_length=32)  # see ENGINES
    model_identifier: str = Field(max_length=256)  # HF repo or served model name
    quant: str | None = Field(default=None, max_length=32)  # nvfp4 | fp8 | q4_k_m | …

    # Sizing — deliberately estimates, not measurements. est_weights_gb is the
    # weights on disk/in memory; min_vram_gb adds KV cache + activation
    # headroom, i.e. "a box smaller than this will not hold it". Both nullable:
    # an honest NULL beats an invented number in a UI that people plan with.
    est_weights_gb: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    min_vram_gb: float | None = Field(default=None, sa_column=Column(Float, nullable=True))
    context_len: int | None = None

    arch: str = Field(default="any", max_length=16)  # see ARCHS
    # True only where the recipe has actually been run on a GB10 (DGX Spark).
    # An untested entry stays False — this flag is the difference between
    # "should work" and "we watched it work".
    gb10_validated: bool = Field(
        default=False,
        sa_column=Column(Boolean, server_default=text("false"), nullable=False),
    )

    # Launch hooks. recipe_ref is the sparkrun recipe handle (e.g.
    # "@mark/laguna-s21-nvfp4-vllm"); launch_template is a free-form command
    # blueprint for the docker engines. PR 2 hands these to the EXISTING
    # switch_recipe path — nothing here executes anything by itself.
    recipe_ref: str | None = Field(default=None, max_length=256)
    launch_template: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # One-click installation (PR 6). The command that puts the engine ON the
    # box — cloning a repo, building it, fetching weights. Runs once, as a
    # background job with a live log (services/recipe_install), never as part
    # of a start. Engines that need no installation leave it NULL.
    install_template: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # ssh_process only: what a runtime built from this recipe gets as
    # stop_command / process_name. Templated like the launch command, because
    # a stop script usually needs the same port.
    stop_template: str | None = Field(default=None, sa_column=Column(Text, nullable=True))
    process_name: str | None = Field(default=None, max_length=64)

    # Attribution. Community engines and quants are somebody's work; the card
    # shows "von {author}" with a link. Nullable because an honest blank beats
    # a guessed credit.
    author: str | None = Field(default=None, max_length=128)
    author_url: str | None = Field(default=None, max_length=512)

    # Provenance: "builtin" for the seed file, otherwise the registry name a
    # refresh pulled it from. Indexed because "show me everything from source
    # X" is the natural way to audit an imported registry.
    source_registry: str = Field(default="builtin", index=True, max_length=64)
    source_url: str | None = Field(default=None, max_length=512)

    # Engine tuning as data (PR 8). A flat ``{"KEY": "value"}`` map that the
    # compose recipes render into the ``environment:`` block of the
    # ``compose.override.yaml`` they already write for the container name and
    # the mc.runtime.slug label. The four values that made DeepSeek V4 Flash fit
    # on the Spark lived in a hand-written compose.tuning.yaml on the box — one
    # re-clone away from being lost. Declared here they survive re-deploys and
    # are visible in the deploy dialog before anything runs.
    #
    # NULL means "no tuning", which is what every non-compose recipe wants.
    env: dict[str, str] | None = Field(default=None, sa_column=Column(JSON, nullable=True))

    tags: list[str] = Field(default_factory=list, sa_column=Column(JSON))
    notes: str | None = Field(default=None, sa_column=Column(Text, nullable=True))

    # Operator decision — a refresh may update every other field but never
    # flips this back on (services/local_registry._apply_update).
    enabled: bool = Field(
        default=True,
        sa_column=Column(Boolean, server_default=text("true"), nullable=False),
    )

    # first_seen_at drives the "new" notification: it is set once on insert and
    # never touched again, so an entry stays "the day MC first learned of it"
    # even after ten refreshes rewrote its description.
    first_seen_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(DateTime(timezone=True), server_default=text("NOW()"), nullable=False),
    )
    updated_at: datetime = Field(
        default_factory=utcnow,
        sa_column=Column(
            DateTime(timezone=True),
            server_default=text("NOW()"),
            onupdate=utcnow,
            nullable=False,
        ),
    )
