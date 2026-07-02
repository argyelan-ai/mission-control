"""Minimal HTTP client — stdlib only, retry on 5xx, hard-fail on 4xx."""
from __future__ import annotations

import json
import time
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
        last_error: Exception | None = None

        for attempt in range(1, MAX_RETRIES + 1):
            req = urllib.request.Request(url, data=data, headers=self._headers(), method=method)
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
