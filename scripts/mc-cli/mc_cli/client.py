"""Minimal HTTP client — stdlib only, retry on 5xx, hard-fail on 4xx."""
from __future__ import annotations

import json
import mimetypes
import os
import time
import uuid
import urllib.error
import urllib.request
from typing import Any

from .config import Config
from .errors import ClientError, ServerError, TimeoutError_

DEFAULT_TIMEOUT = 30  # seconds
MAX_RETRIES = 3
RETRY_BACKOFF = 1.5  # seconds, grows linearly: 1.5, 3.0, 4.5


class Client:
    def __init__(self, cfg: Config):
        self.cfg = cfg

    def _headers(self) -> dict[str, str]:
        h = {
            "Authorization": f"Bearer {self.cfg.require_token()}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.cfg.dispatch_attempt_id:
            h["X-Dispatch-Attempt-Id"] = self.cfg.dispatch_attempt_id
        return h

    def request(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.cfg.api_url}{path}"
        if query:
            from urllib.parse import urlencode
            url = f"{url}?{urlencode({k: v for k, v in query.items() if v is not None})}"

        data = json.dumps(body).encode("utf-8") if body is not None else None
        return self._send(method, path, url, data, self._headers())

    def upload(self, path: str, file_path: str, field: str = "file") -> Any:
        """POST eine Datei als multipart/form-data (z.B. Anhang an einen Thread).

        Stdlib-only wie der Rest des Clients: der Multipart-Koerper wird von
        Hand gebaut (`encode_multipart`). Der Dateiname geht mit, damit das
        Backend Endung/Bildtyp erkennt.
        """
        with open(file_path, "rb") as fh:
            data_bytes = fh.read()
        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        content_type, data = encode_multipart(
            field, os.path.basename(file_path), data_bytes, mime
        )
        headers = self._headers()
        headers["Content-Type"] = content_type
        return self._send("POST", path, f"{self.cfg.api_url}{path}", data, headers)

    def _send(
        self, method: str, path: str, url: str, data: bytes | None, headers: dict[str, str]
    ) -> Any:
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=DEFAULT_TIMEOUT) as resp:
                    raw = resp.read()
                    if not raw:
                        return None
                    return json.loads(raw.decode("utf-8"))
            except urllib.error.HTTPError as e:
                body_txt = e.read().decode("utf-8", errors="replace") if e.fp else ""
                if 400 <= e.code < 500:
                    # Hard-fail on 4xx — retry won't help.
                    raise ClientError(f"HTTP {e.code} {method} {path}: {body_txt[:400]}") from e
                last_error = ServerError(f"HTTP {e.code} {method} {path}: {body_txt[:400]}")
            except urllib.error.URLError as e:
                # Network / DNS / connection refused.
                last_error = ServerError(f"Network error {method} {path}: {e.reason}")
            except TimeoutError:
                last_error = TimeoutError_(f"Timeout {method} {path} after {DEFAULT_TIMEOUT}s")

            if attempt < MAX_RETRIES:
                time.sleep(RETRY_BACKOFF * attempt)

        assert last_error is not None
        raise last_error


def encode_multipart(
    field: str, filename: str, data: bytes, mime: str
) -> tuple[str, bytes]:
    """Baut einen multipart/form-data-Koerper mit genau einem Datei-Feld.

    Gibt (Content-Type-Header, Koerper) zurueck; die Grenze (boundary) steht
    in beiden. Anfuehrungszeichen im Dateinamen werden ersetzt, damit der
    Header nicht zerbricht.
    """
    boundary = f"mc-cli-{uuid.uuid4().hex}"
    safe_name = filename.replace('"', "_")
    head = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="{field}"; filename="{safe_name}"\r\n'
        f"Content-Type: {mime}\r\n\r\n"
    ).encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    return f"multipart/form-data; boundary={boundary}", head + data + tail
