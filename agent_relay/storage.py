"""Atomic, local JSON persistence for the Relay MVP."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, List

from .errors import ConflictError, NotFoundError, ValidationError
from .models import AgentSpec, SCHEMA_VERSION, TaskCheckpoint, utc_now


class RelayStore:
    """Stores user-owned agent definitions and checkpoints under one directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser().resolve()
        self.registry_path = self.root / "agents.json"
        self.tasks_dir = self.root / "tasks"
        self._ensure_private_directory(self.root)
        self._ensure_private_directory(self.tasks_dir)

    @staticmethod
    def _ensure_private_directory(path: Path) -> None:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            path.chmod(0o700)
        except OSError:
            # Some mounted filesystems do not implement POSIX permission changes.
            pass

    @staticmethod
    def _read_json(path: Path) -> Dict[str, Any]:
        try:
            with path.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
        except FileNotFoundError:
            raise NotFoundError("state file was not found: %s" % path.name)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("state file is unreadable or invalid: %s" % path.name) from exc
        if not isinstance(value, dict):
            raise ValidationError("state file must contain a JSON object: %s" % path.name)
        return value

    @staticmethod
    def _atomic_write(path: Path, value: Dict[str, Any]) -> None:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        temporary_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=str(path.parent),
                prefix=".%s." % path.name,
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary_path = Path(handle.name)
                os.chmod(handle.name, 0o600)
                json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(temporary_path), str(path))
            temporary_path = None
        except OSError as exc:
            raise ValidationError("could not persist state file: %s" % path.name) from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def _read_registry(self) -> Dict[str, Any]:
        if not self.registry_path.exists():
            return {"schema_version": SCHEMA_VERSION, "agents": {}}
        registry = self._read_json(self.registry_path)
        if registry.get("schema_version") != SCHEMA_VERSION:
            raise ValidationError("unsupported agent registry schema_version")
        if not isinstance(registry.get("agents"), dict):
            raise ValidationError("agent registry must contain an agents object")
        return registry

    def register_agent(self, spec: AgentSpec, replace: bool = False) -> AgentSpec:
        registry = self._read_registry()
        agents = registry["agents"]
        if spec.agent_id in agents and not replace:
            raise ConflictError("agent already exists; pass --replace to update it")
        agents[spec.agent_id] = spec.to_dict()
        self._atomic_write(self.registry_path, registry)
        return spec

    def get_agent(self, agent_id: str) -> AgentSpec:
        registry = self._read_registry()
        value = registry["agents"].get(agent_id)
        if value is None:
            raise NotFoundError("agent not found: %s" % agent_id)
        return AgentSpec.from_dict(value)

    def list_agents(self) -> List[AgentSpec]:
        registry = self._read_registry()
        return [AgentSpec.from_dict(value) for _, value in sorted(registry["agents"].items())]

    def _task_path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not task_id or not task_id.isalnum() or len(task_id) > 64:
            raise ValidationError("task_id must be an alphanumeric identifier")
        return self.tasks_dir / (task_id + ".json")

    def create_task(self, checkpoint: TaskCheckpoint) -> TaskCheckpoint:
        path = self._task_path(checkpoint.task_id)
        if path.exists():
            raise ConflictError("task already exists: %s" % checkpoint.task_id)
        self._atomic_write(path, checkpoint.to_dict())
        return checkpoint

    def get_task(self, task_id: str) -> TaskCheckpoint:
        path = self._task_path(task_id)
        if not path.exists():
            raise NotFoundError("task not found: %s" % task_id)
        return TaskCheckpoint.from_dict(self._read_json(path))

    def save_task(self, checkpoint: TaskCheckpoint, expected_revision: int) -> TaskCheckpoint:
        path = self._task_path(checkpoint.task_id)
        current = self.get_task(checkpoint.task_id)
        if current.revision != expected_revision:
            raise ConflictError(
                "task changed concurrently: expected revision %d, found %d"
                % (expected_revision, current.revision)
            )
        checkpoint.revision = expected_revision + 1
        checkpoint.updated_at = utc_now()
        self._atomic_write(path, checkpoint.to_dict())
        return checkpoint

    def list_tasks(self) -> List[TaskCheckpoint]:
        checkpoints = []
        for path in sorted(self.tasks_dir.glob("*.json")):
            checkpoints.append(TaskCheckpoint.from_dict(self._read_json(path)))
        return checkpoints
