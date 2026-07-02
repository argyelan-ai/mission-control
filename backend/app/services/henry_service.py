"""Henry playbook orchestration service.

Deprecated as of Workstream C1 (2026-04-20): the field-validation-driven
playbook chat is on its way out. Henry's long-term role is a messenger —
paraphrase the operator's intent, forward a structured ask to Boss via chat_send,
wait for Boss's reply, paraphrase back. See plan doc
`docs/superpowers/plans/2026-04-20-harness-personas-session-handoff.md`.

For now the service keeps working so legacy playbook UIs don't break, but
new sessions should go through the direct agent chat path instead. Do not
add features to `_apply_answer` / `_parse_field_answer`; they'll be
removed once the UI migration is done.
"""
from __future__ import annotations

import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.agent import Agent
from app.models.board import PlannerMessage, Project
from app.models.playbook import Playbook
from app.services.playbook_catalog import get_playbook_definition, normalize_playbook_config
from app.services.playbook_service import playbook_service
from app.services.workflow_validator import WorkflowValidationError
from app.utils import utcnow

HENRY_MODE = "henry_playbook"
LETTER_CHOICES = ["A", "B", "C", "D"]


class HenryService:
    async def get_current_session_state(
        self,
        session: AsyncSession,
        *,
        board_id: uuid.UUID,
    ) -> dict[str, Any] | None:
        result = await session.exec(
            select(Project)
            .where(Project.board_id == board_id)
            .where(Project.created_by == "henry")
            .order_by(Project.updated_at.desc())
        )
        projects = result.all()
        for project in projects:
            config = project.project_config or {}
            if config.get("mode") == HENRY_MODE:
                return await self._build_state(session, project)
        return None

    async def start_session(
        self,
        session: AsyncSession,
        *,
        board_id: uuid.UUID,
        created_by: str,
        kind: str | None = None,
        playbook_id: uuid.UUID | None = None,
    ) -> dict[str, Any]:
        playbook: Playbook | None = None
        if playbook_id:
            playbook = await session.get(Playbook, playbook_id)
            if not playbook:
                raise WorkflowValidationError("Playbook not found")
            kind = playbook.kind
        if not kind:
            raise WorkflowValidationError("Henry needs a playbook kind to start")

        definition = get_playbook_definition(kind)

        if not playbook:
            default_agent = await self._resolve_default_agent(session, board_id)
            if not default_agent:
                raise WorkflowValidationError("Henry needs at least one board agent before drafting a playbook")
            playbook = await playbook_service.create_playbook(
                session,
                {
                    "kind": definition["key"],
                    "name": definition["name"],
                    "summary": definition["summary"],
                    "board_id": board_id,
                    "default_agent_id": default_agent.id,
                    "scope": "board",
                    "current_config": {},
                },
                created_by=created_by,
            )

        answered_fields = self._answered_fields_from_playbook(playbook)
        pending_field_key = self._next_pending_field(playbook.kind, answered_fields)
        project = Project(
            board_id=board_id,
            name=f"Henry · {playbook.name}",
            description=playbook.summary or definition["summary"],
            project_type="automation",
            status="planning",
            created_by="henry",
            project_config={
                "mode": HENRY_MODE,
                "selected_kind": playbook.kind,
                "playbook_id": str(playbook.id),
                "pending_field_key": pending_field_key,
                "answered_fields": answered_fields,
                "stage": "review" if pending_field_key is None else "intake",
            },
        )
        session.add(project)
        await session.commit()
        await session.refresh(project)

        assistant_message = PlannerMessage(
            project_id=project.id,
            role="assistant",
            content=self._build_opening_message(playbook, pending_field_key),
        )
        session.add(assistant_message)
        await session.commit()

        return await self._build_state(session, project)

    async def send_message(
        self,
        session: AsyncSession,
        *,
        project_id: uuid.UUID,
        content: str,
        updated_by: str,
    ) -> dict[str, Any]:
        project = await session.get(Project, project_id)
        if not project or (project.project_config or {}).get("mode") != HENRY_MODE:
            raise WorkflowValidationError("Henry session not found")

        config = dict(project.project_config or {})
        playbook_id_raw = config.get("playbook_id")
        if not playbook_id_raw:
            raise WorkflowValidationError("Henry session lost its playbook link")
        playbook = await session.get(Playbook, uuid.UUID(str(playbook_id_raw)))
        if not playbook:
            raise WorkflowValidationError("Linked playbook not found")

        user_message = PlannerMessage(
            project_id=project.id,
            role="user",
            content=content.strip(),
        )
        session.add(user_message)
        await session.commit()

        pending_field_key = config.get("pending_field_key")
        answered_fields = list(config.get("answered_fields") or [])
        assistant_text: str

        if pending_field_key:
            success, error_text = await self._apply_answer(
                session,
                playbook=playbook,
                pending_field_key=str(pending_field_key),
                answer=content.strip(),
                updated_by=updated_by,
            )
            if not success:
                assistant_text = error_text
            else:
                if pending_field_key not in answered_fields:
                    answered_fields.append(str(pending_field_key))
                pending_field_key = self._next_pending_field(playbook.kind, answered_fields)
                config["answered_fields"] = answered_fields
                config["pending_field_key"] = pending_field_key
                config["stage"] = "review" if pending_field_key is None else "intake"
                project.project_config = config
                project.updated_at = utcnow()
                session.add(project)
                await session.commit()
                await session.refresh(playbook)
                assistant_text = self._build_follow_up_message(playbook, pending_field_key)
        else:
            assistant_text = self._build_review_message(playbook)

        assistant_message = PlannerMessage(
            project_id=project.id,
            role="assistant",
            content=assistant_text,
        )
        session.add(assistant_message)
        await session.commit()

        return await self._build_state(session, project)

    async def _build_state(self, session: AsyncSession, project: Project) -> dict[str, Any]:
        config = project.project_config or {}
        playbook: Playbook | None = None
        if config.get("playbook_id"):
            playbook = await session.get(Playbook, uuid.UUID(str(config["playbook_id"])))
        result = await session.exec(
            select(PlannerMessage)
            .where(PlannerMessage.project_id == project.id)
            .order_by(PlannerMessage.created_at)
        )
        messages = result.all()
        return {
            "session": project.model_dump(),
            "messages": [message.model_dump() for message in messages],
            "playbook": playbook.model_dump() if playbook else None,
            "selected_kind": config.get("selected_kind"),
            "pending_field_key": config.get("pending_field_key"),
            "stage": config.get("stage", "intake"),
        }

    async def _resolve_default_agent(self, session: AsyncSession, board_id: uuid.UUID) -> Agent | None:
        # Phase 30: gateway_agent_id preference dropped. is_board_lead ordering
        # already promotes the Board Lead to the front; the runtime-aware
        # delivery layer downstream handles online/offline state.
        result = await session.exec(
            select(Agent)
            .where(Agent.board_id == board_id)
            .order_by(Agent.is_board_lead.desc(), Agent.updated_at.desc())  # type: ignore[union-attr]
        )
        agents = result.all()
        return agents[0] if agents else None

    def _answered_fields_from_playbook(self, playbook: Playbook) -> list[str]:
        answered: list[str] = []
        if playbook.goal:
            answered.append("goal")
        definition = get_playbook_definition(playbook.kind)
        config = normalize_playbook_config(playbook.kind, playbook.current_config)
        for field in definition["fields"]:
            if not field.get("required"):
                continue
            value = config.get(field["key"])
            if self._field_has_value(field, value):
                answered.append(field["key"])
        return answered

    def _required_field_order(self, kind: str) -> list[str]:
        definition = get_playbook_definition(kind)
        ordered = ["goal"]
        ordered.extend(field["key"] for field in definition["fields"] if field.get("required"))
        return ordered

    def _next_pending_field(self, kind: str, answered_fields: list[str]) -> str | None:
        answered_set = set(answered_fields)
        for key in self._required_field_order(kind):
            if key not in answered_set:
                return key
        return None

    async def _apply_answer(
        self,
        session: AsyncSession,
        *,
        playbook: Playbook,
        pending_field_key: str,
        answer: str,
        updated_by: str,
    ) -> tuple[bool, str]:
        cleaned = answer.strip()
        if not cleaned:
            return False, "I need a bit more detail before I can shape the draft. Please answer in one or two sentences."

        if pending_field_key == "goal":
            await playbook_service.update_playbook(
                session,
                playbook,
                {"goal": cleaned},
                updated_by=updated_by,
            )
            return True, ""

        definition = get_playbook_definition(playbook.kind)
        field = next((item for item in definition["fields"] if item["key"] == pending_field_key), None)
        if not field:
            return False, "Henry lost track of the next field. Please restart this guided setup."

        parsed_value = self._parse_field_answer(field, cleaned)
        if parsed_value is None:
            return False, self._build_clarification_message(playbook, field)

        payload: dict[str, Any] = {
            "current_config": {**normalize_playbook_config(playbook.kind, playbook.current_config), field["key"]: parsed_value},
        }
        if field["key"] == "project_name":
            payload["name"] = cleaned
        await playbook_service.update_playbook(
            session,
            playbook,
            payload,
            updated_by=updated_by,
        )
        return True, ""

    def _parse_field_answer(self, field: dict[str, Any], answer: str) -> Any | None:
        normalized = answer.strip()
        field_type = field["type"]
        if field_type in {"short_text", "long_text"}:
            return normalized
        if field_type == "number":
            digits = "".join(ch for ch in normalized if ch.isdigit())
            return int(digits) if digits else None
        if field_type == "boolean":
            lowered = normalized.lower()
            if lowered in {"a", "yes", "y", "true", "enabled", "on"}:
                return True
            if lowered in {"b", "no", "n", "false", "disabled", "off"}:
                return False
            return None
        if field_type == "select":
            options = field.get("options") or []
            lowered = normalized.lower()
            for index, option in enumerate(options):
                if lowered == LETTER_CHOICES[index].lower():
                    return option["value"]
            for option in options:
                if lowered == str(option["value"]).lower() or lowered == str(option["label"]).lower():
                    return option["value"]
            for option in options:
                if str(option["label"]).lower() in lowered or str(option["value"]).lower() in lowered:
                    return option["value"]
            return None
        return normalized

    def _field_has_value(self, field: dict[str, Any], value: Any) -> bool:
        field_type = field["type"]
        if field_type == "boolean":
            return isinstance(value, bool)
        if field_type == "number":
            return value is not None
        return str(value or "").strip() != ""

    def _build_opening_message(self, playbook: Playbook, pending_field_key: str | None) -> str:
        intro = (
            f"Let's shape **{playbook.name}** together. "
            "I will ask only for the minimum information needed to produce a strong draft."
        )
        if pending_field_key is None:
            return f"{intro}\n\n{self._build_review_message(playbook)}"
        return f"{intro}\n\n{self._build_question(playbook, pending_field_key)}"

    def _build_follow_up_message(self, playbook: Playbook, pending_field_key: str | None) -> str:
        if pending_field_key is None:
            return self._build_review_message(playbook)
        return f"Captured. {self._build_question(playbook, pending_field_key)}"

    def _build_review_message(self, playbook: Playbook) -> str:
        return (
            f"Your **{playbook.name}** draft is ready for review.\n\n"
            "Use the panel on the right to fine-tune details, snapshot a version, and approve it when it feels right."
        )

    def _build_question(self, playbook: Playbook, field_key: str) -> str:
        if field_key == "goal":
            return "What outcome do you want this playbook to achieve?"

        definition = get_playbook_definition(playbook.kind)
        field = next(item for item in definition["fields"] if item["key"] == field_key)
        label = field["label"]
        placeholder = field.get("placeholder")

        if field["type"] == "select":
            options = field.get("options") or []
            option_lines = [
                f"[{LETTER_CHOICES[index]}] {option['label']} — choose this mode"
                for index, option in enumerate(options[:4])
            ]
            intro = f"Choose the best fit for **{label}**."
            return "\n".join([intro, "", *option_lines])

        if field["type"] == "boolean":
            return "\n".join(
                [
                    f"Should I enable **{label}**?",
                    "",
                    "[A] Yes — include it",
                    "[B] No — leave it out",
                ]
            )

        if placeholder:
            return f"{label}: {placeholder}"
        return f"Tell me the right value for **{label}**."

    def _build_clarification_message(self, playbook: Playbook, field: dict[str, Any]) -> str:
        return (
            f"I could not confidently map that answer to **{field['label']}**.\n\n"
            f"{self._build_question(playbook, field['key'])}"
        )


henry_service = HenryService()
