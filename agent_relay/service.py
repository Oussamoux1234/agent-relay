"""Application service coordinating checkpoints, adapters, and action safety."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from .adapters import (
    AdapterRegistry,
    AgentExecutionResult,
    AntigravityCliAdapter,
    CliAgentAdapter,
)
from .errors import ConflictError, NotFoundError, ValidationError
from .failures import FailureClassification, FailureClassifier
from .health import (
    AgentHealthRecord,
    CooldownPolicy,
    format_utc,
    utc_datetime_now,
)
from .models import (
    ActionRecord,
    AgentSpec,
    StructuredAgentResult,
    TaskCheckpoint,
    TaskState,
    utc_now,
)
from .prompting import CheckpointPromptRenderer
from .results import ResultExtraction, StructuredResultExtractor, result_digest
from .storage import RelayStore


@dataclass(frozen=True)
class HandoffOutcome:
    """Observable result returned by preview and execute operations."""

    task: TaskCheckpoint
    prompt: str
    dry_run: bool
    action_id: Optional[str] = None
    execution: Optional[AgentExecutionResult] = None


@dataclass(frozen=True)
class RouteAttempt:
    """One auditable execution within an ordered route."""

    agent_id: str
    action_id: str
    classification: FailureClassification
    execution: AgentExecutionResult
    result_status: str = "not-applicable"
    result: Optional[StructuredAgentResult] = None
    result_error_code: Optional[str] = None


@dataclass(frozen=True)
class RouteOutcome:
    """Preview or execution result for a task's configured route."""

    task: TaskCheckpoint
    candidates: Tuple[str, ...]
    attempts: Tuple[RouteAttempt, ...]
    prompt: str
    dry_run: bool
    skipped: Tuple[AgentHealthRecord, ...] = ()


@dataclass(frozen=True)
class RouteStatus:
    """Read-only eligibility view for a task route at one instant."""

    task: TaskCheckpoint
    candidates: Tuple[str, ...]
    skipped: Tuple[AgentHealthRecord, ...]
    observed_at: datetime


@dataclass(frozen=True)
class ResultPreview:
    """Read-only view of one pending structured checkpoint proposal."""

    task: TaskCheckpoint
    source_action_id: str
    source_agent: str
    proposal: StructuredAgentResult
    changes: Dict[str, Any]


AUTO_ROUTE_PROVIDER_IDS = frozenset(
    ("antigravity-cli", "claude-code", "codex-cli", "gemini-cli")
)


class RelayService:
    """Coordinates safe, provider-neutral handoffs between registered runtimes."""

    def __init__(
        self,
        store: RelayStore,
        adapter: Optional[CliAgentAdapter] = None,
        renderer: Optional[CheckpointPromptRenderer] = None,
        adapter_registry: Optional[AdapterRegistry] = None,
        classifier: Optional[FailureClassifier] = None,
        result_extractor: Optional[StructuredResultExtractor] = None,
        cooldown_policy: Optional[CooldownPolicy] = None,
        clock: Optional[Callable[[], datetime]] = None,
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
        self.classifier = classifier or FailureClassifier()
        self.result_extractor = result_extractor or StructuredResultExtractor()
        self.cooldown_policy = cooldown_policy or CooldownPolicy()
        self.clock = clock or utc_datetime_now

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

    def configure_route(self, task_id: str, routing_order: List[str]) -> TaskCheckpoint:
        """Persist an explicit, analysis-only fallback order for a task."""

        checkpoint = self.store.get_task(task_id)
        if checkpoint.status == "completed":
            raise ConflictError("completed tasks cannot be routed")
        if checkpoint.unresolved_actions():
            raise ConflictError("resolve the task's pending or unknown action before routing")
        if checkpoint.active_agent is None:
            raise ValidationError("task must have an active agent before routing")
        if not isinstance(routing_order, list) or len(routing_order) < 2:
            raise ValidationError("routing_order must contain at least two agents")
        if len(routing_order) > 16:
            raise ValidationError("routing_order must not contain more than 16 agents")
        if len(routing_order) != len(set(routing_order)):
            raise ValidationError("routing_order must not contain duplicate agents")
        if routing_order[0] != checkpoint.active_agent:
            raise ValidationError("routing_order must start with the task's active agent")

        for agent_id in routing_order:
            self._get_safe_routing_agent(agent_id)

        expected_revision = checkpoint.revision
        checkpoint.routing_order = list(routing_order)
        checkpoint.status = "active"
        checkpoint.__post_init__()
        return self.store.save_task(checkpoint, expected_revision)

    def preview_route(self, task_id: str) -> RouteOutcome:
        checkpoint = self.store.get_task(task_id)
        observed_at = self._now()
        candidates, skipped = self._route_plan(checkpoint, observed_at)
        self._assert_route_has_candidates(candidates)
        prompt = self.renderer.render(checkpoint, candidates[0])
        return RouteOutcome(
            task=checkpoint,
            candidates=candidates,
            attempts=(),
            prompt=prompt,
            dry_run=True,
            skipped=skipped,
        )

    def inspect_route(self, task_id: str) -> RouteStatus:
        """Show route eligibility without requiring an available candidate."""

        checkpoint = self.store.get_task(task_id)
        observed_at = self._now()
        candidates, skipped = self._route_plan(checkpoint, observed_at)
        return RouteStatus(
            task=checkpoint,
            candidates=candidates,
            skipped=skipped,
            observed_at=observed_at,
        )

    def run_route(self, task_id: str, working_directory: Path) -> RouteOutcome:
        """Run an ordered route, continuing only after a safe classified failure."""

        checkpoint = self.store.get_task(task_id)
        observed_at = self._now()
        candidates, skipped = self._route_plan(checkpoint, observed_at)
        self._assert_route_has_candidates(candidates)
        configured_order = tuple(checkpoint.routing_order)
        starting_agent = checkpoint.active_agent
        attempts: List[RouteAttempt] = []
        last_prompt = ""

        for position, agent_id in enumerate(candidates):
            checkpoint = self.store.get_task(task_id)
            if (
                tuple(checkpoint.routing_order) != configured_order
                or checkpoint.active_agent != starting_agent
            ):
                raise ConflictError("task route changed while fallback execution was in progress")
            if checkpoint.unresolved_actions():
                raise ConflictError("task gained an unresolved action during fallback execution")
            target = self._get_safe_routing_agent(agent_id)
            runtime_adapter = self.adapters.get(target.adapter_type)
            action = ActionRecord(
                action_id=uuid.uuid4().hex,
                kind="route-run",
                agent_id=agent_id,
                status="pending",
                started_at=utc_now(),
                details={
                    "source_agent": checkpoint.active_agent,
                    "route_position": checkpoint.routing_order.index(agent_id),
                },
            )
            expected_revision = checkpoint.revision
            checkpoint.actions.append(action)
            last_prompt = self.renderer.render(checkpoint, agent_id)
            resolved_directory = runtime_adapter.validate_execution(
                target,
                last_prompt,
                working_directory,
            )
            self.store.save_task(checkpoint, expected_revision)

            execution = runtime_adapter.execute(target, last_prompt, resolved_directory)
            classification = self.classifier.classify(target, execution)
            health_observed_at = self._now()
            health_record = self.cooldown_policy.create_record(
                target,
                classification,
                execution,
                task_id,
                action.action_id,
                health_observed_at,
            )
            extraction = ResultExtraction("not-applicable")
            if execution.status == "completed":
                extraction = self.result_extractor.extract(
                    execution.stdout,
                    task_id,
                    action.action_id,
                )

            checkpoint = self.store.get_task(task_id)
            expected_revision = checkpoint.revision
            persisted_action = self._find_action(checkpoint, action.action_id)
            if persisted_action.status != "pending":
                raise ConflictError("route action changed while the agent was running")
            persisted_action.finished_at = utc_now()
            persisted_action.details.update(
                {
                    "classification": classification.category,
                    "evidence_code": classification.evidence_code,
                    "elapsed_ms": execution.elapsed_ms,
                    "return_code": execution.return_code,
                    "timed_out": execution.timed_out,
                }
            )
            if health_record is not None:
                persisted_action.details.update(
                    {
                        "cooldown_until": health_record.cooldown_until,
                        "cooldown_retry_source": health_record.retry_source,
                        "cooldown_retry_signal_code": health_record.retry_signal_code,
                    }
                )
            if execution.status == "completed":
                if extraction.status == "ready" and extraction.result is not None:
                    persisted_action.details.update(
                        {
                            "result_status": "pending",
                            "result_digest": result_digest(extraction.result),
                            "result_proposal": extraction.result.to_dict(),
                        }
                    )
                else:
                    persisted_action.details.update(
                        {
                            "result_status": extraction.status,
                            "result_error_code": extraction.error_code,
                        }
                    )

            has_next = position + 1 < len(candidates)
            if execution.status == "completed":
                persisted_action.status = "completed"
                checkpoint.active_agent = agent_id
                checkpoint.status = "active"
            elif classification.safe_to_fallback:
                persisted_action.status = "failed"
                checkpoint.status = "active" if has_next else "blocked"
            else:
                persisted_action.status = "unknown"
                checkpoint.status = "blocked"

            if execution.status == "completed":
                self.store.clear_agent_health(agent_id)
            elif health_record is not None:
                self.store.set_agent_health(health_record)

            checkpoint = self.store.save_task(checkpoint, expected_revision)
            attempts.append(
                RouteAttempt(
                    agent_id=agent_id,
                    action_id=action.action_id,
                    classification=classification,
                    execution=execution,
                    result_status=(
                        "pending" if extraction.status == "ready" else extraction.status
                    ),
                    result=extraction.result,
                    result_error_code=extraction.error_code,
                )
            )
            if execution.status == "completed" or not classification.safe_to_fallback or not has_next:
                return RouteOutcome(
                    task=checkpoint,
                    candidates=candidates,
                    attempts=tuple(attempts),
                    prompt=last_prompt,
                    dry_run=False,
                    skipped=skipped,
                )

        raise AssertionError("configured route contained no candidates")

    def list_agent_health(self) -> Tuple[AgentHealthRecord, ...]:
        """Return every persisted cooldown record in stable agent-id order."""

        return tuple(self.store.list_agent_health())

    def get_agent_health(self, agent_id: str) -> AgentHealthRecord:
        """Return one cooldown record or a typed not-found error."""

        self.store.get_agent(agent_id)
        record = self.store.get_agent_health(agent_id)
        if record is None:
            raise NotFoundError("agent has no health record: %s" % agent_id)
        return record

    def clear_agent_health(self, agent_id: str) -> bool:
        """Explicitly clear one provider-instance cooldown."""

        self.store.get_agent(agent_id)
        return self.store.clear_agent_health(agent_id)

    def recover_route(self, task_id: str, target_agent: str) -> TaskCheckpoint:
        """Explicitly move a task back to an earlier, currently eligible route entry."""

        checkpoint = self.store.get_task(task_id)
        observed_at = self._now()
        self._route_plan(checkpoint, observed_at)
        if target_agent not in checkpoint.routing_order:
            raise ValidationError("recovery target is not present in the task route")
        if checkpoint.active_agent is None:
            raise ConflictError("task has no active agent to recover from")
        current_position = checkpoint.routing_order.index(checkpoint.active_agent)
        target_position = checkpoint.routing_order.index(target_agent)
        if target_position >= current_position:
            raise ConflictError("route recovery target must be earlier than the active agent")
        self._get_safe_routing_agent(target_agent)
        health = self.store.get_agent_health(target_agent)
        if health is not None and health.is_active(observed_at):
            raise ConflictError(
                "recovery target is still cooling down; wait for expiry or clear its health"
            )

        expected_revision = checkpoint.revision
        source_agent = checkpoint.active_agent
        timestamp = format_utc(observed_at)
        checkpoint.active_agent = target_agent
        checkpoint.status = "active"
        checkpoint.actions.append(
            ActionRecord(
                action_id=uuid.uuid4().hex,
                kind="route-recover",
                agent_id=target_agent,
                status="completed",
                started_at=timestamp,
                finished_at=timestamp,
                details={
                    "source_agent": source_agent,
                    "recovery": "explicit-user-command",
                },
            )
        )
        checkpoint.__post_init__()
        return self.store.save_task(checkpoint, expected_revision)

    def preview_result(self, task_id: str, source_action_id: str) -> ResultPreview:
        checkpoint = self.store.get_task(task_id)
        source_action, proposal = self._pending_result(checkpoint, source_action_id)
        return self._build_result_preview(checkpoint, source_action, proposal)

    def accept_result(
        self,
        task_id: str,
        source_action_id: str,
        expected_revision: int,
    ) -> TaskCheckpoint:
        """Apply a previewed result if the checkpoint has not changed since preview."""

        checkpoint = self.store.get_task(task_id)
        if (
            isinstance(expected_revision, bool)
            or not isinstance(expected_revision, int)
            or expected_revision < 1
        ):
            raise ValidationError("expected_revision must be a positive integer")
        if checkpoint.revision != expected_revision:
            raise ConflictError(
                "checkpoint changed after result preview: expected revision %d, found %d"
                % (expected_revision, checkpoint.revision)
            )
        if checkpoint.status == "completed":
            raise ConflictError("completed tasks cannot accept result proposals")
        unresolved = checkpoint.unresolved_actions()
        if unresolved:
            raise ConflictError(
                "task has an unresolved action (%s); resolve it before accepting a result"
                % unresolved[0].action_id
            )

        source_action, proposal = self._pending_result(checkpoint, source_action_id)
        source_index = checkpoint.actions.index(source_action)
        later_executions = [
            action
            for action in checkpoint.actions[source_index + 1 :]
            if action.kind in {"handoff", "route-run"}
        ]
        if later_executions:
            raise ConflictError("result proposal is stale because a later agent action exists")

        preview = self._build_result_preview(checkpoint, source_action, proposal)
        checkpoint.state.summary = proposal.summary
        additions = preview.changes["additions"]
        for field_name in (
            "decisions",
            "constraints",
            "files_changed",
            "tests",
            "next_steps",
        ):
            getattr(checkpoint.state, field_name).extend(additions[field_name])
        checkpoint.state.__post_init__()

        accepted_at = utc_now()
        acceptance_action_id = uuid.uuid4().hex
        digest = result_digest(proposal)
        source_action.details.pop("result_proposal", None)
        source_action.details.update(
            {
                "result_status": "accepted",
                "result_digest": digest,
                "result_accepted_action_id": acceptance_action_id,
            }
        )
        checkpoint.actions.append(
            ActionRecord(
                action_id=acceptance_action_id,
                kind="result-accept",
                agent_id=source_action.agent_id,
                status="completed",
                started_at=accepted_at,
                finished_at=accepted_at,
                details={
                    "source_action_id": source_action.action_id,
                    "source_agent": source_action.agent_id,
                    "proposal_digest": digest,
                    "summary_changed": preview.changes["summary_changed"],
                    "applied_counts": {
                        field_name: len(values) for field_name, values in additions.items()
                    },
                },
            )
        )
        checkpoint.__post_init__()
        return self.store.save_task(checkpoint, expected_revision)

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
        if resolution == "completed" and action.kind in {"handoff", "route-run"}:
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

    def _route_plan(
        self,
        checkpoint: TaskCheckpoint,
        observed_at: datetime,
    ) -> Tuple[Tuple[str, ...], Tuple[AgentHealthRecord, ...]]:
        if checkpoint.status == "completed":
            raise ConflictError("completed tasks cannot be routed")
        unresolved = checkpoint.unresolved_actions()
        if unresolved:
            raise ConflictError(
                "task has an unresolved action (%s); resolve it before routing"
                % unresolved[0].action_id
            )
        if not checkpoint.routing_order:
            raise ValidationError("task has no route; configure one with route set")
        if checkpoint.active_agent not in checkpoint.routing_order:
            raise ConflictError("task's active agent is not present in its routing_order")
        active_position = checkpoint.routing_order.index(checkpoint.active_agent)
        remaining = tuple(checkpoint.routing_order[active_position:])
        candidates = []
        skipped = []
        for agent_id in remaining:
            self._get_safe_routing_agent(agent_id)
            health = self.store.get_agent_health(agent_id)
            if health is not None and health.is_active(observed_at):
                skipped.append(health)
            else:
                candidates.append(agent_id)
        return tuple(candidates), tuple(skipped)

    @staticmethod
    def _assert_route_has_candidates(candidates: Tuple[str, ...]) -> None:
        if not candidates:
            raise ConflictError(
                "all remaining route agents are cooling down; inspect with health list or "
                "recover with health clear and route recover"
            )

    def _now(self) -> datetime:
        observed_at = self.clock()
        # format_utc provides a single validation path for naive or invalid clocks.
        format_utc(observed_at)
        return observed_at

    def _get_safe_routing_agent(self, agent_id: str) -> AgentSpec:
        spec = self.store.get_agent(agent_id)
        self.adapters.get(spec.adapter_type)
        if spec.provider_id not in AUTO_ROUTE_PROVIDER_IDS:
            raise ValidationError(
                "automatic routing requires a supported built-in preset: %s" % agent_id
            )
        if spec.capabilities != ("repo-read",):
            raise ValidationError(
                "automatic routing is limited to repo-read agent presets: %s" % agent_id
            )
        return spec

    @staticmethod
    def _pending_result(
        checkpoint: TaskCheckpoint,
        source_action_id: str,
    ) -> Tuple[ActionRecord, StructuredAgentResult]:
        source_action = RelayService._find_action(checkpoint, source_action_id)
        if source_action.kind != "route-run" or source_action.status != "completed":
            raise ConflictError("result proposals require a completed route-run action")
        result_status = source_action.details.get("result_status")
        if result_status != "pending":
            raise ConflictError("action has no pending result proposal")
        proposal_value = source_action.details.get("result_proposal")
        if not isinstance(proposal_value, dict):
            raise ValidationError("pending result proposal is missing or invalid")
        proposal = StructuredAgentResult.from_dict(proposal_value)
        if (
            proposal.task_id != checkpoint.task_id
            or proposal.source_action_id != source_action.action_id
        ):
            raise ValidationError("pending result proposal does not match its source action")
        return source_action, proposal

    @staticmethod
    def _build_result_preview(
        checkpoint: TaskCheckpoint,
        source_action: ActionRecord,
        proposal: StructuredAgentResult,
    ) -> ResultPreview:
        additions = {}
        for field_name in (
            "decisions",
            "constraints",
            "files_changed",
            "tests",
            "next_steps",
        ):
            seen = set(getattr(checkpoint.state, field_name))
            field_additions = []
            for value in getattr(proposal, field_name):
                if value not in seen:
                    field_additions.append(value)
                    seen.add(value)
            additions[field_name] = field_additions
        changes = {
            "summary_changed": checkpoint.state.summary != proposal.summary,
            "summary_before": checkpoint.state.summary,
            "summary_after": proposal.summary,
            "additions": additions,
        }
        TaskState(
            summary=proposal.summary,
            decisions=checkpoint.state.decisions + additions["decisions"],
            constraints=checkpoint.state.constraints + additions["constraints"],
            files_changed=checkpoint.state.files_changed + additions["files_changed"],
            tests=checkpoint.state.tests + additions["tests"],
            next_steps=checkpoint.state.next_steps + additions["next_steps"],
        )
        return ResultPreview(
            task=checkpoint,
            source_action_id=source_action.action_id,
            source_agent=source_action.agent_id,
            proposal=proposal,
            changes=changes,
        )

    @staticmethod
    def _find_action(checkpoint: TaskCheckpoint, action_id: str) -> ActionRecord:
        for action in checkpoint.actions:
            if action.action_id == action_id:
                return action
        raise NotFoundError("action not found: %s" % action_id)
