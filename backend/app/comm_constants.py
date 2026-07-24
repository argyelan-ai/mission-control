"""Single source of truth für das Kommunikations-Protokoll — SOUL/TOOLS/Docs rendern hieraus, §9.1."""

MESSAGE_TYPES: tuple[str, ...] = ("message", "question", "status", "decision", "system")
QUESTION_PRIORITIES: tuple[str, ...] = ("low", "medium", "high", "critical")
QUESTION_TARGETS: tuple[str, ...] = ("mark", "boss", "agent")
WAITING_TIMEOUT_SECONDS: int = 7200
SIDE_THREAD_ROUND_LIMIT: int = 10
THREAD_KINDS: tuple[str, ...] = ("task", "side", "dm")
