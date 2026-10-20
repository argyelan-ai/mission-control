#!/usr/bin/env python3
"""omp subagents run on the bound runtime, never on a built-in provider (fix 20.10.2026).

THE BUG
-------
render-omp-config.sh wrote only the ``mc-openai`` provider (models.yml) and
left omp's ``modelRoles`` empty. omp's bundled subagents pick their model by
ROLE: ``task`` → ``@task``, ``scout`` → ``@smol``, ``reviewer`` → ``@slow``
(``omp agents unpack``, omp 18.10.1). An unset role is auto-resolved across
every *available* model — and omp.env carries ``OPENAI_API_KEY`` (``sk-noauth``
or a real key of a keyed runtime), which makes omp's BUILT-IN ``openai``
provider available. So scout/reviewer subagents (and the effort judge, and a
fresh ACP session before the model pin) went to api.openai.com: "401
invalid_api_key (sk-noauth)" — or, on a keyed runtime, MC's key travelled to a
third party. ADR-086: the harness runs on the runtime MC bound, nothing else.

Second, latent bug found on the way: ``omp config set modelRoles.vision …``
answers "Unknown setting: modelRoles.vision" (exit 1) on omp 18.10.1 — the
vision branch never set the vision role and, chained with ``&&``, never set
``images.questionTimeoutMs`` either. A record setting is written whole:
``omp config set modelRoles '<json>'``.

THE FIX (render-omp-config.sh, the one place that writes omp's profile config
for MC's container agents — container start AND every reload/runtime switch)
  * ``modelRoles``: every chat role + ``judge`` → ``mc-openai/<bound model>``;
    ``vision`` only when the runtime takes images (else images.blockImages).
  * ``task.agentModelOverrides`` → ``{}`` (no per-agent route elsewhere).
  * ``disabledProviders`` → omp's built-in model providers (the gate omp
    checks in role resolution, fallback chains and model availability).

Part (a)/(b): no docker — a fake ``omp`` keeps a JSON settings store and
rejects dotted record keys exactly like omp 18.10.1 does.

Part (c): opt-in, needs docker and the omp image
(``MC_OMP_IMAGE=mc-omp-agent:latest``): throwaway containers on an INTERNAL
docker network — a fake OpenAI engine, a trap that answers for
api.openai.com & co., and the real omp driven over ACP like the bridge does.

  pytest docker/omp-bridge/tests/test_render_omp_model_roles.py -v
  MC_OMP_IMAGE=mc-omp-agent:latest pytest docker/omp-bridge/tests/test_render_omp_model_roles.py -v
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
SCRIPT = ROOT / "render-omp-config.sh"

# Chat roles of omp 18.10.1 (CHAT_MODEL_ROLE_IDS in the binary) without
# `vision`, plus `judge` (the effort judge for task spawns). `vision` follows
# the runtime's image flag; image/web/speech/dictation are not chat roles.
ROLES = ("default", "smol", "slow", "plan", "commit", "tiny", "memory",
         "task", "advisor", "judge")

# Built-in providers that must never be reachable for an MC agent — a sample
# across the kinds: env-keyed clouds, OAuth logins, keyless local discovery.
MUST_BE_DISABLED = {
    "openai", "anthropic", "google", "openrouter", "xai", "groq", "mistral",
    "deepseek", "azure", "azure-openai-responses", "github-copilot",
    "openai-codex", "ollama", "ollama-cloud", "lm-studio", "vllm", "zenmux",
    "huggingface", "together", "fireworks",
}
# Never in the list: MC's own provider; `web` (web_search backends, not a
# model route) and `local` (in-container tiny/tts/stt models); `cursor` is ALSO
# the id of omp's rules/MCP discovery provider, and disabledProviders is shared
# between both registries.
MUST_STAY_ENABLED = {"mc-openai", "web", "local", "cursor"}


# ── (a)/(b) renderer, no docker ──────────────────────────────────────────────

FAKE_OMP = r'''#!/usr/bin/env python3
"""Fake omp: `config set|get|reset` against a JSON store, like omp 18.10.1."""
import json, os, sys
store = os.environ["FAKE_OMP_STORE"]
fail = set(filter(None, os.environ.get("FAKE_OMP_FAIL", "").split(",")))
KNOWN = {
    "modelRoles": "record", "task.agentModelOverrides": "record",
    "disabledProviders": "array", "images.blockImages": "boolean",
    "images.questionTimeoutMs": "number", "browser.cdpUrl": "string",
    "startup.setupWizard": "boolean", "setupVersion": "number",
}
args = sys.argv[1:]
if len(args) < 2 or args[0] != "config":
    sys.exit(1)
action, key = args[1], (args[2] if len(args) > 2 else "")
if key not in KNOWN:
    print(f"Unknown setting: {key}", file=sys.stderr)
    sys.exit(1)
if key in fail:
    sys.exit(1)
data = json.load(open(store)) if os.path.exists(store) else {}
if action == "get":
    print(json.dumps(data.get(key)))
    sys.exit(0)
if action == "reset":
    data.pop(key, None)
elif action == "set":
    raw = " ".join(args[3:])
    kind = KNOWN[key]
    if kind in ("record", "array"):
        value = json.loads(raw)
        if not isinstance(value, dict if kind == "record" else list):
            sys.exit(1)
    elif kind == "boolean":
        value = {"true": True, "false": False}[raw]
    elif kind == "number":
        value = int(raw)
    else:
        value = raw
    data[key] = value
else:
    sys.exit(1)
json.dump(data, open(store, "w"))
'''


def _bindir(tmp_path: Path) -> Path:
    bindir = tmp_path / "bin"
    if not bindir.exists():
        bindir.mkdir()
        omp = bindir / "omp"
        omp.write_text(FAKE_OMP, encoding="utf-8")
        omp.chmod(0o755)
        tmux = bindir / "tmux"
        tmux.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        tmux.chmod(0o755)
    return bindir


def _render(tmp_path: Path, *, model: str = "Model-A", vision: bool = False,
            extra: dict | None = None) -> tuple[dict, subprocess.CompletedProcess]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    store = tmp_path / "omp-settings.json"
    env = {
        k: v for k, v in os.environ.items()
        if not k.startswith(("OMP_", "OPENAI_", "PI_CODING_AGENT_DIR", "AGENT_NAME", "FAKE_OMP_"))
    }
    env.update(
        PATH=f"{_bindir(tmp_path)}:{os.environ.get('PATH', '/usr/bin:/bin')}",
        HOME=str(home),
        OMP_PROFILE="mc-agent",
        OMP_HOME=str(home / ".omp"),
        OMP_ENV_FILE=str(home / ".omp" / "omp.env"),
        OPENAI_BASE_URL="http://192.0.2.20:8000/v1",
        OPENAI_MODEL=model,
        OMP_MODEL_INPUT="text,image" if vision else "text",
        OMP_BROWSER_CDP_TARGET="off",
        OMP_RENDER_MOUNTINFO=str(tmp_path / "no-mountinfo"),
        FAKE_OMP_STORE=str(store),
    )
    env.update(extra or {})
    result = subprocess.run(
        ["bash", str(SCRIPT), "--no-bootstrap"],
        env=env, capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, f"stdout={result.stdout}\nstderr={result.stderr}"
    data = json.loads(store.read_text(encoding="utf-8")) if store.exists() else {}
    return data, result


def test_every_subagent_role_points_at_the_bound_model(tmp_path):
    """(a) The incident: scout (@smol), reviewer (@slow), task (@task), the
    effort judge — every role omp resolves for a subagent names MC's model."""
    data, _ = _render(tmp_path, model="Model-A")
    assert data.get("modelRoles") == {r: "mc-openai/Model-A" for r in ROLES}
    # text-only runtime: no vision role, omp refuses images itself
    assert data.get("images.blockImages") is True


def test_a_vision_runtime_also_gets_the_vision_role(tmp_path):
    """The old dotted `modelRoles.vision` call is rejected by omp 18.10.1 —
    vision was never set, and the `&&` chain never reached the timeout."""
    data, out = _render(tmp_path, model="Model-V", vision=True)
    expected = {r: "mc-openai/Model-V" for r in ROLES}
    expected["vision"] = "mc-openai/Model-V"
    assert data.get("modelRoles") == expected
    assert data.get("images.blockImages") is False
    assert data.get("images.questionTimeoutMs") == 120000
    assert "WARN" not in out.stdout + out.stderr


def test_per_agent_model_overrides_are_cleared(tmp_path):
    """task.agentModelOverrides outranks the roles for a named agent — a
    stale entry pointing at a built-in model would bypass the fix."""
    store = tmp_path / "omp-settings.json"
    store.write_text(json.dumps({"task.agentModelOverrides": {"scout": "openai/gpt-5.5"}}))
    data, _ = _render(tmp_path)
    assert data.get("task.agentModelOverrides") == {}


def test_builtin_providers_are_disabled_but_mc_openai_is_not(tmp_path):
    data, _ = _render(tmp_path)
    disabled = set(data.get("disabledProviders") or [])
    assert MUST_BE_DISABLED <= disabled, sorted(MUST_BE_DISABLED - disabled)
    assert not (disabled & MUST_STAY_ENABLED), sorted(disabled & MUST_STAY_ENABLED)


def test_a_failed_role_write_is_loud_and_does_not_block_the_start(tmp_path):
    """If omp refuses the write, the log says so (a silent miss is how this
    bug lived); the render still succeeds — the entrypoint reads a non-zero
    exit as "no model yet" and would wait 30 minutes."""
    data, out = _render(tmp_path, extra={"FAKE_OMP_FAIL": "modelRoles"})
    assert "modelRoles" not in data
    assert "WARN" in out.stdout + out.stderr
    assert "modelRoles" in out.stdout + out.stderr


def test_a_runtime_switch_moves_every_role_to_the_new_model(tmp_path):
    """(b) Runtime/recipe switch = reload (`docker exec … render-omp-config.sh`)
    or restart → same script. Every role follows, a stale vision role goes."""
    _render(tmp_path, model="Model-A", vision=True)
    data, _ = _render(tmp_path, model="org/Model-B:fp8", vision=False)
    assert data.get("modelRoles") == {r: "mc-openai/org/Model-B:fp8" for r in ROLES}
    assert data.get("images.blockImages") is True
    assert data.get("task.agentModelOverrides") == {}
    assert "openai" in data.get("disabledProviders", [])


# ── (c) real omp in throwaway containers ─────────────────────────────────────

IMAGE = os.environ.get("MC_OMP_IMAGE", "")

# Every env var omp 18.10.1's provider catalog reads a key from — with all of
# them set, every env-keyed built-in provider would be "available".
CATALOG_ENV_VARS = (
    "ABLITERATION_API_KEY ABLIT_KEY AIAND_API_KEY AIMLAPI_API_KEY AI_GATEWAY_API_KEY "
    "ALIBABA_CODING_PLAN_API_KEY ALIBABA_TOKEN_PLAN_API_KEY ANTHROPIC_API_KEY "
    "AWS_BEARER_TOKEN_BEDROCK AZURE_OPENAI_API_KEY BAILIAN_TOKEN_PLAN_API_KEY "
    "BASETEN_API_KEY CEREBRAS_API_KEY CHARM_HYPER_API_KEY CLINE_API_KEY "
    "CLOUDFLARE_AI_GATEWAY_API_KEY CLOUDFLARE_API_TOKEN CLOUDFLARE_AUTH_TOKEN "
    "COMMANDCODE_API_KEY COMMAND_CODE_API_KEY COPILOT_GITHUB_TOKEN CORALBRICKS_API_KEY "
    "CORAL_API_KEY COREWEAVE_API_KEY DEEPINFRA_API_KEY DEEPSEEK_API_KEY "
    "DEVIN_API_KEY FIREPASS_API_KEY FIREWORKS_API_KEY FUGU_API_KEY GEMINI_API_KEY "
    "GITLAB_TOKEN GMI_API_KEY GROQ_API_KEY HELMCODE_API_KEY HF_TOKEN "
    "HUGGINGFACE_HUB_TOKEN HYPER_API_KEY KILO_API_KEY KIMI_API_KEY LITELLM_API_KEY "
    "LM_STUDIO_API_KEY META_API_KEY MINIMAX_API_KEY MINIMAX_CODE_API_KEY "
    "MINIMAX_CODE_CN_API_KEY MISTRAL_API_KEY MODEL_API_KEY MOONSHOT_API_KEY "
    "NANO_GPT_API_KEY NOVITA_API_KEY NVIDIA_API_KEY OLLAMA_API_KEY "
    "OLLAMA_CLOUD_API_KEY OPENAI_API_KEY OPENAI_CODEX_OAUTH_TOKEN OPENCODE_API_KEY "
    "OPENROUTER_API_KEY QIANFAN_API_KEY QWEN_OAUTH_TOKEN QWEN_PORTAL_API_KEY "
    "SAKANA_API_KEY SILICONFLOW_API_KEY SILICONFLOW_CN_API_KEY "
    "SINGULARITYAPI_DEV_API_KEY SINGULARITYAPI_TECH_API_KEY SNOWFLAKE_PAT "
    "STEPFUN_API_KEY SYNTHETIC_API_KEY TOGETHER_API_KEY TYPESAFE_API_KEY "
    "UMANS_AI_CODING_PLAN_API_KEY VENICE_API_KEY VLLM_API_KEY "
    "WAFER_SERVERLESS_API_KEY WANDB_API_KEY XAI_API_KEY XAI_OAUTH_TOKEN "
    "XIAOMI_API_KEY XIAOMI_TOKEN_PLAN_AMS_API_KEY XIAOMI_TOKEN_PLAN_CN_API_KEY "
    "XIAOMI_TOKEN_PLAN_SGP_API_KEY YOLO_AUTO_API_KEY ZAI_API_KEY ZENMUX_API_KEY "
    "ZHIPU_API_KEY"
).split()
# What may still show up with every key set: MC's provider, web_search
# backends, in-container tiny models — and `cursor`, kept enabled for its
# rules/MCP discovery twin (it needs a cursor login, never present in MC).
ALLOWED_AVAILABLE = {"mc-openai", "web", "local", "cursor"}


def _docker_ready() -> str | None:
    if not IMAGE:
        return "set MC_OMP_IMAGE=<omp image> to run the container proof"
    if not shutil.which("docker"):
        return "docker CLI missing"
    probe = subprocess.run(["docker", "image", "inspect", IMAGE],
                           capture_output=True, text=True, timeout=30)
    if probe.returncode != 0:
        return f"image {IMAGE} not present"
    return None


_SKIP_REASON = _docker_ready()
container = pytest.mark.skipif(_SKIP_REASON is not None, reason=str(_SKIP_REASON))


def _docker(*args: str, timeout: int = 120, check: bool = True) -> subprocess.CompletedProcess:
    r = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=timeout)
    if check and r.returncode != 0:
        raise AssertionError(f"docker {' '.join(args[:3])}… failed: {r.stderr[-800:]}")
    return r


RENDER_ENV = {
    "OPENAI_BASE_URL": "http://fake-engine:8000/v1",
    "OPENAI_MODEL": "fake-model",
    "OMP_BROWSER_CDP_TARGET": "off",
    "AGENT_NAME": "roles-it",
}


@container
def test_container_no_builtin_provider_is_available_even_with_every_key_set():
    """(c1) The lock itself, on the real omp: render, then set EVERY key
    omp's catalog knows and ask omp which providers have usable models."""
    script = (
        "set -e; bash /src/render-omp-config.sh --no-bootstrap >/dev/null; "
        "omp models --json --kind all"
    )
    env_args: list[str] = []
    for k, v in RENDER_ENV.items():
        env_args += ["-e", f"{k}={v}"]
    for var in CATALOG_ENV_VARS:
        env_args += ["-e", f"{var}=dummy-not-a-key"]
    r = _docker("run", "--rm", "--network", "none", *env_args,
                "-v", f"{ROOT}:/src:ro", "--entrypoint", "bash", IMAGE, "-c", script,
                timeout=180)
    models = json.loads(r.stdout)["models"]
    providers = {m["provider"] for m in models}
    assert "mc-openai" in providers
    leaked = providers - ALLOWED_AVAILABLE
    assert not leaked, f"built-in providers still available: {sorted(leaked)}"


FAKE_ENGINE = r'''
import json, sys, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
MODEL = "fake-model"
SPAWN = {"i": "delegate", "context": "# Goal\nrole probe", "tasks": [
    {"task": "SUBAGENT-MARKER agent=task reply PONG", "solutionSpace": "one fix: reply PONG", "name": "TaskProbe"},
    {"task": "SUBAGENT-MARKER agent=scout reply PONG", "solutionSpace": "one fix: reply PONG", "name": "ScoutProbe", "agent": "scout"},
    {"task": "SUBAGENT-MARKER agent=reviewer reply PONG", "solutionSpace": "one fix: reply PONG", "name": "ReviewProbe", "agent": "reviewer"}]}
def text_of(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return ""
class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def do_GET(self):
        self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})
    def do_POST(self):
        req = json.loads(self.rfile.read(int(self.headers.get("content-length") or 0)) or b"{}")
        msgs = req.get("messages") or []
        tools = [t.get("function", {}).get("name") for t in (req.get("tools") or [])]
        users = [text_of(m) for m in msgs if m.get("role") == "user"]
        everything = " ".join(text_of(m) for m in msgs if m.get("role") in ("user", "system", "developer"))
        n_results = sum(1 for m in msgs if m.get("role") == "tool")
        agent = ""
        if users and "MAIN-MARKER" in users[0]:
            kind = "main"
        elif "SUBAGENT-MARKER" in everything:
            kind = "subagent"
            agent = everything.split("SUBAGENT-MARKER agent=", 1)[1].split()[0]
        else:
            kind = "other"
        # A subagent SESSION carries the `yield` tool; the parent's one-line
        # task labeler quotes the same assignment text but has no tools.
        print(json.dumps({"kind": kind, "agent": agent, "model": req.get("model"),
                          "session": "yield" in tools}), flush=True)
        call, text = None, "DONE"
        if kind == "main" and n_results == 0 and "task" in tools:
            call = ("task", SPAWN)
        elif kind == "main" and n_results < 4 and "wait" in tools:
            call = ("wait", {})
        elif kind == "subagent" and "yield" in tools:
            call = ("yield", {"data": {"result": "PONG"}})
        elif kind == "other" and "Answer with exactly one of" in everything:
            text = "medium"
        self._answer(req, text, call)
    def _answer(self, req, text, call):
        cid = "chatcmpl-" + uuid.uuid4().hex[:8]
        tc = None
        if call:
            tc = {"index": 0, "id": "call_" + uuid.uuid4().hex[:8], "type": "function",
                  "function": {"name": call[0], "arguments": json.dumps(call[1])}}
        usage = {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}
        if not req.get("stream"):
            msg = {"role": "assistant", "content": None if tc else text}
            if tc:
                msg["tool_calls"] = [{k: v for k, v in tc.items() if k != "index"}]
            return self._json({"id": cid, "object": "chat.completion", "model": MODEL, "usage": usage,
                               "choices": [{"index": 0, "message": msg,
                                            "finish_reason": "tool_calls" if tc else "stop"}]})
        self.send_response(200)
        self.send_header("content-type", "text/event-stream")
        self.end_headers()
        def ev(delta, finish=None):
            obj = {"id": cid, "object": "chat.completion.chunk", "created": int(time.time()), "model": MODEL,
                   "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}
            if finish:
                obj["usage"] = usage
            self.wfile.write(("data: " + json.dumps(obj) + "\n\n").encode())
        ev({"role": "assistant", "content": ""})
        if tc:
            ev({"tool_calls": [tc]})
            ev({}, "tool_calls")
        else:
            ev({"content": text})
            ev({}, "stop")
        self.wfile.write(b"data: [DONE]\n\n")
        self.wfile.flush()
ThreadingHTTPServer(("0.0.0.0", 8000), H).serve_forever()
'''

# Answers for the hosts omp's built-in providers call. Logs every connection
# (with the TLS SNI) — the fix is proven when this log stays EMPTY.
TRAP = r'''
import json, socket, threading, time
def sni(d):
    try:
        if d[0] != 0x16:
            return None
        i = 43
        i += 1 + d[i]
        i += 2 + int.from_bytes(d[i:i + 2], "big")
        i += 1 + d[i]
        end = i + 2 + int.from_bytes(d[i:i + 2], "big")
        i += 2
        while i < end:
            t, l = int.from_bytes(d[i:i + 2], "big"), int.from_bytes(d[i + 2:i + 4], "big")
            if t == 0:
                n = int.from_bytes(d[i + 7:i + 9], "big")
                return d[i + 9:i + 9 + n].decode()
            i += 4 + l
    except Exception:
        return None
def serve(port):
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("0.0.0.0", port))
    s.listen(64)
    while True:
        c, a = s.accept()
        c.settimeout(3)
        try:
            d = c.recv(4096)
        except Exception:
            d = b""
        print(json.dumps({"port": port, "sni": sni(d) if d else None}), flush=True)
        c.close()
for p in (80, 443):
    threading.Thread(target=serve, args=(p,), daemon=True).start()
while True:
    time.sleep(3600)
'''
SUBAGENTS = {"task", "scout", "reviewer"}  # @task, @smol, @slow
TRAP_HOSTS = ("api.openai.com", "api.anthropic.com", "openrouter.ai",
              "generativelanguage.googleapis.com", "api.x.ai", "ollama.com",
              "api.groq.com", "api.mistral.ai", "api.deepseek.com", "chatgpt.com")

# Runs inside the omp container: render exactly like the entrypoint, load
# omp.env like launch-omp.sh / acp_model, then drive `omp acp` like the bridge
# (session/new → pin the MC model → prompt). With RELOAD_TO set, the runtime
# switches while omp keeps running: re-render (= `docker exec … render-omp-
# config.sh`), then the next turn.
DRIVER = r"""
import json, os, subprocess, sys, time
sys.path.insert(0, "/src")
from acp_client import ACPClient, ALLOW_ONCE
def render(model):
    e = dict(os.environ, OPENAI_MODEL=model)
    return subprocess.run(["bash", "/src/render-omp-config.sh", "--no-bootstrap"], env=e,
                          capture_output=True, text=True).returncode
def omp_env():
    env = dict(os.environ)
    for line in open(os.path.join(env["HOME"], ".omp", "omp.env")):
        k, sep, v = line.rstrip("\n").partition("=")
        if sep:
            env[k] = v
    return env
rc = render(os.environ["OPENAI_MODEL"])
env = omp_env()
os.makedirs("/tmp/w", exist_ok=True)
c = ACPClient(cwd="/tmp/w", env=env)
c.on_permission(lambda req: ALLOW_ONCE)
c._ensure_process()
c.initialize()
sid = c.new_session("/tmp/w")
opened = next((o.get("currentValue") for o in c.last_session_result.get("configOptions") or []
               if o.get("id") == "model"), None)
c.set_config_option(sid, "model", env["OMP_MODEL_SELECTOR"])
if os.environ.get("RELOAD_TO"):
    rc = rc or render(os.environ["RELOAD_TO"])
    time.sleep(3)
    try:  # a running omp may not offer the new model yet; subagents must still move
        c.set_config_option(sid, "model", omp_env()["OMP_MODEL_SELECTOR"])
    except Exception:
        pass
res = c.prompt(sid, "MAIN-MARKER: delegate to subagents", timeout=200)
print("RESULT " + json.dumps({"render_rc": rc, "opened_on": opened,
                              "stop": res.stopReason}), flush=True)
# Keep omp alive: background subagents may still be on their first request.
# The test stops this container once the engine has seen all of them.
time.sleep(90)
c.close()
"""


def _reached(log: list[dict]) -> dict[str, set[str]]:
    """agent → models its subagent SESSIONS asked the engine for."""
    out: dict[str, set[str]] = {}
    for e in log:
        if e["kind"] == "subagent" and e["session"]:
            out.setdefault(e["agent"], set()).add(e["model"])
    return out


def _scenario(extra_env: dict | None = None) -> tuple[dict, list[dict], list[str]]:
    """Fake engine + trap + omp driver on an internal network. Returns the
    driver's RESULT, the engine's request log and the trap's connections."""
    tag = "omp-roles-it-" + uuid.uuid4().hex[:8]
    net, engine, trap, driver = tag, f"{tag}-engine", f"{tag}-trap", f"{tag}-omp"
    _docker("network", "create", "--internal", net)
    try:
        _docker("run", "-d", "--rm", "--name", engine, "--network", net,
                "--network-alias", "fake-engine", "--entrypoint", "python3", IMAGE,
                "-c", FAKE_ENGINE)
        aliases: list[str] = []
        for host in TRAP_HOSTS:
            aliases += ["--network-alias", host]
        _docker("run", "-d", "--rm", "--name", trap, "--user", "0", "--network", net,
                *aliases, "--entrypoint", "python3", IMAGE, "-c", TRAP)
        env_args: list[str] = []
        for k, v in {**RENDER_ENV, **(extra_env or {})}.items():
            env_args += ["-e", f"{k}={v}"]
        _docker("run", "-d", "--name", driver, "--network", net, *env_args,
                "-v", f"{ROOT}:/src:ro", "--entrypoint", "python3", IMAGE, "-c", DRIVER)

        def engine_log() -> list[dict]:
            return [json.loads(ln) for ln in _docker("logs", engine).stdout.splitlines()
                    if ln.startswith("{")]

        deadline = time.monotonic() + 240
        while time.monotonic() < deadline:
            if [ln for ln in _docker("logs", trap).stdout.splitlines() if ln.strip()]:
                break  # fail fast: something knocked on a built-in provider
            done = "RESULT " in _docker("logs", driver, check=False).stdout
            if done and set(_reached(engine_log())) >= SUBAGENTS:
                break
            state = _docker("inspect", "-f", "{{.State.Running}}", driver).stdout.strip()
            if state != "true":
                break
            time.sleep(1)
        driver_out = _docker("logs", driver, check=False)
        log = engine_log()
        trap_hits = [ln for ln in _docker("logs", trap).stdout.splitlines() if ln.strip()]
    finally:
        for name in (driver, engine, trap):
            _docker("rm", "-f", name, check=False)
        _docker("network", "rm", net, check=False)

    assert trap_hits == [], f"a built-in provider was called: {trap_hits[:5]}"
    lines = [ln for ln in driver_out.stdout.splitlines() if ln.startswith("RESULT ")]
    assert lines, f"driver did not finish: {driver_out.stdout[-1500:]}{driver_out.stderr[-1500:]}"
    result = json.loads(lines[-1][len("RESULT "):])
    assert result["render_rc"] == 0, result
    assert result["stop"] == "end_turn", result
    return result, log, trap_hits


@container
def test_container_task_subagents_reach_the_bound_engine_never_a_builtin():
    """(c2) The incident end to end: the main agent spawns task, scout and
    reviewer subagents; each must land at the fake engine, and nothing may
    knock on api.openai.com & co. (trap log empty — `_scenario` asserts it).
    Also: a fresh ACP session opens on MC's model, not on openai/gpt-5.5."""
    result, log, _ = _scenario()
    assert result["opened_on"] == "mc-openai/fake-model", result
    assert _reached(log) == {a: {"fake-model"} for a in SUBAGENTS}, log
    assert all(e["model"] == "fake-model" for e in log), log


@container
def test_container_a_runtime_switch_moves_subagents_without_a_restart():
    """(c3) Runtime switch while omp runs (ADR-078 reload, no restart): the
    re-render alone moves every subagent to the NEW model — omp picks the
    roles up live. Still nothing for a built-in provider."""
    _, log, _ = _scenario({"RELOAD_TO": "fake-model-next"})
    assert _reached(log) == {a: {"fake-model-next"} for a in SUBAGENTS}, log
