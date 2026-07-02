#!/usr/bin/env python3
"""WS-PTY-Bridge fuer host-side Agents (Boss + Hermes + zukuenftige).

Listet auf 127.0.0.1:7682 und attached via pty an eine tmux-Session.
Per Default `boss-host:0` (custom Socket fuer Backwards-Compat); via
`?session=<name>&socket=<path>` query-params auf andere host-tmux Sessions
umlenkbar (z.B. Hermes -> hermes-worker auf User-Default-Socket).

Bidirectional bytes — kein ttyd-Protocol. Resize via JSON
{"type":"resize","cols":N,"rows":N}.

Sicherheit: session-Name + socket-Pfad werden strikt validiert
(Regex / Pfad-Whitelist) bevor sie an `tmux` weitergereicht werden.
Phase 24 / HERM-01.
"""
import asyncio
import fcntl
import json
import os
import pty
import re
import struct
import sys
import termios
from urllib.parse import parse_qs

import websockets

# ── Boss-Default (Backwards-Compat) ─────────────────────────────────────────
# Wenn kein ?session= mitkommt, attachen wir an die Boss-Session ueber den
# Boss-eigenen Custom-Socket. Dieser Pfad bleibt unveraendert.
_HOME = os.environ.get("HOME_HOST", os.path.expanduser("~"))
DEFAULT_SOCKET = f"{_HOME}/.mc/agents/boss-host/.tmux.sock"
DEFAULT_SESSION = "boss-host:0"
CLAUDE_LOG = f"{_HOME}/.mc/agents/boss-host/logs/claude.log"
HOST = "127.0.0.1"
PORT = 7682

# Session-Name: alphanumerisch + - _ optional :<N> (Window-Index).
# Lehnt Shell-Metacharacter, Slashes, Whitespace ab.
_SESSION_RE = re.compile(r"^[a-zA-Z0-9_-]+(?::[0-9]+)?$")

# Socket muss absoluter Pfad sein, unterhalb von /tmp/tmux-* oder $TMPDIR/tmux-*.
# Verhindert ?socket=/etc/passwd & Co.
def _socket_allowed(path: str) -> bool:
    if not path or not path.startswith("/"):
        return False
    if ".." in path.split("/"):
        return False
    if path.startswith("/tmp/tmux-"):
        return True
    tmpdir = os.environ.get("TMPDIR", "").rstrip("/")
    if tmpdir and path.startswith(f"{tmpdir}/tmux-"):
        return True
    # macOS: TMPDIR ist per default /var/folders/.../T/. Fallback erlaubt
    # /var/folders/*/tmux-* falls TMPDIR nicht gesetzt ist.
    if path.startswith("/var/folders/") and "/tmux-" in path:
        return True
    # Boss-Custom-Socket explizit whitelisten (legacy, vor Phase 24)
    if path == DEFAULT_SOCKET:
        return True
    return False


def resolve_target(query_string: str) -> tuple[str, str]:
    """Parst ?session= + ?socket= aus dem Pfad-Query.

    Rueckgabe: (session_name, socket_path).
    Wirft ValueError bei ungueltigen Werten (-> 400 fuer Aufrufer).
    """
    qs = parse_qs(query_string or "")
    session_values = qs.get("session", [])
    socket_values = qs.get("socket", [])

    if session_values:
        session_name = session_values[0]
        if not _SESSION_RE.match(session_name):
            raise ValueError(f"invalid session name: {session_name!r}")
    else:
        session_name = DEFAULT_SESSION

    if socket_values:
        socket_path = socket_values[0]
        if not _socket_allowed(socket_path):
            raise ValueError(f"invalid socket path: {socket_path!r}")
    else:
        socket_path = DEFAULT_SOCKET

    return session_name, socket_path


# Scrollback-Replay: Letzte N Bytes von claude.log an neue Clients senden,
# bevor live an tmux attached wird. Grund: claude clear't den Screen bei
# jedem Start (nach Task-Done + Auto-Restart durch Wrapper), sodass tmux-
# history nur den aktuellen idle-Banner zeigt. claude.log (via pipe-pane)
# persistiert den vollen PTY-Stream — wir replay'en die letzten Aktivitaeten
# damit der User sehen kann was Boss getan hat.
SCROLLBACK_BYTES = 128 * 1024  # 128 KB reicht fuer mehrere Task-Zyklen


def read_scrollback() -> bytes:
    """Liest die letzten SCROLLBACK_BYTES aus claude.log.

    Defensiv: Wenn Log fehlt oder nicht lesbar -> leer, nicht crashen.
    """
    try:
        size = os.path.getsize(CLAUDE_LOG)
    except OSError:
        return b""
    if size == 0:
        return b""
    try:
        with open(CLAUDE_LOG, "rb") as f:
            if size > SCROLLBACK_BYTES:
                f.seek(-SCROLLBACK_BYTES, os.SEEK_END)
                # Ersten (vermutlich partiellen) ANSI-Escape-Start ueberspringen,
                # damit xterm.js nicht auf einem halben Escape-Code stehenbleibt.
                chunk = f.read()
                esc = chunk.find(b"\x1b")
                if esc > 0 and esc < 256:
                    chunk = chunk[esc:]
                return chunk
            return f.read()
    except OSError as e:
        print(f"[bridge] scrollback read failed: {e}", file=sys.stderr)
        return b""


async def handler(ws):
    # Query-Params parsen (Validierung wirft ValueError bei boesem Input)
    # `path` ist veraltet, neue websockets-Versionen geben das via ws.request.path
    raw_path = getattr(ws, "path", None) or getattr(getattr(ws, "request", None), "path", "")
    query_string = raw_path.split("?", 1)[1] if "?" in raw_path else ""
    try:
        session_name, socket_path = resolve_target(query_string)
    except ValueError as e:
        print(f"[bridge] rejecting client: {e}", file=sys.stderr)
        try:
            await ws.close(code=1008, reason=str(e)[:120])
        except Exception:
            pass
        return

    is_boss_default = (
        session_name == DEFAULT_SESSION and socket_path == DEFAULT_SOCKET
    )
    print(
        f"[bridge] new client connected, attaching to tmux "
        f"session={session_name} socket={socket_path}",
        file=sys.stderr,
    )

    master_fd, slave_fd = pty.openpty()

    # Scrollback-Replay nur fuer Boss-Default (claude.log gehoert zu Boss).
    # Andere Sessions (Hermes etc.) haben ihren eigenen Replay-Mechanismus
    # oder leben rein in tmux-history.
    if is_boss_default:
        scrollback = read_scrollback()
        if scrollback:
            header = (
                b"\r\n\x1b[38;5;246m"
                b"--- History Replay (letzte Aktivitaet aus claude.log) ---"
                b"\x1b[0m\r\n"
            )
            footer = (
                b"\r\n\x1b[38;5;246m"
                b"--- Ende History | Live-Session folgt ---"
                b"\x1b[0m\r\n\r\n"
            )
            try:
                await ws.send(header + scrollback + footer)
                print(f"[bridge] sent {len(scrollback)} bytes scrollback", file=sys.stderr)
            except websockets.ConnectionClosed:
                return

    # tmux braucht ein bekanntes TERM um attach durchzufuehren
    # ("open terminal failed: terminal does not support clear" sonst).
    # xterm-256color matched was xterm.js im Browser ohnehin emuliert.
    env = {**os.environ, "TERM": "xterm-256color"}

    proc = await asyncio.create_subprocess_exec(
        "tmux", "-S", socket_path, "attach-session", "-dt", session_name,
        stdin=slave_fd, stdout=slave_fd, stderr=slave_fd,
        env=env,
    )
    os.close(slave_fd)

    async def pty_to_ws():
        loop = asyncio.get_running_loop()
        try:
            while True:
                data = await loop.run_in_executor(None, lambda: os.read(master_fd, 4096))
                if not data:
                    break
                await ws.send(data)
        except (OSError, websockets.ConnectionClosed):
            pass
        except Exception as e:
            print(f"[bridge] pty_to_ws stopped: {e}", file=sys.stderr)

    async def ws_to_pty():
        # Phase 26 / HERM-13 (F7): byte-counter diagnostics so silent drops
        # become visible in the bridge log.
        written_total = 0
        frames_total = 0
        try:
            async for msg in ws:
                wrote = 0
                if isinstance(msg, (bytes, bytearray)):
                    wrote = os.write(master_fd, bytes(msg))
                else:
                    # text — could be JSON resize/input or raw keystroke
                    handled = False
                    try:
                        d = json.loads(msg)
                        if isinstance(d, dict):
                            if d.get("type") == "resize":
                                cols = int(d.get("cols", 80))
                                rows = int(d.get("rows", 24))
                                fcntl.ioctl(
                                    master_fd, termios.TIOCSWINSZ,
                                    struct.pack("HHHH", rows, cols, 0, 0),
                                )
                                handled = True
                            elif d.get("type") == "input":
                                wrote = os.write(master_fd, d.get("data", "").encode())
                                handled = True
                    except (json.JSONDecodeError, ValueError):
                        pass
                    if not handled:
                        wrote = os.write(master_fd, msg.encode())
                if wrote:
                    written_total += wrote
                    frames_total += 1
                    print(
                        f"[bridge] ws_to_pty: wrote {wrote} bytes to pty "
                        f"(session={session_name} frames={frames_total} "
                        f"total_bytes={written_total})",
                        file=sys.stderr,
                    )
        except (OSError, websockets.ConnectionClosed):
            pass
        except Exception as e:
            print(
                f"[bridge] ws_to_pty stopped after {frames_total} frames / "
                f"{written_total} bytes: {e}",
                file=sys.stderr,
            )

    try:
        await asyncio.gather(pty_to_ws(), ws_to_pty())
    finally:
        try:
            proc.terminate()
        except Exception:
            pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        print(f"[bridge] client disconnected, pty closed", file=sys.stderr)


async def main():
    print(
        f"[bridge] starting on {HOST}:{PORT} "
        f"(default tmux {DEFAULT_SESSION} via {DEFAULT_SOCKET}, "
        f"override via ?session=&socket=)",
        file=sys.stderr,
    )
    async with websockets.serve(handler, HOST, PORT, max_size=10 * 1024 * 1024):
        await asyncio.Future()  # run forever


if __name__ == "__main__":
    asyncio.run(main())
