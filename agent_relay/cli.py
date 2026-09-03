"""Command-line interface for registering agents and moving checkpoints."""

from __future__ import annotations

import argparse
import json
import os
import posixpath
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Sequence

from .adapters import AgentExecutionResult
from .errors import RelayError, ValidationError
from .health import AgentHealthRecord, utc_datetime_now
from .models import AgentSpec
from .presets import PRESETS, build_preset, list_preset_statuses
from .service import (
    HandoffOutcome,
    RelayService,
    ResultPreview,
    RouteOutcome,
    WorkspaceReviewOutcome,
)
from .storage import RelayStore


def _default_state_dir() -> str:
    configured = os.environ.get("AGENT_RELAY_STATE_DIR")
    if configured:
        return configured
    if sys.platform == "darwin":
        return str(Path.home() / "Library" / "Application Support" / "agent-relay")
    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        base = Path(local_app_data) if local_app_data else Path.home() / "AppData" / "Local"
        return str(base / "agent-relay")
    configured_base = os.environ.get("XDG_STATE_HOME")
    base = Path(configured_base).expanduser() if configured_base else Path.home() / ".local" / "state"
    # XDG paths always use POSIX path semantics.  Keeping that validation
    # independent from the host path flavour also makes platform simulation
    # reliable in the cross-platform test matrix.
    if configured_base and not posixpath.isabs(configured_base):
        base = Path.home() / ".local" / "state"
    return str(base / "agent-relay")


def _print_json(value: Any, stream: Any = None) -> None:
    if stream is None:
        stream = sys.stdout
    json.dump(value, stream, indent=2, sort_keys=True, ensure_ascii=False)
    stream.write("\n")


def _service(state_dir: str) -> RelayService:
    return RelayService(RelayStore(Path(state_dir)))


def _agent_to_public_dict(spec: AgentSpec) -> Dict[str, Any]:
    value = spec.to_dict()
    # Credential values are never persisted. An optional provider config-directory path is public.
    return value


def _handoff_to_dict(outcome: HandoffOutcome, include_prompt: bool) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "dry_run": outcome.dry_run,
        "task_id": outcome.task.task_id,
        "task_status": outcome.task.status,
        "active_agent": outcome.task.active_agent,
        "revision": outcome.task.revision,
        "action_id": outcome.action_id,
    }
    if include_prompt:
        value["prompt"] = outcome.prompt
    if outcome.execution is not None:
        value["execution"] = _execution_to_dict(outcome.execution)
        value["result_status"] = outcome.result_status
        value["result_error_code"] = outcome.result_error_code
        value["result_proposal"] = (
            outcome.result.to_dict() if outcome.result is not None else None
        )
        value["workspace_review"] = (
            outcome.workspace_review.to_dict()
            if outcome.workspace_review is not None
            else None
        )
    return value


def _execution_to_dict(execution: AgentExecutionResult) -> Dict[str, Any]:
    return {
        "status": execution.status,
        "return_code": execution.return_code,
        "elapsed_ms": execution.elapsed_ms,
        "started": execution.started,
        "timed_out": execution.timed_out,
        "error": execution.error,
        "stdout": execution.stdout,
        "stderr": execution.stderr,
        "session_id": execution.session_id,
        "turn_id": execution.turn_id,
        "protocol_status": execution.protocol_status,
        "event_types": list(execution.event_types),
    }


def _route_to_dict(outcome: RouteOutcome, include_prompt: bool) -> Dict[str, Any]:
    value: Dict[str, Any] = {
        "dry_run": outcome.dry_run,
        "task_id": outcome.task.task_id,
        "task_status": outcome.task.status,
        "active_agent": outcome.task.active_agent,
        "revision": outcome.task.revision,
        "routing_order": list(outcome.task.routing_order),
        "candidates": list(outcome.candidates),
        "skipped": [_health_to_dict(record) for record in outcome.skipped],
        "attempts": [
            {
                "agent_id": attempt.agent_id,
                "action_id": attempt.action_id,
                "classification": attempt.classification.category,
                "evidence_code": attempt.classification.evidence_code,
                "safe_to_fallback": attempt.classification.safe_to_fallback,
                "execution": _execution_to_dict(attempt.execution),
                "result_status": attempt.result_status,
                "result_error_code": attempt.result_error_code,
                "result_proposal": (
                    attempt.result.to_dict() if attempt.result is not None else None
                ),
            }
            for attempt in outcome.attempts
        ],
    }
    if include_prompt:
        value["prompt"] = outcome.prompt
    return value


def _health_to_dict(record: AgentHealthRecord, observed_at: Any = None) -> Dict[str, Any]:
    return record.to_status_dict(observed_at or utc_datetime_now())


def _result_preview_to_dict(preview: ResultPreview) -> Dict[str, Any]:
    return {
        "task_id": preview.task.task_id,
        "checkpoint_revision": preview.task.revision,
        "source_action_id": preview.source_action_id,
        "source_agent": preview.source_agent,
        "proposal": preview.proposal.to_dict(),
        "changes": preview.changes,
    }


def _workspace_review_to_dict(outcome: WorkspaceReviewOutcome) -> Dict[str, Any]:
    return {
        "task_id": outcome.task.task_id,
        "task_status": outcome.task.status,
        "checkpoint_revision": outcome.task.revision,
        "source_action_id": outcome.source_action_id,
        "source_agent": outcome.source_agent,
        "workspace_review": outcome.review.to_dict(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="agent-relay",
        description="Carry explicit task checkpoints between user-owned AI agents.",
    )
    parser.add_argument(
        "--state-dir",
        default=_default_state_dir(),
        help="local state directory (default: %(default)s)",
    )
    commands = parser.add_subparsers(dest="command_name", required=True)

    agent = commands.add_parser("agent", help="manage agent adapters")
    agent_commands = agent.add_subparsers(dest="agent_command", required=True)
    agent_add = agent_commands.add_parser("add", help="register a CLI agent")
    agent_add.add_argument("agent_id")
    agent_add.add_argument("--name", required=True)
    agent_add.add_argument("--transport", choices=("stdin", "argument"), default="stdin")
    agent_add.add_argument("--timeout", type=int, default=900)
    agent_add.add_argument("--capability", action="append", default=[])
    agent_add.add_argument("--allow-env", action="append", default=[])
    agent_add.add_argument("--replace", action="store_true")
    agent_add.add_argument("--command", required=True, help="agent executable")
    agent_add.add_argument(
        "--arg",
        action="append",
        default=[],
        help="one fixed argv item; repeat as needed and use --arg=-x for values starting with -",
    )
    agent_commands.add_parser("list", help="list registered agents")
    agent_commands.add_parser("presets", help="show built-in presets and local availability")
    agent_add_preset = agent_commands.add_parser(
        "add-preset",
        help="register a reviewed built-in agent preset",
    )
    agent_add_preset.add_argument("preset_id", choices=tuple(sorted(PRESETS)))
    agent_add_preset.add_argument("--id", dest="agent_id")
    agent_add_preset.add_argument("--executable")
    agent_add_preset.add_argument(
        "--config-home",
        help="absolute provider config directory for an isolated Codex or Claude instance",
    )
    agent_add_preset.add_argument("--timeout", type=int, default=900)
    agent_add_preset.add_argument("--replace", action="store_true")

    task = commands.add_parser("task", help="manage portable task checkpoints")
    task_commands = task.add_subparsers(dest="task_command", required=True)
    task_create = task_commands.add_parser("create", help="create a checkpoint")
    task_create.add_argument("--title", required=True)
    task_create.add_argument("--goal", required=True)
    task_create.add_argument("--agent")
    task_create.add_argument("--summary", default="Not started")
    task_show = task_commands.add_parser("show", help="show one checkpoint")
    task_show.add_argument("task_id")
    task_commands.add_parser("list", help="list checkpoints")
    task_note = task_commands.add_parser("note", help="append verified checkpoint facts")
    task_note.add_argument("task_id")
    task_note.add_argument("--summary")
    task_note.add_argument("--decision", action="append", default=[])
    task_note.add_argument("--constraint", action="append", default=[])
    task_note.add_argument("--file", action="append", default=[])
    task_note.add_argument("--test", action="append", default=[])
    task_note.add_argument("--next", action="append", default=[])

    handoff = commands.add_parser("handoff", help="preview or execute a task handoff")
    handoff.add_argument("task_id")
    handoff.add_argument("target_agent")
    handoff.add_argument("--execute", action="store_true")
    handoff.add_argument("--cwd", default=".")
    handoff.add_argument(
        "--thread-id",
        dest="session_id",
        help="Codex thread to resume; supported only by the Codex App Server adapter",
    )
    handoff.add_argument(
        "--show-prompt",
        action="store_true",
        help="include the full checkpoint prompt in executed handoff output",
    )

    route = commands.add_parser("route", help="manage safe ordered fallback routing")
    route_commands = route.add_subparsers(dest="route_command", required=True)
    route_set = route_commands.add_parser("set", help="set the ordered agents for a task")
    route_set.add_argument("task_id")
    route_set.add_argument(
        "--agent",
        action="append",
        required=True,
        help="agent id in priority order; repeat at least twice",
    )
    route_show = route_commands.add_parser("show", help="show a task's route")
    route_show.add_argument("task_id")
    route_run = route_commands.add_parser("run", help="preview or execute a task's route")
    route_run.add_argument("task_id")
    route_run.add_argument("--execute", action="store_true")
    route_run.add_argument("--cwd", default=".")
    route_run.add_argument(
        "--show-prompt",
        action="store_true",
        help="include the last checkpoint prompt in executed route output",
    )
    route_recover = route_commands.add_parser(
        "recover",
        help="explicitly move a task back to an earlier eligible route entry",
    )
    route_recover.add_argument("task_id")
    route_recover.add_argument("target_agent")

    result = commands.add_parser("result", help="preview or accept structured agent results")
    result_commands = result.add_subparsers(dest="result_command", required=True)
    result_preview = result_commands.add_parser(
        "preview",
        help="preview one pending result proposal without changing the checkpoint",
    )
    result_preview.add_argument("task_id")
    result_preview.add_argument("source_action_id")
    result_accept = result_commands.add_parser(
        "accept",
        help="accept a previewed result if the checkpoint revision still matches",
    )
    result_accept.add_argument("task_id")
    result_accept.add_argument("source_action_id")
    result_accept.add_argument("--expected-revision", type=int, required=True)

    workspace = commands.add_parser(
        "workspace",
        help="authorize and review bounded workspace-write actions",
    )
    workspace_commands = workspace.add_subparsers(
        dest="workspace_command",
        required=True,
    )
    workspace_authorize = workspace_commands.add_parser(
        "authorize",
        help="authorize one reviewed write agent for one task and exact Git root",
    )
    workspace_authorize.add_argument("task_id")
    workspace_authorize.add_argument("agent_id")
    workspace_authorize.add_argument("--root", required=True)
    workspace_revoke = workspace_commands.add_parser(
        "revoke",
        help="revoke one task/agent workspace-write authorization",
    )
    workspace_revoke.add_argument("task_id")
    workspace_revoke.add_argument("agent_id")
    workspace_review = workspace_commands.add_parser(
        "review",
        help="show the content-free change summary and rollback guidance",
    )
    workspace_review.add_argument("task_id")
    workspace_review.add_argument("source_action_id")
    workspace_accept = workspace_commands.add_parser(
        "accept",
        help="accept a reviewed write snapshot if it and the checkpoint are unchanged",
    )
    workspace_accept.add_argument("task_id")
    workspace_accept.add_argument("source_action_id")
    workspace_accept.add_argument("--expected-revision", type=int, required=True)
    workspace_accept.add_argument("--cwd", default=".")
    workspace_rollback = workspace_commands.add_parser(
        "verify-rollback",
        help="verify the pre-run snapshot was restored and unblock the task",
    )
    workspace_rollback.add_argument("task_id")
    workspace_rollback.add_argument("source_action_id")
    workspace_rollback.add_argument("--expected-revision", type=int, required=True)
    workspace_rollback.add_argument("--cwd", default=".")

    health = commands.add_parser("health", help="inspect or clear provider cooldowns")
    health_commands = health.add_subparsers(dest="health_command", required=True)
    health_commands.add_parser("list", help="list persisted agent health records")
    health_show = health_commands.add_parser("show", help="show one agent health record")
    health_show.add_argument("agent_id")
    health_clear = health_commands.add_parser(
        "clear",
        help="explicitly clear one agent cooldown",
    )
    health_clear.add_argument("agent_id")

    resolve = commands.add_parser("resolve", help="resolve an unknown handoff outcome")
    resolve.add_argument("task_id")
    resolve.add_argument("action_id")
    resolve.add_argument("--as", dest="resolution", choices=("completed", "failed", "cancelled"), required=True)
    return parser


def run(args: argparse.Namespace) -> Dict[str, Any]:
    service = _service(args.state_dir)
    if args.command_name == "agent" and args.agent_command == "add":
        spec = AgentSpec(
            agent_id=args.agent_id,
            display_name=args.name,
            command=tuple([args.command] + args.arg),
            prompt_transport=args.transport,
            timeout_seconds=args.timeout,
            capabilities=tuple(args.capability),
            env_allowlist=tuple(args.allow_env),
        )
        return {"agent": _agent_to_public_dict(service.register_agent(spec, replace=args.replace))}
    if args.command_name == "agent" and args.agent_command == "list":
        return {"agents": [_agent_to_public_dict(spec) for spec in service.store.list_agents()]}
    if args.command_name == "agent" and args.agent_command == "presets":
        return {"presets": list(list_preset_statuses())}
    if args.command_name == "agent" and args.agent_command == "add-preset":
        spec = build_preset(
            preset_id=args.preset_id,
            agent_id=args.agent_id,
            executable=args.executable,
            timeout_seconds=args.timeout,
            config_home=args.config_home,
        )
        return {"agent": _agent_to_public_dict(service.register_agent(spec, replace=args.replace))}
    if args.command_name == "task" and args.task_command == "create":
        checkpoint = service.create_task(
            title=args.title,
            goal=args.goal,
            active_agent=args.agent,
            summary=args.summary,
        )
        return {"task": checkpoint.to_dict()}
    if args.command_name == "task" and args.task_command == "show":
        return {"task": service.store.get_task(args.task_id).to_dict()}
    if args.command_name == "task" and args.task_command == "list":
        return {"tasks": [checkpoint.to_dict() for checkpoint in service.store.list_tasks()]}
    if args.command_name == "task" and args.task_command == "note":
        checkpoint = service.add_task_notes(
            task_id=args.task_id,
            summary=args.summary,
            decisions=args.decision,
            constraints=args.constraint,
            files_changed=args.file,
            tests=args.test,
            next_steps=args.next,
        )
        return {"task": checkpoint.to_dict()}
    if args.command_name == "handoff":
        if args.session_id is not None and not args.execute:
            raise ValidationError("--thread-id requires --execute")
        if args.execute:
            outcome = service.handoff(
                task_id=args.task_id,
                target_agent=args.target_agent,
                working_directory=Path(args.cwd),
                session_id=args.session_id,
            )
            return _handoff_to_dict(outcome, include_prompt=args.show_prompt)
        outcome = service.preview_handoff(args.task_id, args.target_agent)
        return _handoff_to_dict(outcome, include_prompt=True)
    if args.command_name == "route" and args.route_command == "set":
        checkpoint = service.configure_route(args.task_id, args.agent)
        return {
            "task_id": checkpoint.task_id,
            "active_agent": checkpoint.active_agent,
            "revision": checkpoint.revision,
            "routing_order": list(checkpoint.routing_order),
        }
    if args.command_name == "route" and args.route_command == "show":
        status = service.inspect_route(args.task_id)
        checkpoint = status.task
        return {
            "task_id": checkpoint.task_id,
            "active_agent": checkpoint.active_agent,
            "task_status": checkpoint.status,
            "revision": checkpoint.revision,
            "routing_order": list(checkpoint.routing_order),
            "candidates": list(status.candidates),
            "skipped": [
                _health_to_dict(record, status.observed_at) for record in status.skipped
            ],
        }
    if args.command_name == "route" and args.route_command == "run":
        if args.execute:
            outcome = service.run_route(args.task_id, Path(args.cwd))
            return _route_to_dict(outcome, include_prompt=args.show_prompt)
        outcome = service.preview_route(args.task_id)
        return _route_to_dict(outcome, include_prompt=True)
    if args.command_name == "route" and args.route_command == "recover":
        checkpoint = service.recover_route(args.task_id, args.target_agent)
        return {
            "task_id": checkpoint.task_id,
            "active_agent": checkpoint.active_agent,
            "task_status": checkpoint.status,
            "revision": checkpoint.revision,
            "routing_order": list(checkpoint.routing_order),
            "recovery_action_id": checkpoint.actions[-1].action_id,
        }
    if args.command_name == "result" and args.result_command == "preview":
        preview = service.preview_result(args.task_id, args.source_action_id)
        return _result_preview_to_dict(preview)
    if args.command_name == "result" and args.result_command == "accept":
        checkpoint = service.accept_result(
            args.task_id,
            args.source_action_id,
            args.expected_revision,
        )
        return {"task": checkpoint.to_dict()}
    if args.command_name == "workspace" and args.workspace_command == "authorize":
        checkpoint = service.authorize_workspace(
            args.task_id,
            args.agent_id,
            Path(args.root),
        )
        return {
            "task_id": checkpoint.task_id,
            "task_status": checkpoint.status,
            "revision": checkpoint.revision,
            "authorization_action": checkpoint.actions[-1].to_dict(),
        }
    if args.command_name == "workspace" and args.workspace_command == "revoke":
        checkpoint = service.revoke_workspace(args.task_id, args.agent_id)
        return {
            "task_id": checkpoint.task_id,
            "task_status": checkpoint.status,
            "revision": checkpoint.revision,
            "revocation_action": checkpoint.actions[-1].to_dict(),
        }
    if args.command_name == "workspace" and args.workspace_command == "review":
        return _workspace_review_to_dict(
            service.inspect_workspace_review(args.task_id, args.source_action_id)
        )
    if args.command_name == "workspace" and args.workspace_command == "accept":
        checkpoint = service.accept_workspace_review(
            args.task_id,
            args.source_action_id,
            args.expected_revision,
            Path(args.cwd),
        )
        return {"task": checkpoint.to_dict()}
    if args.command_name == "workspace" and args.workspace_command == "verify-rollback":
        checkpoint = service.verify_workspace_rollback(
            args.task_id,
            args.source_action_id,
            args.expected_revision,
            Path(args.cwd),
        )
        return {"task": checkpoint.to_dict()}
    if args.command_name == "health" and args.health_command == "list":
        return {
            "health": [_health_to_dict(record) for record in service.list_agent_health()]
        }
    if args.command_name == "health" and args.health_command == "show":
        return {"health": _health_to_dict(service.get_agent_health(args.agent_id))}
    if args.command_name == "health" and args.health_command == "clear":
        return {
            "agent_id": args.agent_id,
            "cleared": service.clear_agent_health(args.agent_id),
        }
    if args.command_name == "resolve":
        checkpoint = service.resolve_action(args.task_id, args.action_id, args.resolution)
        return {"task": checkpoint.to_dict()}
    raise AssertionError("unhandled command")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _print_json(run(args))
        return 0
    except RelayError as exc:
        _print_json({"error": str(exc), "error_type": type(exc).__name__}, stream=sys.stderr)
        return 2
    except KeyboardInterrupt:
        _print_json({"error": "interrupted", "error_type": "KeyboardInterrupt"}, stream=sys.stderr)
        return 130


def entrypoint() -> None:
    raise SystemExit(main())
