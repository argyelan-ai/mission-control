"""Effort level -> request-body fields for an OpenAI-compatible engine.

ONE small mapper, no model-family table. Which key an engine honours is not
knowable from the model name — three sources disagreed (MEMORY: Qwen only
obeys ``enable_thinking``; the spark-bench-standard skill: DeepSeek DSpark
only obeys ``thinking``; the bench research: DeepSeek-V4.1 on TensorFold
takes ``reasoning_effort``). So a caller first PROBES the served model
(bench_studio ``effort_probe``) and hands the probed key in here; this module
only knows how each key is spelled on the wire.

Agents are not served by this mapper: their effort is the harness's own
switch (``agent_chat_input.effort_levels_for`` / ``set_effort``), which stays
the single source for agents.
"""
from __future__ import annotations

from typing import Any

#: Levels a caller may request. ``default`` = send nothing (the server's own
#: default governs). ``on`` exists for engines that only have a switch.
EFFORT_LEVELS: tuple[str, ...] = ("default", "off", "on", "low", "medium", "high", "max")

#: Levels that carry a depth on a graded key (``reasoning_effort``).
GRADED_LEVELS: tuple[str, ...] = ("low", "medium", "high", "max")

#: The keys a probe tries, in this order. ``reasoning_effort`` is the only
#: graded one; the two chat-template switches only turn thinking on or off.
REASONING_EFFORT = "reasoning_effort"
ENABLE_THINKING = "chat_template_kwargs.enable_thinking"
THINKING = "chat_template_kwargs.thinking"
THINKING_KEYS: tuple[str, ...] = (REASONING_EFFORT, ENABLE_THINKING, THINKING)

#: ``reasoning_effort`` value that means "do not think" (TensorFold and the
#: OpenAI spec call it ``none``).
_EFFORT_OFF_VALUE = "none"

#: max_tokens multiplier per level: deeper thinking spends more tokens before
#: the HTML starts, and a fixed budget made high/max runs end as "model
#: returned no HTML content" (finish_reason "length").
_MAX_TOKENS_FACTOR: dict[str, int] = {"on": 2, "high": 2, "max": 4}

#: Head-room kept free for the prompt when a context limit is known.
_PROMPT_HEADROOM = 4096


class UnsupportedEffortError(ValueError):
    """The requested level cannot be expressed with the probed key."""


def is_known_level(level: str | None) -> bool:
    return (level or "default") in EFFORT_LEVELS


def request_fields(thinking_key: str | None, level: str | None) -> dict[str, Any]:
    """Request-body fields that ask for ``level`` under ``thinking_key``.

    ``default`` (or ``None``) -> ``{}``: nothing is sent. A switch-only key
    (the chat-template ones) maps every thinking level to ``True``; a graded
    level other than ``on`` on such a key is still accepted here — whether a
    level is OFFERED is the probe's decision (``levels``), not this mapper's.
    Raises :class:`UnsupportedEffortError` for an unknown level or key."""
    level = level or "default"
    if level not in EFFORT_LEVELS:
        raise UnsupportedEffortError(f"unknown effort level {level!r}")
    if level == "default":
        return {}
    if thinking_key == REASONING_EFFORT:
        if level == "off":
            return {REASONING_EFFORT: _EFFORT_OFF_VALUE}
        if level == "on":
            return {REASONING_EFFORT: "high"}
        return {REASONING_EFFORT: level}
    if thinking_key in (ENABLE_THINKING, THINKING):
        name = thinking_key.split(".", 1)[1]
        return {"chat_template_kwargs": {name: level != "off"}}
    raise UnsupportedEffortError(f"no thinking key for effort {level!r}")


def wire_fields(fields: dict[str, Any]) -> dict[str, Any] | None:
    """``{key, value}`` of what ``request_fields`` put on the wire (dotted
    key for nested fields), ``None`` when nothing was sent. Stored on the
    bench entry as ``effort_sent``."""
    if not fields:
        return None
    if REASONING_EFFORT in fields:
        return {"key": REASONING_EFFORT, "value": fields[REASONING_EFFORT]}
    kwargs = fields.get("chat_template_kwargs") or {}
    for name, value in kwargs.items():
        return {"key": f"chat_template_kwargs.{name}", "value": value}
    return None


def max_tokens_for(level: str | None, base: int, context_limit: int | None = None) -> int:
    """``base`` scaled for deep thinking (``high`` x2, ``max`` x4, ``on`` x2).
    When the engine's context limit is known the scaled value stays below it
    minus room for the prompt (a request over the limit is a 400); it never
    drops below ``base``, today's budget for every run."""
    scaled = base * _MAX_TOKENS_FACTOR.get(level or "default", 1)
    if context_limit and context_limit > _PROMPT_HEADROOM:
        scaled = min(scaled, context_limit - _PROMPT_HEADROOM)
    return max(scaled, base)


def reasoning_of(message: dict[str, Any] | None, usage: dict[str, Any] | None) -> dict[str, Any]:
    """What the response shows about thinking: ``reasoning_tokens`` from
    ``usage.completion_tokens_details`` when the engine reports it, and
    ``reasoning_chars`` from the reasoning text (``reasoning_content``,
    ``reasoning`` or a ``<think>`` block in ``content`` — engines differ).
    An engine without a reasoning parser whose chat template already puts
    ``<think>`` into the PROMPT answers ``…</think>42``: the text before a
    lone ``</think>`` is thinking too.
    ``thinking`` = "on" when either is > 0, else "off"."""
    message = message or {}
    usage = usage or {}
    text = (message.get("reasoning_content") or "") + (message.get("reasoning") or "")
    content = message.get("content") or ""
    lower = content.lower()
    start = lower.find("<think>")
    close = lower.find("</think>")
    if start >= 0 and (close < 0 or start < close):
        end = lower.find("</think>", start)
        text += content[start + 7 : end if end >= 0 else len(content)]
    elif close >= 0:
        text += content[:close]
    details = usage.get("completion_tokens_details") or {}
    tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    chars = len(text.strip())
    on = bool(chars) or bool(tokens)
    return {
        "thinking": "on" if on else "off",
        "reasoning_tokens": int(tokens) if isinstance(tokens, (int, float)) else None,
        "reasoning_chars": chars,
    }
