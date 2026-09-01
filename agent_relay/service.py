"""Application service coordinating checkpoints, adapters, and action safety."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from .adapters import (
    AdapterRegistry,
    AgentExecutionResult,
    AntigravityCliAdapter,
    CliAgentAdapter,
)
from .errors import ConflictError, NotFoundError, ValidationError
from .models import ActionRecord, AgentSpec, TaskCheckpoint, utc_now
from .prompting import CheckpointPromptRenderer
from .storage import RelayStore


@dataclass(frozen=True)
class HandoffOutcome:
    """Observable result returned by preview and execute operations."""

    task: TaskCheckpoint
    prompt: str
    dry_run: bool
    action_id: Optional[str] = None
    execution: Optional[AgentExecutionResult] = None


class RelayService:
    """Coordinates safe, provider-neutral handoffs between registered runtimes."""

    def __init__(
        self,
        store: RelayStore,
        adapter: Optional[CliAgentAdapter] = None,
        renderer: Optional[CheckpointPromptRenderer] = None,
        adapter_registry: Optional[AdapterRegistry] = None,
    ) -> None:
        self.store = store
        if adapter is not None and adapter_registry is not None:
            raise ValidationError("pass adapter or adapter_registry, not both")
        self.adapters = adapter_registry or AdapterRegistry()
        if adapter_registry is None:
            self.adapter = adapter or CliAgentAdapter()
            self.adapters.register(self.adapter)
            if adapter is None:
                self.adapters.register(AntigravityCliAdapter())
        else:
            self.adapter = None
        self.renderer = renderer or CheckpointPromptRenderer()

    def register_agent(self, spec: AgentSpec, replace: bool = False) -> AgentSpec:
        self.adapters.get(spec.adapter_type)
        return self.store.register_agent(spec, replace=replace)

    def create_task(
        self,
        title: str,
        goal: str,
        active_agent: Optional[str] = None,
        summary: str = "Not started",
    ) -> TaskCheckpoint:
        if active_agent is not None:
            self.store.get_agent(active_agent)
        checkpoint = TaskCheckpoint.create(
            title=title,
            goal=goal,
            active_agent=active_agent,
            summary=summary,
        )
        return self.store.create_task(checkpoint)

    def add_task_notes(
        self,
        task_id: str,
        summary: Optional[str] = None,
        decisions: Optional[List[str]] = None,
        constraints: Optional[List[str]] = None,
        files_changed: Optional[List[str]] = None,
        tests: Optional[List[str]] = None,
        next_steps: Optional[List[str]] = None,
    ) -> TaskCheckpoint:
        checkpoint = self.store.get_task(task_id)
        expected_revision = checkpoint.revision
        if summary is not None:
            if not isinstance(summary, str) or not summary.strip():
                raise ValidationError("summary must be a non-empty string")
            checkpoint.state.summary = summary.strip()
        checkpoint.state.decisions.extend(decisions or [])
        checkpoint.state.constraints.extend(constraints or [])
        checkpoint.state.files_changed.extend(files_changed or [])
        checkpoint.state.tests.extend(tests or [])
        checkpoint.state.next_steps.extend(next_steps or [])
        # Re-validate the public state before persistence.
        checkpoint.state.__post_init__()
        return self.store.save_task(checkpoint, expected_revision)

    def preview_handoff(self, task_id: str, target_agent: str) -> HandoffOutcome:
        checkpoint = self.store.get_task(task_id)
        target = self.store.get_agent(target_agent)
        self.adapters.get(target.adapter_type)
        self._assert_handoff_allowed(checkpoint, target_agent)
        prompt = self.renderer.render(checkpoint, target_agent)
        return HandoffOutcome(task=checkpoint, prompt=prompt, dry_run=True)

    def handoff(
        self,
        task_id: str,
        target_agent: str,
        working_directory: Path,
    ) -> HandoffOutcome:
        checkpoint = self.store.get_task(task_id)
        target = self.store.get_agent(target_agent)
        runtime_adapter = self.adapters.get(target.adapter_type)
        self._assert_handoff_allowed(checkpoint, target_agent)

        source_agent = checkpoint.active_agent
        action = ActionRecord(
            action_id=uuid.uuid4().hex,
            kind="handoff",
            agent_id=target_agent,
            status="pending",
            started_at=utc_now(),
            details={"source_agent": source_agent},
        )
        expected_revision = checkpoint.revision
        checkpoint.actions.append(action)
        prompt = self.renderer.render(checkpoint, target_agent)
        working_directory = runtime_adapter.validate_execution(target, prompt, working_directory)
        checkpoint = self.store.save_task(checkpoint, expected_revision)

        execution = runtime_adapter.execute(target, prompt, working_directory)

        # Reload before resolving so a concurrent note cannot be silently overwritten.
        checkpoint = self.store.get_task(task_id)
        expected_revision = checkpoint.revision
        persisted_action = self._find_action(checkpoint, action.action_id)
        if persisted_action.status != "pending":
            raise ConflictError("handoff action changed while the target agent was running")
        persisted_action.finished_at = utc_now()
        persisted_action.details.update(
            {
                "elapsed_ms": execution.elapsed_ms,
                "return_code": execution.return_code,
                "timed_out": execution.timed_out,
            }
        )
        if execution.status == "completed":
            persisted_action.status = "completed"
            checkpoint.active_agent = target_agent
            checkpoint.status = "active"
        elif execution.started:
            persisted_action.status = "unknown"
            checkpoint.status = "blocked"
        else:
            persisted_action.status = "failed"
            checkpoint.status = "active"
        checkpoint = self.store.save_task(checkpoint, expected_revision)
        return HandoffOutcome(
            task=checkpoint,
            prompt=prompt,
            dry_run=False,
            action_id=action.action_id,
            execution=execution,
        )

    def resolve_action(self, task_id: str, action_id: str, resolution: str) -> TaskCheckpoint:
        if resolution not in {"completed", "failed", "cancelled"}:
            raise ValidationError("resolution must be completed, failed, or cancelled")
        checkpoint = self.store.get_task(task_id)
        expected_revision = checkpoint.revision
        action = self._find_action(checkpoint, action_id)
        if action.status not in {"pending", "unknown"}:
            raise ConflictError("only pending or unknown actions can be resolved")
        action.status = resolution
        action.finished_at = utc_now()
        action.details["resolved_manually"] = True
        if resolution == "completed" and action.kind == "handoff":
            checkpoint.active_agent = action.agent_id
        checkpoint.status = "blocked" if checkpoint.unresolved_actions() else "active"
        return self.store.save_task(checkpoint, expected_revision)

    @staticmethod
    def _assert_handoff_allowed(checkpoint: TaskCheckpoint, target_agent: str) -> None:
        if checkpoint.status == "completed":
            raise ConflictError("completed tasks cannot be handed off")
        if checkpoint.active_agent == target_agent:
            raise ConflictError("target agent is already active")
        unresolved = checkpoint.unresolved_actions()
        if unresolved:
            raise ConflictError(
                "task has an unresolved action (%s); resolve it before another handoff"
                % unresolved[0].action_id
            )

    @staticmethod
    def _find_action(checkpoint: TaskCheckpoint, action_id: str) -> ActionRecord:
        for action in checkpoint.actions:
            if action.action_id == action_id:
                return action
        raise NotFoundError("action not found: %s" % action_id)
