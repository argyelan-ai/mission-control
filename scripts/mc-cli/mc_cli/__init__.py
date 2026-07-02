"""Mission Control agent CLI (`mc`).

Thin wrapper around the agent-scoped HTTP API so Claude agents can interact
with MC via short commands (`mc ack`, `mc done`, ...) instead of embedding
multi-line curl snippets in their SOUL prompt.

Stdlib only — runs in every agent image without extra deps.
"""

__version__ = "0.1.0"
