from __future__ import annotations

from typing import Any

from app.services.workflow_validator import WorkflowValidationError


AI_NEWS_KIND = "ai_news_briefing"
AI_NEWS_SOURCE_PROFILES = {"official", "balanced", "broad"}
AI_NEWS_FACT_CHECK_LEVELS = {"fast", "balanced", "strict"}


def compile_guided_workflow_payload(payload: dict[str, Any]) -> dict[str, Any]:
    execution_policy = dict(payload.get("execution_policy") or {})
    workflow_kind = str(execution_policy.get("workflow_kind") or "").strip()
    if workflow_kind != AI_NEWS_KIND:
        return payload

    guided_config = _normalize_ai_news_config(execution_policy.get("guided_config"))
    compiled_payload = dict(payload)
    compiled_payload["execution_policy"] = {
        "workflow_kind": AI_NEWS_KIND,
        "guided_config": guided_config,
    }
    compiled_payload["current_definition"] = {
        "steps": _build_ai_news_steps(guided_config)
    }
    if not compiled_payload.get("description"):
        compiled_payload["description"] = (
            f"AI News Briefing for the last {guided_config['timeframe_hours']} hours."
        )
    return compiled_payload


def _normalize_ai_news_config(raw: Any) -> dict[str, Any]:
    source = raw if isinstance(raw, dict) else {}
    source_profile = str(source.get("source_profile") or "balanced").strip().lower()
    if source_profile not in AI_NEWS_SOURCE_PROFILES:
        source_profile = "balanced"

    fact_check_level = str(source.get("fact_check_level") or "strict").strip().lower()
    if fact_check_level not in AI_NEWS_FACT_CHECK_LEVELS:
        fact_check_level = "strict"

    timeframe_hours = _clamp_int(source.get("timeframe_hours"), default=24, minimum=6, maximum=168)
    max_items = _clamp_int(source.get("max_items"), default=7, minimum=3, maximum=10)
    openclaw_items = _clamp_int(source.get("openclaw_items"), default=2, minimum=1, maximum=3)

    return {
        "agent_id": str(source.get("agent_id") or "").strip(),
        "topic_focus": str(source.get("topic_focus") or "").strip(),
        "custom_instructions": str(source.get("custom_instructions") or "").strip(),
        "timeframe_hours": timeframe_hours,
        "max_items": max_items,
        "source_profile": source_profile,
        "fact_check_level": fact_check_level,
        "include_impacts": bool(source.get("include_impacts", True)),
        "include_emojis": bool(source.get("include_emojis", True)),
        "include_openclaw_corner": bool(source.get("include_openclaw_corner", True)),
        "openclaw_items": openclaw_items,
    }


def _build_ai_news_steps(config: dict[str, Any]) -> list[dict[str, Any]]:
    agent_id = str(config.get("agent_id") or "").strip()
    if not agent_id:
        raise WorkflowValidationError("AI News Briefing requires an agent_id")

    steps: list[dict[str, Any]] = []
    if config.get("include_openclaw_corner", True):
        steps.append(
            {
                "key": "openclaw_skills_snapshot",
                "name": "Load OpenClaw skills snapshot",
                "step_type": "deterministic",
                "execution_mode": "single",
                "output_type": "json",
                "timeout_seconds": 60,
                "on_error": "skip",
                "retry_max_attempts": 1,
                "retry_delay_seconds": 5,
                "retry_backoff": "linear",
                "executor_type": "internal_api",
                "executor_config": {
                    "method": "GET",
                    "path": "/api/v1/skills",
                },
            }
        )

    prompt = _build_ai_news_prompt(config)
    steps.append(
        {
            "key": "compose_ai_news_briefing",
            "name": "Compose AI news briefing",
            "step_type": "llm",
            "execution_mode": "single",
            "output_type": "text",
            "timeout_seconds": 900,
            "on_error": "abort",
            "retry_max_attempts": 0,
            "retry_delay_seconds": 0,
            "retry_backoff": "linear",
            "agent_id": agent_id,
            "input_template": prompt,
        }
    )

    return steps


def _build_ai_news_prompt(config: dict[str, Any]) -> str:
    source_profile = config["source_profile"]
    fact_check_level = config["fact_check_level"]
    timeframe_hours = config["timeframe_hours"]
    max_items = config["max_items"]
    include_impacts = bool(config["include_impacts"])
    include_emojis = bool(config["include_emojis"])
    include_openclaw_corner = bool(config["include_openclaw_corner"])
    openclaw_items = config["openclaw_items"]
    topic_focus = str(config.get("topic_focus") or "").strip()
    custom_instructions = str(config.get("custom_instructions") or "").strip()

    profile_text = {
        "official": "Prioritize official company blogs, model providers, research labs and primary announcements. Use press coverage only as supporting context.",
        "balanced": "Prioritize official announcements, then add reliable tech press or research coverage when it helps confirm impact or context.",
        "broad": "Cast a wider net across official sources, reliable tech press and noteworthy secondary reporting, but clearly mark lower-confidence items.",
    }[source_profile]

    fact_check_text = {
        "fast": "Move quickly. Prefer at least one credible source per story and explicitly flag weaker verification.",
        "balanced": "Use multiple sources when practical. Prefer a primary source and one confirming source for major stories.",
        "strict": "Be conservative. Prefer a primary source plus a confirming source. If only one credible source exists, say so explicitly and lower confidence.",
    }[fact_check_level]

    lines = [
        "You are preparing an AI News Briefing for Discord.",
        "",
        f"Find the {max_items} most important AI news stories from the last {timeframe_hours} hours.",
        "Use web/search tools if they are available to the agent.",
        "",
        "Source profile:",
        profile_text,
        "",
        "Fact-check policy:",
        fact_check_text,
        "",
        "Selection rules:",
        "- Prioritize relevance over volume.",
        "- Deduplicate repeated coverage of the same story.",
        "- Avoid weak rumors unless you clearly label them as unconfirmed.",
        "- Prefer primary sources whenever possible.",
    ]

    if topic_focus:
        lines.extend(["", "Topic focus:", topic_focus])

    lines.extend(
        [
            "",
            "For each news item include:",
            "- A short headline",
            "- A 2-3 sentence summary",
            "- Why it matters",
        ]
    )

    if include_impacts:
        lines.append("- Potential impact")

    lines.extend(
        [
            "- Sources",
            "- Confidence: High, Medium, or Low",
            "",
            "Formatting rules:",
            "- Write in English.",
            "- Use Discord-friendly markdown.",
            "- Keep the briefing easy to scan.",
            f"- {'Use emojis sparingly and intentionally.' if include_emojis else 'Do not use emojis.'}",
            "",
            "If a story only has one credible source, mention that clearly.",
            "If you are uncertain, say so explicitly instead of guessing.",
        ]
    )

    if include_openclaw_corner:
        lines.extend(
            [
                "",
                f"At the end include an 'OpenClaw Corner' with {openclaw_items} short bullets.",
                "Use the provided OpenClaw skills snapshot if it is available.",
                "If the snapshot is missing or empty, say that there are no fresh OpenClaw highlights today.",
                "",
                "OpenClaw skills snapshot JSON:",
                "{{steps.openclaw_skills_snapshot.output_text}}",
            ]
        )

    if custom_instructions:
        lines.extend(
            [
                "",
                "Additional editorial guidance:",
                custom_instructions,
            ]
        )

    return "\n".join(lines)


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))
