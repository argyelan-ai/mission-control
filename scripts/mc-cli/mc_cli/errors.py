"""Exit codes + typed errors for `mc` CLI.

Exit codes:
  0 = success
  1 = client error (4xx, usage, missing env)
  2 = server error (5xx after retries, network)
  3 = timeout
"""
from __future__ import annotations


class CLIError(Exception):
    exit_code = 1


class UsageError(CLIError):
    exit_code = 1


class ClientError(CLIError):
    """4xx from backend — hard-fail, don't retry."""
    exit_code = 1


class ServerError(CLIError):
    """5xx after 3 retries, or network error."""
    exit_code = 2


class TimeoutError_(CLIError):  # shadow builtin intentionally in this ns
    exit_code = 3
