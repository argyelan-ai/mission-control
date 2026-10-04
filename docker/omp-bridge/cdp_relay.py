#!/usr/bin/env python3
"""cdp_relay.py — omp's local CDP relay that names its agent to cdp-gateway.

WHY THIS FILE EXISTS
--------------------
omp's browser tool reaches the shared agent browser through a relay on
127.0.0.1 inside the agent container (Chromium only accepts an IP or
`localhost` as Host — see cdp-relay.sh). Behind it, cdp-gateway
(docker/cdp-browser/gateway/cdp_gateway.py) works out which agent a CDP
connection belongs to from a `/a/<slug>/` path prefix, so the operator's
per-agent browser panel can show "Alpha's tabs".

Putting the prefix into omp's `browser.cdpUrl` is not enough (live finding
04.10.2026, verified in the omp 18.1.10 bundle): omp attaches with
Puppeteer's `connect({browserURL})`, which fetches
`new URL("/json/version", browserURL)` — an absolute path that DROPS the
prefix on exactly the request that hands out the browser WebSocket URL. And
the gateway's fallback, reverse DNS of the container IP, failed in the real
Docker network ("Name does not resolve"). Every omp tab ended up "nobody's".

So the relay adds the prefix itself, on every request line it forwards:

    GET /json/version HTTP/1.1          ->  GET /a/<slug>/json/version HTTP/1.1
    PUT /json/new?https://x HTTP/1.1    ->  PUT /a/<slug>/json/new?https://x HTTP/1.1
    GET /devtools/browser/<id> HTTP/1.1 ->  GET /a/<slug>/devtools/browser/<id> ...

That makes attribution independent of how any client library builds its
URLs. Rules:
  * Every request on a keep-alive connection is rewritten, not just the
    first (omp's Bun fetch reuses connections).
  * A path that already carries exactly this agent's prefix stays as it is
    (no doubling); nothing else is stripped — a foreign `/a/<other>` would
    end up behind ours and fail loudly instead of being re-attributed.
  * After a protocol upgrade (WebSocket `101`) the rest of the connection is
    passed through byte for byte. So is anything that does not parse as an
    origin-form HTTP/1.x request, and anything after a chunked body.
  * The server -> client direction is never touched.

When attribution is OFF (empty agent path) this is a plain TCP relay —
`cdp-relay.sh` still prefers socat then, so the rollback path stays the
exact pre-change setup.

Attribution is ON when `OMP_BROWSER_CDP_ATTRIBUTION` is `auto` (the default)
and the target is the gateway port 9300, or `on` for another gateway port.
`cdp-browser:9223` — the documented rollback that bypasses the gateway — is
plain Chromium, which would answer `/a/<slug>/json/version` with 404: `auto`
leaves it alone and `on` is refused there with a warning.
The slug comes from `AGENT_SLUG`, else `AGENT_NAME` (lowercased), and must
match the gateway's own slug rule or attribution stays off.

Usage
-----
  cdp_relay.py --agent-path                      # print "/a/<slug>" or "" (shell helper)
  cdp_relay.py --listen-port 9222 --target cdp-browser:9300
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from typing import Optional

logger = logging.getLogger("cdp_relay")

# Same rule as cdp_gateway._SLUG_RE — a slug the gateway would reject must
# never be sent (it would silently fall back to "unidentified" there).
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_GATEWAY_PORT = "9300"
_PLAIN_CHROMIUM_PORT = "9223"  # cdp-browser's socat port: Chromium itself, no gateway
_MAX_HEAD_BYTES = 64 * 1024
_CHUNK = 64 * 1024
# After one direction ends, give the other this long to finish (socat's -t).
_HALF_CLOSE_GRACE = 2.0


def agent_path(env: Optional[dict] = None) -> str:
    """`/a/<slug>` when attribution is on for this container, else ""."""
    env = os.environ if env is None else env
    target = (env.get("OMP_BROWSER_CDP_TARGET") or "cdp-browser:9300").strip()
    mode = (env.get("OMP_BROWSER_CDP_ATTRIBUTION") or "auto").strip().lower()
    port = target.rsplit(":", 1)[-1]
    if mode in ("off", "0", "false", "no"):
        return ""
    if mode in ("on", "1", "true", "yes"):
        if port == _PLAIN_CHROMIUM_PORT:
            # Plain Chromium answers every `/a/<slug>/...` path with 404 —
            # forcing the prefix there would break the browser outright.
            print(
                f"[cdp-relay] WARN: OMP_BROWSER_CDP_ATTRIBUTION=on ignored — {target} is plain "
                "Chromium (no cdp-gateway), it would answer every prefixed request with 404",
                file=sys.stderr,
            )
            return ""
        if port != _GATEWAY_PORT:
            print(
                f"[cdp-relay] note: attribution forced on for {target} — it must be a cdp-gateway, "
                "plain Chromium would answer every prefixed request with 404",
                file=sys.stderr,
            )
    elif port != _GATEWAY_PORT:  # auto
        return ""
    slug = (env.get("AGENT_SLUG") or env.get("AGENT_NAME") or "").strip().lower()
    if not _SLUG_RE.match(slug):
        return ""
    return f"/a/{slug}"


def prefix_request_line(line: bytes, path_prefix: str) -> bytes:
    """Returns the request line with `path_prefix` in front of its path —
    unless the path already starts with exactly this prefix (omp's own
    cdpUrl, or a WebSocket URL the gateway handed out). Nothing else is
    stripped or reinterpreted: a foreign `/a/<other>` stays behind our
    prefix and fails loudly at the gateway instead of being silently
    re-attributed. Lines that are not an origin-form HTTP/1.x request come
    back unchanged."""
    if not path_prefix:
        return line
    parts = line.split(b" ")
    if len(parts) != 3 or not parts[2].startswith(b"HTTP/1."):
        return line
    method, target, version = parts
    if not target.startswith(b"/"):
        return line  # absolute-form / authority-form: not CDP, leave it
    prefix = path_prefix.encode("ascii")
    if target == prefix or (target.startswith(prefix) and target[len(prefix):len(prefix) + 1] in (b"/", b"?")):
        return line
    return b" ".join((method, prefix + target, version))


def _header(head: bytes, name: bytes) -> Optional[bytes]:
    lname = name.lower()
    for line in head.split(b"\r\n")[1:]:
        key, sep, value = line.partition(b":")
        if sep and key.strip().lower() == lname:
            return value.strip()
    return None


async def _copy_exact(reader: asyncio.StreamReader, writer: asyncio.StreamWriter, n: int) -> None:
    while n > 0:
        data = await reader.read(min(n, _CHUNK))
        if not data:
            raise asyncio.IncompleteReadError(b"", n)
        writer.write(data)
        await writer.drain()
        n -= len(data)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    while True:
        data = await reader.read(_CHUNK)
        if not data:
            return
        writer.write(data)
        await writer.drain()


async def _client_to_upstream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter, path_prefix: str,
) -> None:
    if not path_prefix:
        await _pipe(reader, writer)
        return
    while True:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except asyncio.IncompleteReadError as e:
            if e.partial:  # trailing bytes that never became a request head
                writer.write(e.partial)
                await writer.drain()
            return
        except asyncio.LimitOverrunError:
            # Not an HTTP head we understand — stop interpreting, just relay.
            await _pipe(reader, writer)
            return
        first, sep, rest = head.partition(b"\r\n")
        new_first = prefix_request_line(first, path_prefix)
        writer.write(new_first + sep + rest)
        await writer.drain()
        if new_first == first and not first.split(b" ")[-1].startswith(b"HTTP/1."):
            await _pipe(reader, writer)  # wasn't HTTP after all
            return
        if _header(head, b"Upgrade") is not None:
            await _pipe(reader, writer)  # WebSocket (or any upgrade): raw from here
            return
        if b"chunked" in (_header(head, b"Transfer-Encoding") or b"").lower():
            await _pipe(reader, writer)  # can't find the next request boundary cheaply
            return
        length = _header(head, b"Content-Length")
        if length:
            try:
                n = int(length)
            except ValueError:
                await _pipe(reader, writer)
                return
            await _copy_exact(reader, writer, n)


async def _close(writer: asyncio.StreamWriter) -> None:
    try:
        writer.close()
        await writer.wait_closed()
    except Exception:
        pass


async def _handle(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    target_host: str,
    target_port: int,
    path_prefix: str,
) -> None:
    try:
        # Resolved per connection (like socat's `fork`): a recreated
        # cdp-browser with a new IP needs no agent restart.
        up_reader, up_writer = await asyncio.open_connection(target_host, target_port, limit=_MAX_HEAD_BYTES)
    except OSError as e:
        logger.info("cdp_relay: upstream %s:%s unreachable: %s", target_host, target_port, e)
        await _close(client_writer)
        return
    up = asyncio.ensure_future(_client_to_upstream(client_reader, up_writer, path_prefix))
    down = asyncio.ensure_future(_pipe(up_reader, client_writer))
    try:
        done, pending = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        # Half-close the side whose source ended, then let the other
        # direction finish its last bytes (a response still in flight).
        for task, writer in ((up, up_writer), (down, client_writer)):
            if task in done:
                try:
                    if writer.can_write_eof():
                        writer.write_eof()
                except (OSError, RuntimeError):
                    pass
        if pending:
            await asyncio.wait(pending, timeout=_HALF_CLOSE_GRACE)
    finally:
        for task in (up, down):
            if not task.done():
                task.cancel()
        await asyncio.gather(up, down, return_exceptions=True)
        await _close(up_writer)
        await _close(client_writer)


async def start_relay(
    listen_host: str, listen_port: int, target_host: str, target_port: int, path_prefix: str,
):
    """Starts the relay server; returns the asyncio Server."""

    async def _on_client(reader, writer):
        await _handle(reader, writer, target_host, target_port, path_prefix)

    return await asyncio.start_server(_on_client, listen_host, listen_port, limit=_MAX_HEAD_BYTES)


def _split_target(target: str) -> tuple[str, int]:
    host, sep, port = target.rpartition(":")
    if not sep or not host or not port.isdigit():
        raise SystemExit(f"cdp_relay: --target must be host:port, got {target!r}")
    return host, int(port)


async def _serve(listen_port: int, target: str) -> None:
    host, port = _split_target(target)
    prefix = agent_path()
    server = await start_relay("127.0.0.1", listen_port, host, port, prefix)
    logger.info(
        "cdp_relay: 127.0.0.1:%d -> %s (agent path %s)", listen_port, target, prefix or "(off)",
    )
    async with server:
        await server.serve_forever()


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--agent-path", action="store_true", help='print "/a/<slug>" or "" and exit')
    parser.add_argument("--listen-port", type=int, default=9222)
    parser.add_argument("--target", default=os.environ.get("OMP_BROWSER_CDP_TARGET") or "cdp-browser:9300")
    args = parser.parse_args(argv)
    if args.agent_path:
        print(agent_path())
        return 0
    logging.basicConfig(level=logging.INFO, format="[cdp-relay] %(message)s")
    try:
        asyncio.run(_serve(args.listen_port, args.target))
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
