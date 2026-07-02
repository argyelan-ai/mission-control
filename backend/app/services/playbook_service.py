from __future__ import annotations

import uuid
from typing import Any

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.models.playbook import (
    Automation,
    Playbook,
    PlaybookVersion,
    SkillCandidate,
    SkillPack,
)
from app.models.workflow import WorkflowRun, WorkflowTemplate
from app.services.playbook_catalog import (
    build_playbook_preview,
    build_playbook_prompt,
    get_playbook_definition,
    list_playbook_catalog,
    normalize_playbook_config,
)
from app.services.scheduler import scheduler
from app.services.workflow_service import workflow_service
from app.services.workflow_validator import WorkflowValidationError
from app.utils import utcnow


PLAYBOOK_ACTIVE_STATUSES = {"active"}


class PlaybookService:
    def get_catalog(self) -> list[dict[str, Any]]:
        return list_playbook_catalog()

    async def list_skill_packs(self, session: AsyncSession) -> list[SkillPack]:
        result = await session.exec(select(SkillPack).order_by(SkillPack.category.asc(), SkillPack.name.asc()))
        return result.all()

    async def list_playbooks(
        self,
        session: AsyncSession,
        *,
        board_id: uuid.UUID | None = None,
        include_archived: bool = False,
    ) -> list[Playbook]:
        query = select(Playbook).order_by(Playbook.updated_at.desc())
        if board_id:
            query = query.where((Playbook.board_id == board_id) | (Playbook.board_id.is_(None)))
        if not include_archived:
            query = query.where(Playbook.status != "archived")
        result = await session.exec(query)
        return result.all()

    async def get_playbook(self, session: AsyncSession, playbook_id: uuid.UUID) -> Playbook | None:
        return await session.get(Playbook, playbook_id)

    async def create_playbook(
        self,
        session: AsyncSession,
        payload: dict[str, Any],
        *,
        created_by: str,
    ) -> Playbook:
        definition = get_playbook_definition(str(payload.get("kind") or ""))
        skill_pack = await self._resolve_skill_pack(session, payload.get("skill_pack_id"), definition["default_skill_pack_key"])
        normalized_config = normalize_playbook_config(definition["key"], payload.get("current_config"))
        preview = build_playbook_preview(
            definition["key"],
            name=str(payload["name"]).strip(),
            goal=payload.get("goal"),
            config=normalized_config,
        )

        workflow_payload = self._build_workflow_payload(
            kind=definition["key"],
            name=str(payload["name"]).strip(),
            summary=payload.get("summary"),
            goal=payload.get("goal"),
            board_id=payload.get("board_id"),
            project_id=payload.get("project_id"),
            agent_id=payload.get("default_agent_id"),
            skill_pack=skill_pack,
            config=normalized_config,
            status=self._playbook_to_workflow_status(payload.get("status", "draft")),
        )
        workflow = await workflow_service.create_workflow(
            session,
            workflow_payload,
            created_by=created_by,
        )

        playbook = Playbook(
            workflow_id=workflow.id,
            board_id=payload.get("board_id"),
            project_id=payload.get("project_id"),
            skill_pack_id=skill_pack.id if skill_pack else None,
            default_agent_id=payload.get("default_agent_id"),
            kind=definition["key"],
            name=str(payload["name"]).strip(),
            summary=payload.get("summary"),
            goal=payload.get("goal"),
            scope=payload.get("scope", "global"),
            status=payload.get("status", "draft"),
            current_version=1,
            input_contract={"fields": definition["fields"]},
            output_contract=definition.get("output_contract"),
            current_config=normalized_config,
            preview_markdown=preview,
            extra_metadata=payload.get("metadata"),
            review_notes=payload.get("review_notes"),
            created_by=created_by,
        )
        if playbook.status == "active":
            playbook.approved_by = created_by
            playbook.approved_at = utcnow()

        session.add(playbook)
        await session.commit()
        await session.refresh(playbook)
        await self._store_version(session, playbook, version_number=1, created_by=created_by)
        return playbook

    async def update_playbook(
        self,
        session: AsyncSession,
        playbook: Playbook,
        payload: dict[str, Any],
        *,
        updated_by: str,
    ) -> Playbook:
        definition = get_playbook_definition(payload.get("kind", playbook.kind))
        skill_pack = await self._resolve_skill_pack(
            session,
            payload.get("skill_pack_id", playbook.skill_pack_id),
            definition["default_skill_pack_key"],
        )
        merged_config = normalize_playbook_config(
            definition["key"],
            {**(playbook.current_config or {}), **(payload.get("current_config") or {})},
        )
        next_name = str(payload.get("name", playbook.name)).strip()
        next_summary = payload.get("summary", playbook.summary)
        next_goal = payload.get("goal", playbook.goal)
        next_status = payload.get("status", playbook.status)
        next_board_id = payload.get("board_id", playbook.board_id)
        next_project_id = payload.get("project_id", playbook.project_id)
        next_agent_id = payload.get("default_agent_id", playbook.default_agent_id)

        workflow = await session.get(WorkflowTemplate, playbook.workflow_id) if playbook.workflow_id else None
        if not workflow:
            raise WorkflowValidationError("Linked workflow not found for playbook")

        workflow_payload = self._build_workflow_payload(
            kind=definition["key"],
            name=next_name,
            summary=next_summary,
            goal=next_goal,
            board_id=next_board_id,
            project_id=next_project_id,
            agent_id=next_agent_id,
            skill_pack=skill_pack,
            config=merged_config,
            status=self._playbook_to_workflow_status(next_status),
        )
        await workflow_service.update_workflow(
            session,
            workflow,
            workflow_payload,
            updated_by=updated_by,
        )
        self._sync_runtime_schedule(workflow)

        playbook.board_id = next_board_id
        playbook.project_id = next_project_id
        playbook.skill_pack_id = skill_pack.id if skill_pack else None
        playbook.default_agent_id = next_agent_id
        playbook.kind = definition["key"]
        playbook.name = next_name
        playbook.summary = next_summary
        playbook.goal = next_goal
        playbook.scope = payload.get("scope", playbook.scope)
        playbook.status = next_status
        playbook.input_contract = {"fields": definition["fields"]}
        playbook.output_contract = definition.get("output_contract")
        playbook.current_config = merged_config
        playbook.preview_markdown = build_playbook_preview(
            definition["key"],
            name=next_name,
            goal=next_goal,
            config=merged_config,
        )
        playbook.extra_metadata = payload.get("metadata", playbook.extra_metadata)
        playbook.review_notes = payload.get("review_notes", playbook.review_notes)
        playbook.updated_at = utcnow()
        if playbook.status == "active" and not playbook.approved_at:
            playbook.approved_by = updated_by
            playbook.approved_at = utcnow()

        session.add(playbook)
        await session.commit()
        await session.refresh(playbook)
        return playbook

    async def approve_playbook(
        self,
        session: AsyncSession,
        playbook: Playbook,
        *,
        approved_by: str,
    ) -> Playbook:
        workflow = await session.get(WorkflowTemplate, playbook.workflow_id) if playbook.workflow_id else None
        if not workflow:
            raise WorkflowValidationError("Linked workflow not found for playbook")
        await workflow_service.update_workflow(
            session,
            workflow,
            {"status": "active"},
            updated_by=approved_by,
        )
        self._sync_runtime_schedule(workflow)

        playbook.status = "active"
        playbook.approved_by = approved_by
        playbook.approved_at = utcnow()
        playbook.updated_at = utcnow()
        session.add(playbook)
        await session.commit()
        await session.refresh(playbook)
        return playbook

    async def get_versions(self, session: AsyncSession, playbook_id: uuid.UUID) -> list[PlaybookVersion]:
        result = await session.exec(
            select(PlaybookVersion)
            .where(PlaybookVersion.playbook_id == playbook_id)
            .order_by(PlaybookVersion.version.desc())
        )
        return result.all()

    async def create_version(
        self,
        session: AsyncSession,
        playbook: Playbook,
        *,
        created_by: str,
        change_reason: str | None = None,
    ) -> PlaybookVersion:
        result = await session.exec(
            select(PlaybookVersion.version)
            .where(PlaybookVersion.playbook_id == playbook.id)
            .order_by(PlaybookVersion.version.desc())
        )
        latest = result.first() or 0
        version = await self._store_version(
            session,
            playbook,
            version_number=int(latest) + 1,
            created_by=created_by,
            change_reason=change_reason,
        )
        playbook.current_version = version.version
        playbook.updated_at = utcnow()
        session.add(playbook)
        await session.commit()
        await session.refresh(playbook)
        return version

    async def list_automations(
        self,
        session: AsyncSession,
        *,
        board_id: uuid.UUID | None = None,
    ) -> list[Automation]:
        query = select(Automation).order_by(Automation.updated_at.desc())
        if board_id:
            query = query.where((Automation.board_id == board_id) | (Automation.board_id.is_(None)))
        result = await session.exec(query)
        return result.all()

    async def get_automation(self, session: AsyncSession, automation_id: uuid.UUID) -> Automation | None:
        return await session.get(Automation, automation_id)

    async def create_automation(
        self,
        session: AsyncSession,
        playbook: Playbook,
        payload: dict[str, Any],
        *,
        created_by: str,
    ) -> Automation:
        workflow_payload = await self._build_automation_workflow_payload(session, playbook, payload)
        workflow = await workflow_service.create_workflow(session, workflow_payload, created_by=created_by)
        self._sync_runtime_schedule(workflow)

        automation = Automation(
            playbook_id=playbook.id,
            workflow_id=workflow.id,
            board_id=payload.get("board_id", playbook.board_id),
            project_id=payload.get("project_id", playbook.project_id),
            name=str(payload.get("name") or f"{playbook.name} Automation").strip(),
            summary=payload.get("summary"),
            status=payload.get("status", "draft"),
            trigger_type=payload.get("trigger_type", "scheduled"),
            trigger_config=payload.get("trigger_config"),
            delivery_config=payload.get("delivery_config"),
            runtime_overrides=payload.get("runtime_overrides"),
            next_run_at=workflow.next_run_at,
            created_by=created_by,
        )
        session.add(automation)
        await session.commit()
        await session.refresh(automation)
        return automation

    async def update_automation(
        self,
        session: AsyncSession,
        automation: Automation,
        payload: dict[str, Any],
        *,
        updated_by: str,
    ) -> Automation:
        playbook = await session.get(Playbook, automation.playbook_id)
        if not playbook:
            raise WorkflowValidationError("Automation playbook not found")
        workflow = await session.get(WorkflowTemplate, automation.workflow_id) if automation.workflow_id else None
        if not workflow:
            raise WorkflowValidationError("Linked workflow not found for automation")

        merged_payload = {
            "name": payload.get("name", automation.name),
            "summary": payload.get("summary", automation.summary),
            "status": payload.get("status", automation.status),
            "trigger_type": payload.get("trigger_type", automation.trigger_type),
            "trigger_config": payload.get("trigger_config", automation.trigger_config),
            "delivery_config": payload.get("delivery_config", automation.delivery_config),
            "runtime_overrides": payload.get("runtime_overrides", automation.runtime_overrides),
            "board_id": payload.get("board_id", automation.board_id),
            "project_id": payload.get("project_id", automation.project_id),
        }
        workflow_payload = await self._build_automation_workflow_payload(session, playbook, merged_payload)
        await workflow_service.update_workflow(session, workflow, workflow_payload, updated_by=updated_by)
        self._sync_runtime_schedule(workflow)

        automation.board_id = merged_payload["board_id"]
        automation.project_id = merged_payload["project_id"]
        automation.name = merged_payload["name"]
        automation.summary = merged_payload["summary"]
        automation.status = merged_payload["status"]
        automation.trigger_type = merged_payload["trigger_type"]
        automation.trigger_config = merged_payload["trigger_config"]
        automation.delivery_config = merged_payload["delivery_config"]
        automation.runtime_overrides = merged_payload["runtime_overrides"]
        automation.next_run_at = workflow.next_run_at
        automation.updated_at = utcnow()
        session.add(automation)
        await session.commit()
        await session.refresh(automation)
        return automation

    async def run_automation(self, session: AsyncSession, automation: Automation) -> WorkflowRun:
        workflow = await session.get(WorkflowTemplate, automation.workflow_id) if automation.workflow_id else None
        if not workflow:
            raise WorkflowValidationError("Linked workflow not found for automation")
        run = await workflow_service.start_run(
            session,
            workflow,
            triggered_by="user",
            trigger_payload={"automation_id": str(automation.id)},
        )
        automation.last_run_at = run.started_at
        session.add(automation)
        await session.commit()
        return run

    async def list_recent_runs(
        self,
        session: AsyncSession,
        *,
        board_id: uuid.UUID | None = None,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        playbooks = await self.list_playbooks(session, board_id=board_id, include_archived=True)
        automations = await self.list_automations(session, board_id=board_id)
        workflow_to_playbook = {str(playbook.workflow_id): playbook for playbook in playbooks if playbook.workflow_id}
        workflow_to_automation = {str(automation.workflow_id): automation for automation in automations if automation.workflow_id}
        workflow_ids = list({*workflow_to_playbook.keys(), *workflow_to_automation.keys()})
        if not workflow_ids:
            return []

        result = await session.exec(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id.in_([uuid.UUID(item) for item in workflow_ids]))  # type: ignore[arg-type]
            .order_by(WorkflowRun.started_at.desc())
            .limit(limit)
        )
        runs: list[dict[str, Any]] = []
        for run in result.all():
            playbook = workflow_to_playbook.get(str(run.workflow_id))
            automation = workflow_to_automation.get(str(run.workflow_id))
            runs.append(
                {
                    "run": run.model_dump(),
                    "playbook": playbook.model_dump() if playbook else None,
                    "automation": automation.model_dump() if automation else None,
                }
            )
        return runs

    async def list_skill_candidates(
        self,
        session: AsyncSession,
        *,
        board_id: uuid.UUID | None = None,
    ) -> list[SkillCandidate]:
        query = select(SkillCandidate).order_by(SkillCandidate.updated_at.desc())
        if board_id:
            query = query.where((SkillCandidate.board_id == board_id) | (SkillCandidate.board_id.is_(None)))
        result = await session.exec(query)
        return result.all()

    async def get_skill_candidate(self, session: AsyncSession, candidate_id: uuid.UUID) -> SkillCandidate | None:
        return await session.get(SkillCandidate, candidate_id)

    async def update_skill_candidate(
        self,
        session: AsyncSession,
        candidate: SkillCandidate,
        payload: dict[str, Any],
        *,
        reviewed_by: str,
    ) -> SkillCandidate:
        for key in ("title", "summary", "status", "target_skill_key", "evidence", "source_run_ids", "draft_skill_content"):
            if key in payload:
                setattr(candidate, key, payload[key])
        if "status" in payload and payload["status"] in {"approved", "rejected", "applied"}:
            candidate.reviewed_by = reviewed_by
            candidate.reviewed_at = utcnow()
        candidate.updated_at = utcnow()
        session.add(candidate)
        await session.commit()
        await session.refresh(candidate)
        return candidate

    async def _resolve_skill_pack(
        self,
        session: AsyncSession,
        skill_pack_id: uuid.UUID | str | None,
        default_key: str,
    ) -> SkillPack | None:
        if skill_pack_id:
            try:
                resolved_id = uuid.UUID(str(skill_pack_id))
            except ValueError as exc:
                raise WorkflowValidationError("Invalid skill_pack_id") from exc
            pack = await session.get(SkillPack, resolved_id)
            if not pack:
                raise WorkflowValidationError("Unknown skill pack")
            return pack

        result = await session.exec(select(SkillPack).where(SkillPack.key == default_key))
        return result.first()

    def _build_workflow_payload(
        self,
        *,
        kind: str,
        name: str,
        summary: str | None,
        goal: str | None,
        board_id: uuid.UUID | None,
        project_id: uuid.UUID | None,
        agent_id: uuid.UUID | str | None,
        skill_pack: SkillPack | None,
        config: dict[str, Any],
        status: str,
        trigger_type: str = "manual",
        trigger_config: dict[str, Any] | None = None,
        delivery_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not agent_id:
            raise WorkflowValidationError("Playbook needs a default_agent_id")

        prompt = build_playbook_prompt(
            kind,
            name=name,
            summary=summary,
            goal=goal,
            config=config,
            skill_pack_name=skill_pack.name if skill_pack else None,
        )
        definition = get_playbook_definition(kind)
        return {
            "name": name,
            "description": summary or definition["summary"],
            "board_id": board_id,
            "project_id": project_id,
            "trigger_type": trigger_type,
            "trigger_config": trigger_config,
            "status": status,
            "current_definition": {
                "steps": [
                    {
                        "key": "run_playbook",
                        "name": f"Run {definition['name']}",
                        "step_type": "llm",
                        "execution_mode": "single",
                        "output_type": "text",
                        "timeout_seconds": 900,
                        "on_error": "abort",
                        "retry_max_attempts": 0,
                        "retry_delay_seconds": 0,
                        "retry_backoff": "linear",
                        "agent_id": str(agent_id),
                        "skill_key": skill_pack.key if skill_pack else None,
                        "input_template": prompt,
                        "evaluation_contract": {
                            "type": "section_presence",
                            "required_sections": definition.get("output_contract", {}).get("sections", []),
                        },
                    }
                ]
            },
            "execution_policy": {
                "product_layer": "playbook",
                "playbook_kind": kind,
                "skill_pack_key": skill_pack.key if skill_pack else None,
                "config": config,
            },
            "delivery_config": delivery_config,
        }

    async def _build_automation_workflow_payload(
        self,
        session: AsyncSession,
        playbook: Playbook,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        skill_pack = await self._resolve_skill_pack(
            session,
            playbook.skill_pack_id,
            get_playbook_definition(playbook.kind)["default_skill_pack_key"],
        )
        workflow_payload = self._build_workflow_payload(
            kind=playbook.kind,
            name=str(payload.get("name") or f"{playbook.name} Automation").strip(),
            summary=payload.get("summary") or playbook.summary,
            goal=playbook.goal,
            board_id=payload.get("board_id", playbook.board_id),
            project_id=payload.get("project_id", playbook.project_id),
            agent_id=playbook.default_agent_id,
            skill_pack=skill_pack,
            config=playbook.current_config or {},
            status=self._automation_to_workflow_status(payload.get("status", "draft")),
            trigger_type=payload.get("trigger_type", "scheduled"),
            trigger_config=payload.get("trigger_config"),
            delivery_config=payload.get("delivery_config"),
        )
        workflow_payload["execution_policy"] = {
            **(workflow_payload.get("execution_policy") or {}),
            "product_layer": "automation",
            "playbook_id": str(playbook.id),
            "runtime_overrides": payload.get("runtime_overrides"),
        }
        return workflow_payload

    def _playbook_to_workflow_status(self, status: str) -> str:
        return "active" if status in PLAYBOOK_ACTIVE_STATUSES else "draft"

    def _automation_to_workflow_status(self, status: str) -> str:
        if status == "active":
            return "active"
        if status == "archived":
            return "archived"
        return "draft"

    def _sync_runtime_schedule(self, workflow: WorkflowTemplate) -> None:
        if workflow.status == "active" and workflow.trigger_type == "scheduled":
            scheduler.register_workflow(workflow)
            return
        scheduler.unregister_workflow(str(workflow.id))

    async def _store_version(
        self,
        session: AsyncSession,
        playbook: Playbook,
        *,
        version_number: int,
        created_by: str,
        change_reason: str | None = None,
    ) -> PlaybookVersion:
        version = PlaybookVersion(
            playbook_id=playbook.id,
            version=version_number,
            snapshot=self._build_playbook_snapshot(playbook),
            change_reason=change_reason,
            created_by=created_by,
        )
        session.add(version)
        await session.commit()
        await session.refresh(version)
        return version

    def _build_playbook_snapshot(self, playbook: Playbook) -> dict[str, Any]:
        return {
            "id": str(playbook.id),
            "workflow_id": str(playbook.workflow_id) if playbook.workflow_id else None,
            "board_id": str(playbook.board_id) if playbook.board_id else None,
            "project_id": str(playbook.project_id) if playbook.project_id else None,
            "skill_pack_id": str(playbook.skill_pack_id) if playbook.skill_pack_id else None,
            "default_agent_id": str(playbook.default_agent_id) if playbook.default_agent_id else None,
            "kind": playbook.kind,
            "name": playbook.name,
            "summary": playbook.summary,
            "goal": playbook.goal,
            "scope": playbook.scope,
            "status": playbook.status,
            "current_version": playbook.current_version,
            "input_contract": playbook.input_contract,
            "output_contract": playbook.output_contract,
            "current_config": playbook.current_config,
            "preview_markdown": playbook.preview_markdown,
            "extra_metadata": playbook.extra_metadata,
            "review_notes": playbook.review_notes,
            "approved_by": playbook.approved_by,
            "approved_at": playbook.approved_at.isoformat() if playbook.approved_at else None,
        }


playbook_service = PlaybookService()
