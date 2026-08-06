"""Recipe → launch_command rendering (Box-Wizard, PR 4).

The wizard's last step needs one string: the ``launch_command`` that
``runtime_manager.start_runtime`` will run over SSH when the new runtime is
started. That string comes from the local registry entry — either from its own
``launch_template``, or from the per-engine default below when the entry
doesn't ship one (none of today's builtin recipes do).

Three deliberate choices:

* **Placeholders are ``{name}``, and only the known ones.** An unknown
  placeholder raises instead of rendering a literal ``{gpus}`` into a shell
  command. The regex only matches ``[a-z_]+`` inside braces, so docker's own
  ``--format '{{.State.Status}}'`` passes through untouched — that syntax
  would break ``str.format`` and is exactly why this isn't ``str.format``.
* **sparkrun is not handled here.** It already has
  ``sparkrun_manager.build_launch_command`` and its own switch-recipe path; a
  second way to phrase a sparkrun launch is a second thing to keep in sync.
* **The ``mc.runtime.slug`` label is mandatory.** Stop, restart and the
  start-verification find the container by that label — a launch command
  without it produces a runtime MC can start but never stop again.
"""

from __future__ import annotations

import re

# The only placeholders a template may use. The last three exist for
# ssh_process (PR 6): a host engine has no image and no container, it has a
# source checkout, a weight directory and a context budget.
KNOWN_PLACEHOLDERS = (
    "port", "model", "slug", "container_name", "image", "src_dir", "gguf_dir", "ctx",
)

_PLACEHOLDER_RE = re.compile(r"\{([a-z_]+)\}")

# Defaults per engine, used when a registry entry has no launch_template.
# Both keep --label mc.runtime.slug and bind 0.0.0.0 so the endpoint is
# reachable from the MC host, not just from inside the box.
DEFAULT_TEMPLATES: dict[str, str] = {
    "llamacpp_docker": (
        "docker run -d --rm --name {container_name} "
        "--label mc.runtime.slug={slug} "
        "-p {port}:{port} {image} "
        "-hf {model} --host 0.0.0.0 --port {port} --jinja"
    ),
    "vllm_docker": (
        "docker run -d --rm --name {container_name} "
        "--label mc.runtime.slug={slug} "
        "--gpus all --ipc=host -p {port}:8000 {image} "
        "--model {model}"
    ),
}

# Engine → container image for the default templates. GPU variants: the CUDA
# llama.cpp tag is multi-arch, so the same string works on the ARM64 Spark and
# on an x86 box (see docs/ARCHITECTURE.md, llamacpp_docker).
DEFAULT_IMAGES: dict[str, str] = {
    "llamacpp_docker": "ghcr.io/ggml-org/llama.cpp:server-cuda",
    "vllm_docker": "vllm/vllm-openai:latest",
}

# Engines the wizard can create a runtime for. sparkrun entries are shown but
# not deployable here — see the module docstring.
SUPPORTED_ENGINES = tuple(DEFAULT_TEMPLATES)

_SLUG_RE = re.compile(r"[A-Za-z0-9_-]+")

# ssh_process engines install themselves into the login's home. These are the
# defaults the seeded templates assume; a registry entry can point elsewhere by
# passing its own values.
DEFAULT_SRC_DIR = "~/code/mc-engines"
DEFAULT_GGUF_DIR = "~/gguf"

# Engines without a container have no label to carry, and no image to pull.
LABEL_FREE_ENGINES = ("ssh_process",)


def render_launch_template(template: str, values: dict[str, object]) -> str:
    """Substitute ``{placeholder}`` occurrences in *template*.

    Raises ValueError for an unknown placeholder or a known one without a
    value — both mean the caller built the command wrong, and a half-rendered
    docker command is not something to discover on the remote box.
    """
    if not template or not template.strip():
        raise ValueError("launch_template ist leer")

    missing: list[str] = []
    unknown: list[str] = []

    def _sub(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in KNOWN_PLACEHOLDERS:
            unknown.append(name)
            return match.group(0)
        value = values.get(name)
        if value is None or str(value) == "":
            missing.append(name)
            return match.group(0)
        return str(value)

    rendered = _PLACEHOLDER_RE.sub(_sub, template)

    if unknown:
        raise ValueError(
            f"Unbekannte Platzhalter im launch_template: {', '.join(sorted(set(unknown)))}. "
            f"Erlaubt: {', '.join(KNOWN_PLACEHOLDERS)}"
        )
    if missing:
        raise ValueError(
            f"Kein Wert für Platzhalter: {', '.join(sorted(set(missing)))}"
        )
    return rendered


def build_launch_command(
    *,
    engine: str,
    model_identifier: str,
    slug: str,
    port: int,
    launch_template: str | None = None,
    container_name: str | None = None,
    image: str | None = None,
    src_dir: str | None = None,
    gguf_dir: str | None = None,
    ctx: int | None = None,
) -> str:
    """The launch command for a new runtime from a registry entry.

    ``launch_template`` from the entry wins; otherwise the engine default is
    used. For container engines the rendered command always carries
    ``--label mc.runtime.slug=<slug>`` — without it the lifecycle ops can't
    find the container again. ssh_process engines are exempt: there is no
    container to label, and their handle is ``process_name`` instead.
    """
    if not _SLUG_RE.fullmatch(slug or ""):
        raise ValueError(f"slug muss alphanumerisch / _ / - sein: {slug!r}")
    if not model_identifier or not model_identifier.strip():
        raise ValueError("model_identifier fehlt")
    port = int(port)
    if not (1 <= port <= 65535):
        raise ValueError(f"Port ausserhalb des gültigen Bereichs: {port}")

    template = launch_template or DEFAULT_TEMPLATES.get(engine)
    if not template:
        raise ValueError(
            f"Engine '{engine}' hat kein launch_template und keinen Default — "
            f"unterstützt: {', '.join(SUPPORTED_ENGINES)}"
            + (
                ". ssh_process-Einträge MÜSSEN ein eigenes launch_template mitbringen: "
                "wie eine Host-Engine gestartet wird, weiss nur sie selbst."
                if engine in LABEL_FREE_ENGINES
                else ""
            )
        )

    command = render_launch_template(
        template,
        {
            "port": port,
            "model": model_identifier.strip(),
            "slug": slug,
            "container_name": container_name or f"mc-{slug}",
            "image": image or DEFAULT_IMAGES.get(engine, ""),
            "src_dir": src_dir or DEFAULT_SRC_DIR,
            "gguf_dir": gguf_dir or DEFAULT_GGUF_DIR,
            "ctx": ctx if ctx else 0,
        },
    )

    if engine not in LABEL_FREE_ENGINES and f"mc.runtime.slug={slug}" not in command:
        raise ValueError(
            "launch_template muss --label mc.runtime.slug={slug} enthalten — "
            "ohne dieses Label findet MC den Container später nicht wieder."
        )
    return command


def build_install_command(
    *,
    slug: str,
    install_template: str,
    port: int | None = None,
    model_identifier: str | None = None,
    src_dir: str | None = None,
    gguf_dir: str | None = None,
    ctx: int | None = None,
) -> str:
    """Render a recipe's ``install_template`` — same renderer, same placeholders.

    Separate from :func:`build_launch_command` only because an install has no
    container name, no image and no per-engine default: an entry that does not
    ship an install_template simply has nothing to install.
    """
    if not _SLUG_RE.fullmatch(slug or ""):
        raise ValueError(f"slug muss alphanumerisch / _ / - sein: {slug!r}")
    if not install_template or not install_template.strip():
        raise ValueError("Dieser Eintrag hat kein install_template — nichts zu installieren.")
    return render_launch_template(
        install_template,
        {
            "port": port if port else 0,
            "model": (model_identifier or "").strip() or "-",
            "slug": slug,
            "container_name": f"mc-{slug}",
            "image": "-",
            "src_dir": src_dir or DEFAULT_SRC_DIR,
            "gguf_dir": gguf_dir or DEFAULT_GGUF_DIR,
            "ctx": ctx if ctx else 0,
        },
    )
