#!/usr/bin/env python3
"""Capture golden ACP fixtures from a REAL `omp acp` run (provenance tool).

Regenerates rpc/acp-{normal-turn,permission-tool,cancel-mid-turn}.ndjson by
driving `omp acp` as a child process over stdin/stdout (line-delimited
JSON-RPC) and recording every server->client line verbatim. stderr is logged
separately and never parsed.

Requires: omp on PATH with a working model (the harness env: OMP_HOME,
OMP_PROFILE, OPENAI_* / models.yml provider mc-openai) and the profile
settings `tools.approvalMode: always-ask` so the permission round-trip is
exercised instead of auto-approved.

Usage:  python3 tests/make_acp_fixtures.py [output-dir]   (default: ../rpc)
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
OMP = ["omp", "acp"]


def capture(name: str, driver, out_dir: str) -> None:
    proc = subprocess.Popen(
        OMP, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=open(f"/tmp/acp-capture-{name}.stderr.log", "wb"),
        cwd=os.environ.get("ACP_CAPTURE_CWD", os.getcwd()),
        env=dict(os.environ),  # inherit the harness env (model/provider/profile)
    )
    lines: list[dict] = []

    def reader():
        for raw in proc.stdout:
            lines.append(json.loads(raw.decode()))

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    try:
        driver(proc, lines)
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait(timeout=15)
    dst = os.path.join(out_dir, f"acp-{name}.ndjson")
    with open(dst, "w") as f:
        for obj in normalize(lines):
            f.write(json.dumps(obj) + "\n")
    print(name, len(lines), "server->client lines ->", dst)


def normalize(lines: list[dict]) -> list[dict]:
    """Renumber reply ids to the canonical client-request order the replay
    tests correlate by: 1=initialize, 2=session/new, 3=set_config_option,
    4=prompt. Server->client request ids are kept as recorded."""
    id_map = {1: 1, 2: 2, 90: 3, 3: 4}
    out = []
    for o in lines:
        if "id" in o and "method" not in o:
            o["id"] = id_map[o["id"]]
        out.append(o)
    return out


def rpc(proc, obj) -> None:
    proc.stdin.write((json.dumps(obj) + "\n").encode())
    proc.stdin.flush()


def wait_result(lines: list[dict], id_: int, timeout: float = 240) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        for obj in lines:
            if obj.get("id") == id_ and ("result" in obj or "error" in obj):
                return obj
        time.sleep(0.1)
    raise TimeoutError(f"no reply for id {id_}")


def sid_of(lines: list[dict]) -> str:
    for obj in lines:
        if obj.get("id") == 2:
            if "result" in obj:
                return obj["result"]["sessionId"]
            raise RuntimeError(f"session/new failed: {obj.get('error')}")
    raise LookupError("session/new reply missing")


def initialize(proc) -> None:
    rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": 1,
        "clientCapabilities": {"fs": {"readTextFile": True, "writeTextFile": True}}}})
    wait_result(lines_cache["lines"], 1)


lines_cache: dict = {}


def select_model(proc, lines, sid, value="mc-openai/GLM-5.3-Flash-EXL3") -> dict:
    rpc(proc, {"jsonrpc": "2.0", "id": 90, "method": "session/set_config_option",
               "params": {"sessionId": sid, "configId": "model", "value": value}})
    r = wait_result(lines, 90)
    if "error" in r:
        raise RuntimeError(f"set_config_option failed: {r['error']}")
    return r


def drive_normal_turn(proc, lines) -> None:
    lines_cache["lines"] = lines
    initialize(proc)
    rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": os.environ.get("ACP_CAPTURE_CWD", os.getcwd()),
                          "mcpServers": []}})
    wait_result(lines, 2)
    select_model(proc, lines, sid_of(lines))
    rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
        "sessionId": sid_of(lines),
        "prompt": [{"type": "text", "text": "Say exactly: hello golden fixture"}]}})
    wait_result(lines, 3)


def drive_permission_tool(proc, lines) -> None:
    """Turn that triggers a shell tool call + session/request_permission."""
    lines_cache["lines"] = lines
    initialize(proc)
    rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": os.environ.get("ACP_CAPTURE_CWD", os.getcwd()),
                          "mcpServers": []}})
    wait_result(lines, 2)
    sid = sid_of(lines)
    select_model(proc, lines, sid)
    rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": (
            "Use a shell command to write the text 'fixture-proof' into the "
            "file proof.txt in the current directory. Then confirm.")}]}})

    def answer_requests():
        """Respond to server->client requests like the library does."""
        deadline = time.time() + 240
        while time.time() < deadline:
            for obj in list(lines):
                if "id" in obj and obj.get("method") == "session/request_permission":
                    if not any(r.get("id") == obj["id"] and "method" not in r
                               for r in lines):
                        rpc(proc, {"jsonrpc": "2.0", "id": obj["id"], "result": {
                            "outcome": {"outcome": "selected",
                                        "optionId": "allow_once"}}})
                if "id" in obj and obj.get("method") == "fs/write_text_file":
                    if not any(r.get("id") == obj["id"] and "method" not in r
                               for r in lines):
                        rpc(proc, {"jsonrpc": "2.0", "id": obj["id"],
                                   "result": {}})
                if "id" in obj and obj.get("method") == "fs/read_text_file":
                    if not any(r.get("id") == obj["id"] and "method" not in r
                               for r in lines):
                        rpc(proc, {"jsonrpc": "2.0", "id": obj["id"],
                                   "result": {"content": "fixture-proof\n"}})
            time.sleep(0.2)

    threading.Thread(target=answer_requests, daemon=True).start()
    wait_result(lines, 3)


def drive_cancel_mid_turn(proc, lines) -> None:
    """Long streaming turn cancelled mid-flight -> stopReason=cancelled."""
    lines_cache["lines"] = lines
    initialize(proc)
    rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "session/new",
               "params": {"cwd": os.environ.get("ACP_CAPTURE_CWD", os.getcwd()),
                          "mcpServers": []}})
    wait_result(lines, 2)
    sid = sid_of(lines)
    select_model(proc, lines, sid)
    rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "session/prompt", "params": {
        "sessionId": sid,
        "prompt": [{"type": "text", "text": (
            "Count slowly from 1 to 300, one number per line, no other text.")}]}})
    time.sleep(8)
    rpc(proc, {"jsonrpc": "2.0", "method": "session/cancel",
               "params": {"sessionId": sid}})
    wait_result(lines, 3)


if __name__ == "__main__":
    out_dir = sys.argv[1] if len(sys.argv) > 1 else os.path.join(HERE, "..", "rpc")
    capture("normal-turn", drive_normal_turn, out_dir)
    capture("permission-tool", drive_permission_tool, out_dir)
    capture("cancel-mid-turn", drive_cancel_mid_turn, out_dir)
