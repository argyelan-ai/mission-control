#!/usr/bin/env python3
"""
MC Open Helper — läuft auf dem Host (nicht in Docker).
Empfängt POST /open vom Backend-Container und führt macOS `open` aus.

Start: python3 tools/mc-open-helper.py
Auto-Start: launchd plist unter tools/com.missioncontrol.openhelper.plist installieren.
"""
import json
import os
import subprocess
from http.server import BaseHTTPRequestHandler, HTTPServer

PORT = 8765


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path == "/open":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length))
                path = body.get("path", "").strip()
                reveal = body.get("reveal", False)

                if not path:
                    self._respond(400, {"error": "path required"})
                    return

                # Sicherheit: nur absolute Pfade akzeptieren
                if not os.path.isabs(path) or ".." in path:
                    self._respond(400, {"error": "invalid path"})
                    return

                cmd = ["open", "-R", path] if reveal else ["open", path]
                subprocess.Popen(cmd)
                self._respond(200, {"ok": True})
            except Exception as e:
                self._respond(500, {"error": str(e)})
        else:
            self._respond(404, {"error": "not found"})

    def _respond(self, status: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # silent


if __name__ == "__main__":
    server = HTTPServer(("127.0.0.1", PORT), Handler)
    print(f"MC Open Helper läuft auf http://127.0.0.1:{PORT}")
    print("Warte auf open-Requests vom Backend-Container...")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBeendet.")
