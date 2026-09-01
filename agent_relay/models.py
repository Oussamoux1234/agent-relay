"""Versioned data contracts for adapters, checkpoints, and action records."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import ValidationError


SCHEMA_VERSION = "1.0"
AGENT_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ADAPTER_TYPE_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
ACTION_STATUSES = {"pending", "completed", "failed", "unknown", "cancelled"}
TASK_STATUSES = {"active", "blocked", "completed"}
PROMPT_TRANSPORTS = {"stdin", "argument"}
CONFIG_HOME_ENVIRONMENTS = {"CLAUDE_CONFIG_DIR", "CODEX_HOME"}


def utc_now() -> str:
    """Return a stable, timezone-qualified UTC timestamp."""

    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _required_text(value: Any, field_name: str, max_length: int = 100_000) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError("%s must be a non-empty string" % field_name)
    if "\x00" in value:
        raise ValidationError("%s must not contain a null byte" % field_name)
    if len(value) > max_length:
        raise ValidationError("%s exceeds the maximum length of %d" % (field_name, max_length))
    return value.strip()


def _string_list(values: Iterable[Any], field_name: str, max_items: int = 1_000) -> List[str]:
    if not isinstance(values, (list, tuple)):
        raise ValidationError("%s must be a list of strings" % field_name)
    result = [_required_text(value, field_name, max_length=20_000) for value in values]
    if len(result) > max_items:
        raise ValidationError("%s exceeds the maximum item count of %d" % (field_name, max_items))
    return result


@dataclass(frozen=True)
class AgentSpec:
    """A user-owned CLI agent registered through a shell-free argv contract."""

    agent_id: str
    display_name: str
    command: Tuple[str, ...]
    prompt_transport: str = "stdin"
    timeout_seconds: int = 900
    capabilities: Tuple[str, ...] = ()
    env_allowlist: Tuple[str, ...] = ()
    adapter_type: str = "cli"
    config_home: Optional[Tuple[str, str]] = None
    provider_id: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or AGENT_ID_PATTERN.fullmatch(self.agent_id) is None:
            raise ValidationError(
                "agent_id must start with a lowercase letter and contain only lowercase letters, "
                "digits, underscores, or hyphens"
            )
        _required_text(self.display_name, "display_name", max_length=120)
        if not isinstance(self.command, tuple) or not self.command:
            raise ValidationError("command must contain at least one argv item")
        if len(self.command) > 64:
            raise ValidationError("command must not contain more than 64 argv items")
        for item in self.command:
            _required_text(item, "command item", max_length=4_096)
        if (
            not isinstance(self.adapter_type, str)
            or ADAPTER_TYPE_PATTERN.fullmatch(self.adapter_type) is None
        ):
            raise ValidationError("adapter_type must be a lowercase identifier")
        if self.prompt_transport not in PROMPT_TRANSPORTS:
            raise ValidationError("prompt_transport must be stdin or argument")
        if isinstance(self.timeout_seconds, bool) or not isinstance(self.timeout_seconds, int):
            raise ValidationError("timeout_seconds must be an integer")
        if not 1 <= self.timeout_seconds <= 3_600:
            raise ValidationError("timeout_seconds must be between 1 and 3600")
        _string_list(self.capabilities, "capability", max_items=128)
        for name in self.env_allowlist:
            if not isinstance(name, str) or ENV_NAME_PATTERN.fullmatch(name) is None:
                raise ValidationError("env_allowlist contains an invalid environment variable name")
        if self.config_home is not None:
            if not isinstance(self.config_home, tuple) or len(self.config_home) != 2:
                raise ValidationError("config_home must contain an environment name and path")
            environment_name, directory = self.config_home
            if (
                not isinstance(environment_name, str)
                or environment_name not in CONFIG_HOME_ENVIRONMENTS
            ):
                raise ValidationError("config_home environment is not supported")
            normalized_directory = _required_text(
                directory,
                "config_home path",
                max_length=4_096,
            )
            if directory != normalized_directory:
                raise ValidationError("config_home path must not have surrounding whitespace")
            if not Path(directory).is_absolute():
                raise ValidationError("config_home path must be absolute")
        if self.provider_id is not None:
            if (
                not isinstance(self.provider_id, str)
                or AGENT_ID_PATTERN.fullmatch(self.provider_id) is None
            ):
                raise ValidationError("provider_id must be a lowercase identifier")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "command": list(self.command),
            "adapter_type": self.adapter_type,
            "prompt_transport": self.prompt_transport,
            "timeout_seconds": self.timeout_seconds,
            "capabilities": list(self.capabilities),
            "env_allowlist": list(self.env_allowlist),
            "config_home": (
                {
                    "environment": self.config_home[0],
                    "path": self.config_home[1],
                }
                if self.config_home is not None
                else None
            ),
            "provider_id": self.provider_id,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "AgentSpec":
        if not isinstance(value, dict):
            raise ValidationError("agent specification must be an object")
        command = value.get("command", [])
        capabilities = value.get("capabilities", [])
        env_allowlist = value.get("env_allowlist", [])
        config_home_value = value.get("config_home")
        if not isinstance(command, list):
            raise ValidationError("agent command must be a list")
        if not isinstance(capabilities, list):
            raise ValidationError("agent capabilities must be a list")
        if not isinstance(env_allowlist, list):
            raise ValidationError("agent env_allowlist must be a list")
        if config_home_value is None:
            config_home = None
        elif isinstance(config_home_value, dict):
            config_home = (
                config_home_value.get("environment"),
                config_home_value.get("path"),
            )
        else:
            raise ValidationError("agent config_home must be an object")
        return cls(
            agent_id=value.get("agent_id"),
            display_name=value.get("display_name"),
            command=tuple(command),
            adapter_type=value.get("adapter_type", "cli"),
            prompt_transport=value.get("prompt_transport", "stdin"),
            timeout_seconds=value.get("timeout_seconds", 900),
            capabilities=tuple(capabilities),
            env_allowlist=tuple(env_allowlist),
            config_home=config_home,
            provider_id=value.get("provider_id"),
        )


@dataclass
class TaskState:
    """Provider-neutral facts needed by the next agent to resume work."""

    summary: str = "Not started"
    decisions: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    files_changed: List[str] = field(default_factory=list)
    tests: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.summary = _required_text(self.summary, "summary", max_length=100_000)
        self.decisions = _string_list(self.decisions, "decisions")
        self.constraints = _string_list(self.constraints, "constraints")
        self.files_changed = _string_list(self.files_changed, "files_changed")
        self.tests = _string_list(self.tests, "tests")
        self.next_steps = _string_list(self.next_steps, "next_steps")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "summary": self.summary,
            "decisions": list(self.decisions),
            "constraints": list(self.constraints),
            "files_changed": list(self.files_changed),
            "tests": list(self.tests),
            "next_steps": list(self.next_steps),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TaskState":
        if not isinstance(value, dict):
            raise ValidationError("task state must be an object")
        return cls(
            summary=value.get("summary", "Not started"),
            decisions=value.get("decisions", []),
            constraints=value.get("constraints", []),
            files_changed=value.get("files_changed", []),
            tests=value.get("tests", []),
            next_steps=value.get("next_steps", []),
        )


@dataclass
class ActionRecord:
    """An auditable state transition whose outcome may need confirmation."""

    action_id: str
    kind: str
    agent_id: str
    status: str
    started_at: str
    finished_at: Optional[str] = None
    details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _required_text(self.action_id, "action_id", max_length=64)
        _required_text(self.kind, "kind", max_length=64)
        if not isinstance(self.agent_id, str) or AGENT_ID_PATTERN.fullmatch(self.agent_id) is None:
            raise ValidationError("action agent_id is invalid")
        if self.status not in ACTION_STATUSES:
            raise ValidationError("action status is invalid")
        _required_text(self.started_at, "started_at", max_length=64)
        if self.finished_at is not None:
            _required_text(self.finished_at, "finished_at", max_length=64)
        if not isinstance(self.details, dict):
            raise ValidationError("action details must be an object")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "action_id": self.action_id,
            "kind": self.kind,
            "agent_id": self.agent_id,
            "status": self.status,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "details": dict(self.details),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "ActionRecord":
        if not isinstance(value, dict):
            raise ValidationError("action record must be an object")
        return cls(
            action_id=value.get("action_id"),
            kind=value.get("kind"),
            agent_id=value.get("agent_id"),
            status=value.get("status"),
            started_at=value.get("started_at"),
            finished_at=value.get("finished_at"),
            details=value.get("details", {}),
        )


@dataclass
class TaskCheckpoint:
    """The versioned save point that moves between agent runtimes."""

    task_id: str
    title: str
    goal: str
    state: TaskState
    active_agent: Optional[str]
    status: str
    revision: int
    created_at: str
    updated_at: str
    actions: List[ActionRecord] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    routing_order: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        _required_text(self.task_id, "task_id", max_length=64)
        self.title = _required_text(self.title, "title", max_length=240)
        self.goal = _required_text(self.goal, "goal", max_length=100_000)
        if self.active_agent is not None and AGENT_ID_PATTERN.fullmatch(self.active_agent) is None:
            raise ValidationError("active_agent is invalid")
        if self.status not in TASK_STATUSES:
            raise ValidationError("task status is invalid")
        if isinstance(self.revision, bool) or not isinstance(self.revision, int) or self.revision < 1:
            raise ValidationError("revision must be a positive integer")
        _required_text(self.created_at, "created_at", max_length=64)
        _required_text(self.updated_at, "updated_at", max_length=64)
        if self.schema_version != SCHEMA_VERSION:
            raise ValidationError("unsupported checkpoint schema_version")
        if not isinstance(self.state, TaskState):
            raise ValidationError("state must be a TaskState")
        if not all(isinstance(action, ActionRecord) for action in self.actions):
            raise ValidationError("actions must contain ActionRecord values")
        self.routing_order = _string_list(
            self.routing_order,
            "routing_order",
            max_items=16,
        )
        for agent_id in self.routing_order:
            if AGENT_ID_PATTERN.fullmatch(agent_id) is None:
                raise ValidationError("routing_order contains an invalid agent_id")
        if len(self.routing_order) != len(set(self.routing_order)):
            raise ValidationError("routing_order must not contain duplicate agents")

    @classmethod
    def create(
        cls,
        title: str,
        goal: str,
        active_agent: Optional[str] = None,
        summary: str = "Not started",
    ) -> "TaskCheckpoint":
        now = utc_now()
        return cls(
            task_id=uuid.uuid4().hex,
            title=title,
            goal=goal,
            state=TaskState(summary=summary),
            active_agent=active_agent,
            status="active",
            revision=1,
            created_at=now,
            updated_at=now,
        )

    def unresolved_actions(self) -> List[ActionRecord]:
        return [action for action in self.actions if action.status in {"pending", "unknown"}]

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "title": self.title,
            "goal": self.goal,
            "state": self.state.to_dict(),
            "active_agent": self.active_agent,
            "status": self.status,
            "revision": self.revision,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "actions": [action.to_dict() for action in self.actions],
            "routing_order": list(self.routing_order),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "TaskCheckpoint":
        if not isinstance(value, dict):
            raise ValidationError("checkpoint must be an object")
        action_values = value.get("actions", [])
        if not isinstance(action_values, list):
            raise ValidationError("checkpoint actions must be a list")
        routing_order = value.get("routing_order", [])
        if not isinstance(routing_order, list):
            raise ValidationError("checkpoint routing_order must be a list")
        return cls(
            schema_version=value.get("schema_version"),
            task_id=value.get("task_id"),
            title=value.get("title"),
            goal=value.get("goal"),
            state=TaskState.from_dict(value.get("state", {})),
            active_agent=value.get("active_agent"),
            status=value.get("status"),
            revision=value.get("revision"),
            created_at=value.get("created_at"),
            updated_at=value.get("updated_at"),
            actions=[ActionRecord.from_dict(item) for item in action_values],
            routing_order=routing_order,
        )
