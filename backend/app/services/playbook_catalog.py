from __future__ import annotations

from typing import Any


DEFAULT_SKILL_PACKS: list[dict[str, Any]] = [
    {
        "key": "planning_stack",
        "name": "Planning Stack",
        "description": "Clear scoping, sequencing, tradeoff thinking and delivery planning.",
        "category": "planning",
        "icon": "Map",
        "color": "#7C3AED",
        "skill_keys": ["planning", "risk-review", "delivery-design"],
        "guidance": {
            "style": "structured",
            "strengths": ["scope control", "roadmaps", "delivery order"],
        },
    },
    {
        "key": "discovery_stack",
        "name": "Discovery Stack",
        "description": "Market framing, product discovery and synthesis for vague ideas.",
        "category": "research",
        "icon": "Compass",
        "color": "#2563EB",
        "skill_keys": ["research", "synthesis", "strategy"],
        "guidance": {
            "style": "curious",
            "strengths": ["market research", "opportunity framing", "competitor synthesis"],
        },
    },
    {
        "key": "repo_stack",
        "name": "Repo Stack",
        "description": "Repo triage, code shipping and verification-oriented execution.",
        "category": "build",
        "icon": "Boxes",
        "color": "#059669",
        "skill_keys": ["github", "repo-analysis", "verification"],
        "guidance": {
            "style": "technical",
            "strengths": ["repo analysis", "next-step planning", "release readiness"],
        },
    },
    {
        "key": "operations_stack",
        "name": "Operations Stack",
        "description": "Execution reviews, signal digestion and operational follow-through.",
        "category": "operate",
        "icon": "Radar",
        "color": "#EA580C",
        "skill_keys": ["ops-review", "prioritization", "signal-digest"],
        "guidance": {
            "style": "operational",
            "strengths": ["review", "prioritization", "follow-up generation"],
        },
    },
    {
        "key": "learning_stack",
        "name": "Learning Stack",
        "description": "Pattern extraction, skill drafting and safe improvement suggestions.",
        "category": "learn",
        "icon": "Sparkles",
        "color": "#DB2777",
        "skill_keys": ["pattern-detection", "skill-authoring", "evidence-review"],
        "guidance": {
            "style": "forensic",
            "strengths": ["pattern detection", "candidate generation", "evidence trails"],
        },
    },
]


CORE_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "key": "spec_to_delivery_plan",
        "name": "Spec to Delivery Plan",
        "summary": "Turn a rough spec or note into a concrete delivery plan.",
        "icon": "ClipboardList",
        "default_skill_pack_key": "planning_stack",
        "suggested_mode": "manual",
        "fields": [
            {"key": "source_text", "label": "Source material", "type": "long_text", "required": True, "placeholder": "Paste the spec, notes or raw idea."},
            {"key": "target_outcome", "label": "Target outcome", "type": "short_text", "required": True, "placeholder": "What should be true after this plan is executed?"},
            {"key": "scope_mode", "label": "Scope mode", "type": "select", "required": True, "default": "mvp", "options": [
                {"value": "mvp", "label": "MVP"},
                {"value": "phased", "label": "Phased rollout"},
                {"value": "full", "label": "Full implementation"},
            ]},
            {"key": "constraints", "label": "Constraints", "type": "long_text", "required": False, "placeholder": "Deadlines, hard requirements, things to avoid."},
            {"key": "include_risks", "label": "Include risks", "type": "boolean", "default": True},
        ],
        "output_contract": {
            "sections": ["Goal", "Scope", "Architecture Notes", "Phases", "Risks", "Dependencies", "Next Actions"],
        },
    },
    {
        "key": "product_discovery_sprint",
        "name": "Product Discovery Sprint",
        "summary": "Clarify a product idea, its audience, and the best opportunity angle.",
        "icon": "Compass",
        "default_skill_pack_key": "discovery_stack",
        "suggested_mode": "manual",
        "fields": [
            {"key": "idea_summary", "label": "Idea summary", "type": "long_text", "required": True, "placeholder": "Describe the product or opportunity."},
            {"key": "audience", "label": "Audience", "type": "short_text", "required": True, "placeholder": "Who is this for?"},
            {"key": "market_focus", "label": "Market focus", "type": "short_text", "required": False, "placeholder": "Market, niche, or industry."},
            {"key": "research_depth", "label": "Research depth", "type": "select", "required": True, "default": "standard", "options": [
                {"value": "standard", "label": "Standard"},
                {"value": "deep", "label": "Deep"},
            ]},
            {"key": "include_competitors", "label": "Include competitors", "type": "boolean", "default": True},
        ],
        "output_contract": {
            "sections": ["Idea Summary", "Audience", "Opportunity Map", "Competitor Snapshot", "Risks", "Recommended Next Steps"],
        },
    },
    {
        "key": "repo_shipping_assistant",
        "name": "Repo Shipping Assistant",
        "summary": "Inspect a repo and turn it into a concrete next shipping plan.",
        "icon": "Boxes",
        "default_skill_pack_key": "repo_stack",
        "suggested_mode": "manual",
        "fields": [
            {"key": "repository_name", "label": "Repository", "type": "short_text", "required": True, "placeholder": "owner/repo or local repo name"},
            {"key": "branch_name", "label": "Branch", "type": "short_text", "required": False, "placeholder": "main"},
            {"key": "objective", "label": "Objective", "type": "long_text", "required": True, "placeholder": "What are we trying to ship or unblock?"},
            {"key": "time_budget", "label": "Time budget", "type": "short_text", "required": False, "placeholder": "e.g. 1 day, this week"},
            {"key": "include_tests", "label": "Include tests and release checks", "type": "boolean", "default": True},
        ],
        "output_contract": {
            "sections": ["Current State", "Blockers", "Recommended Next Actions", "Validation", "Release Notes"],
        },
    },
    {
        "key": "execution_review",
        "name": "Execution Review",
        "summary": "Review recent work, highlight wins, bottlenecks and recommended moves.",
        "icon": "BarChart3",
        "default_skill_pack_key": "operations_stack",
        "suggested_mode": "scheduled",
        "fields": [
            {"key": "time_window", "label": "Time window", "type": "select", "required": True, "default": "7d", "options": [
                {"value": "7d", "label": "Last 7 days"},
                {"value": "14d", "label": "Last 14 days"},
                {"value": "30d", "label": "Last 30 days"},
            ]},
            {"key": "review_scope", "label": "Review scope", "type": "select", "required": True, "default": "board", "options": [
                {"value": "board", "label": "Board"},
                {"value": "project", "label": "Project"},
            ]},
            {"key": "focus_area", "label": "Focus area", "type": "short_text", "required": False, "placeholder": "Optional area to zoom in on."},
            {"key": "depth", "label": "Detail level", "type": "select", "required": True, "default": "concise", "options": [
                {"value": "concise", "label": "Concise"},
                {"value": "detailed", "label": "Detailed"},
            ]},
        ],
        "output_contract": {
            "sections": ["What Happened", "Wins", "Bottlenecks", "Missed Opportunities", "Recommended Actions"],
        },
    },
    {
        "key": "skill_extractor",
        "name": "Skill Extractor",
        "summary": "Turn repeated good work into a safe skill suggestion.",
        "icon": "Sparkles",
        "default_skill_pack_key": "learning_stack",
        "suggested_mode": "manual",
        "fields": [
            {"key": "source_run_notes", "label": "Source run notes", "type": "long_text", "required": True, "placeholder": "What repeated well? Which runs or outcomes matter?"},
            {"key": "candidate_type", "label": "Candidate type", "type": "select", "required": True, "default": "new_skill", "options": [
                {"value": "new_skill", "label": "New skill"},
                {"value": "patch", "label": "Improve existing skill"},
            ]},
            {"key": "target_skill_key", "label": "Target skill key", "type": "short_text", "required": False, "placeholder": "Required for patches."},
            {"key": "confidence_threshold", "label": "Confidence threshold", "type": "select", "required": True, "default": "balanced", "options": [
                {"value": "balanced", "label": "Balanced"},
                {"value": "strict", "label": "Strict"},
            ]},
        ],
        "output_contract": {
            "sections": ["Candidate Summary", "Evidence", "Suggested Skill", "Application Notes", "Approval Recommendation"],
        },
    },
    {
        "key": "project_planning",
        "name": "Project Planning",
        "summary": "Guide a product idea into a structured project plan and staged execution.",
        "icon": "Layers3",
        "default_skill_pack_key": "planning_stack",
        "suggested_mode": "manual",
        "fields": [
            {"key": "project_name", "label": "Project name", "type": "short_text", "required": True, "placeholder": "Name the project."},
            {"key": "project_type", "label": "Project type", "type": "select", "required": True, "default": "feature", "options": [
                {"value": "feature", "label": "Feature"},
                {"value": "website", "label": "Website / App"},
                {"value": "content", "label": "Content"},
                {"value": "research", "label": "Research"},
                {"value": "automation", "label": "Automation"},
                {"value": "design", "label": "Design"},
                {"value": "free", "label": "Freeform"},
            ]},
            {"key": "project_brief", "label": "Project brief", "type": "long_text", "required": True, "placeholder": "Describe the project, desired result and context."},
            {"key": "success_definition", "label": "Success definition", "type": "long_text", "required": False, "placeholder": "What does success look like?"},
        ],
        "output_contract": {
            "sections": ["Briefing", "Project Goal", "Technology or Approach", "Phase 1", "Phase 2", "Next Decision"],
        },
    },
]


def list_playbook_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in CORE_PLAYBOOKS]


def list_skill_pack_catalog() -> list[dict[str, Any]]:
    return [dict(item) for item in DEFAULT_SKILL_PACKS]


def get_playbook_definition(kind: str) -> dict[str, Any]:
    normalized = kind.strip().lower()
    for definition in CORE_PLAYBOOKS:
        if definition["key"] == normalized:
            return definition
    raise ValueError(f"Unknown playbook kind: {kind}")


def normalize_playbook_config(kind: str, raw_config: Any) -> dict[str, Any]:
    definition = get_playbook_definition(kind)
    source = raw_config if isinstance(raw_config, dict) else {}
    normalized: dict[str, Any] = {}
    for field in definition["fields"]:
        key = field["key"]
        default = field.get("default")
        value = source.get(key, default)
        field_type = field["type"]

        if field_type == "boolean":
            normalized[key] = bool(value)
            continue
        if field_type == "number":
            try:
                normalized[key] = int(value)
            except (TypeError, ValueError):
                normalized[key] = int(default or 0)
            continue

        normalized[key] = str(value or "").strip()

    return normalized


def build_playbook_preview(kind: str, *, name: str, goal: str | None, config: dict[str, Any]) -> str:
    definition = get_playbook_definition(kind)
    lines = [f"# {name}", "", definition["summary"]]
    if goal:
        lines.extend(["", f"Goal: {goal.strip()}"])

    if config:
        lines.extend(["", "Key inputs:"])
        for field in definition["fields"]:
            key = field["key"]
            value = config.get(key)
            if value in (None, "", [], {}):
                continue
            label = field["label"]
            if isinstance(value, bool):
                rendered = "Yes" if value else "No"
            else:
                rendered = str(value)
            lines.append(f"- {label}: {rendered}")

    contract = definition.get("output_contract") or {}
    sections = contract.get("sections") or []
    if sections:
        lines.extend(["", "Expected output:"])
        lines.extend([f"- {section}" for section in sections])

    return "\n".join(lines).strip()


def build_playbook_prompt(
    kind: str,
    *,
    name: str,
    summary: str | None,
    goal: str | None,
    config: dict[str, Any],
    skill_pack_name: str | None,
) -> str:
    intro = [
        f"You are running the '{name}' playbook inside Mission Control.",
        "Follow the playbook goal closely and produce a structured, high-signal result.",
        "Write in English.",
    ]
    if summary:
        intro.extend(["", f"Summary: {summary.strip()}"])
    if goal:
        intro.extend(["", f"Primary goal: {goal.strip()}"])
    if skill_pack_name:
        intro.extend(["", f"Preferred skill pack: {skill_pack_name}"])

    if kind == "spec_to_delivery_plan":
        return "\n".join(
            intro
            + [
                "",
                "Turn the source material into a concrete delivery plan.",
                f"Source material:\n{config.get('source_text', '')}",
                f"Target outcome: {config.get('target_outcome', '')}",
                f"Scope mode: {config.get('scope_mode', 'mvp')}",
                f"Constraints: {config.get('constraints', '') or 'None provided.'}",
                f"Include risks: {'yes' if config.get('include_risks', True) else 'no'}",
                "",
                "Return markdown with these sections:",
                "- Goal",
                "- Scope",
                "- Architecture Notes",
                "- Phases",
                "- Risks",
                "- Dependencies",
                "- Next Actions",
            ]
        )

    if kind == "product_discovery_sprint":
        return "\n".join(
            intro
            + [
                "",
                "Run a focused product discovery sprint.",
                f"Idea summary:\n{config.get('idea_summary', '')}",
                f"Audience: {config.get('audience', '')}",
                f"Market focus: {config.get('market_focus', '') or 'Open exploration'}",
                f"Research depth: {config.get('research_depth', 'standard')}",
                f"Include competitors: {'yes' if config.get('include_competitors', True) else 'no'}",
                "",
                "Return markdown with these sections:",
                "- Idea Summary",
                "- Audience",
                "- Opportunity Map",
                "- Competitor Snapshot",
                "- Risks",
                "- Recommended Next Steps",
            ]
        )

    if kind == "repo_shipping_assistant":
        return "\n".join(
            intro
            + [
                "",
                "Review the repo and propose the strongest next shipping move.",
                f"Repository: {config.get('repository_name', '')}",
                f"Branch: {config.get('branch_name', '') or 'main'}",
                f"Objective:\n{config.get('objective', '')}",
                f"Time budget: {config.get('time_budget', '') or 'Not specified'}",
                f"Include tests and release checks: {'yes' if config.get('include_tests', True) else 'no'}",
                "",
                "Return markdown with these sections:",
                "- Current State",
                "- Blockers",
                "- Recommended Next Actions",
                "- Validation",
                "- Release Notes",
            ]
        )

    if kind == "execution_review":
        return "\n".join(
            intro
            + [
                "",
                "Review recent execution and highlight what matters.",
                f"Time window: {config.get('time_window', '7d')}",
                f"Review scope: {config.get('review_scope', 'board')}",
                f"Focus area: {config.get('focus_area', '') or 'Whole scope'}",
                f"Detail level: {config.get('depth', 'concise')}",
                "",
                "Return markdown with these sections:",
                "- What Happened",
                "- Wins",
                "- Bottlenecks",
                "- Missed Opportunities",
                "- Recommended Actions",
            ]
        )

    if kind == "skill_extractor":
        return "\n".join(
            intro
            + [
                "",
                "Extract a safe skill suggestion from repeated good work.",
                f"Source run notes:\n{config.get('source_run_notes', '')}",
                f"Candidate type: {config.get('candidate_type', 'new_skill')}",
                f"Target skill key: {config.get('target_skill_key', '') or 'None specified'}",
                f"Confidence threshold: {config.get('confidence_threshold', 'balanced')}",
                "",
                "Return markdown with these sections:",
                "- Candidate Summary",
                "- Evidence",
                "- Suggested Skill",
                "- Application Notes",
                "- Approval Recommendation",
            ]
        )

    if kind == "project_planning":
        return "\n".join(
            intro
            + [
                "",
                "Create a practical project plan that can be reviewed before execution.",
                f"Project name: {config.get('project_name', '')}",
                f"Project type: {config.get('project_type', 'feature')}",
                f"Project brief:\n{config.get('project_brief', '')}",
                f"Success definition: {config.get('success_definition', '') or 'Not specified'}",
                "",
                "Return markdown with these sections:",
                "- Briefing",
                "- Project Goal",
                "- Technology or Approach",
                "- Phase 1",
                "- Phase 2",
                "- Next Decision",
            ]
        )

    raise ValueError(f"Unknown playbook kind: {kind}")
