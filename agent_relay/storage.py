"""Atomic, local JSON persistence for the Relay MVP."""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .errors import ConflictError, NotFoundError, ValidationError
from .health import AgentHealthRecord, HEALTH_SCHEMA_VERSION, MAX_HEALTH_RECORDS
from .models import AgentSpec, SCHEMA_VERSION, TaskCheckpoint, utc_now


class RelayStore:
    """Stores user-owned agent definitions and checkpoints under one directory."""

    _LOCK_FILENAME = ".relay.lock"

    def __init__(self, root: Path) -> None:
        requested_root = Path(root).expanduser()
        absolute_root = Path(os.path.abspath(str(requested_root)))
        self.root = absolute_root.parent.resolve() / absolute_root.name
        if self.root.is_symlink():
            raise ValidationError("state root must not be a symlink")
        self.registry_path = self.root / "agents.json"
        self.health_path = self.root / "health.json"
        self.tasks_dir = self.root / "tasks"
        self._root_identity = self._ensure_private_directory(self.root)
        self._tasks_identity = self._ensure_private_child_directory("tasks")
        self._lock_identity = self._ensure_transaction_lock()

    def require_disjoint_workspace(self, workspace_root: Path) -> None:
        """Reject a write scope that overlaps Relay's trusted state directory."""

        try:
            canonical_workspace = Path(workspace_root).resolve(strict=True)
            canonical_state = self.root.resolve(strict=True)
        except OSError as exc:
            raise ValidationError(
                "state and workspace roots must be available for isolation validation"
            ) from exc
        if (
            canonical_state == canonical_workspace
            or canonical_state.is_relative_to(canonical_workspace)
            or canonical_workspace.is_relative_to(canonical_state)
        ):
            raise ValidationError(
                "Relay state must be outside and disjoint from an authorized workspace"
            )

    @staticmethod
    def _directory_flags() -> int:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        return flags | getattr(os, "O_CLOEXEC", 0)

    @classmethod
    def _ensure_private_directory(cls, path: Path) -> Tuple[int, int]:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise ValidationError("could not create state directory: %s" % path.name) from exc
        descriptor = cls._open_directory(path)
        try:
            opened = os.fstat(descriptor)
            try:
                os.fchmod(descriptor, 0o700)
            except OSError:
                # Some mounted filesystems do not implement POSIX permission changes.
                pass
            return opened.st_dev, opened.st_ino
        finally:
            os.close(descriptor)

    @classmethod
    def _open_directory(
        cls,
        path: Path,
        expected_identity: Optional[Tuple[int, int]] = None,
    ) -> int:
        try:
            metadata = path.lstat()
        except OSError as exc:
            raise ValidationError("state directory is unavailable: %s" % path.name) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError("state directory must not be a symlink: %s" % path.name)
        descriptor = None
        try:
            descriptor = os.open(str(path), cls._directory_flags())
            opened = os.fstat(descriptor)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ValidationError("state directory is unavailable: %s" % path.name) from exc
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            os.close(descriptor)
            raise ValidationError("state directory changed during validation: %s" % path.name)
        if expected_identity is not None and expected_identity != (opened.st_dev, opened.st_ino):
            os.close(descriptor)
            raise ValidationError("managed state directory was replaced: %s" % path.name)
        return descriptor

    def _open_root_directory(self) -> int:
        return self._open_directory(self.root, self._root_identity)

    @classmethod
    def _open_child_directory(
        cls,
        parent_descriptor: int,
        name: str,
        expected_identity: Optional[Tuple[int, int]] = None,
    ) -> int:
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except OSError as exc:
            raise ValidationError("state directory is unavailable: %s" % name) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError("state directory must not be a symlink: %s" % name)
        descriptor = None
        try:
            descriptor = os.open(name, cls._directory_flags(), dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ValidationError("state directory is unavailable: %s" % name) from exc
        if opened.st_dev != metadata.st_dev or opened.st_ino != metadata.st_ino:
            os.close(descriptor)
            raise ValidationError("state directory changed during validation: %s" % name)
        if expected_identity is not None and expected_identity != (opened.st_dev, opened.st_ino):
            os.close(descriptor)
            raise ValidationError("managed state directory was replaced: %s" % name)
        return descriptor

    def _ensure_private_child_directory(self, name: str) -> Tuple[int, int]:
        parent_descriptor = self._open_root_directory()
        descriptor = None
        try:
            try:
                os.mkdir(name, mode=0o700, dir_fd=parent_descriptor)
            except FileExistsError:
                pass
            except OSError as exc:
                raise ValidationError("could not create state directory: %s" % name) from exc
            descriptor = self._open_child_directory(parent_descriptor, name)
            opened = os.fstat(descriptor)
            try:
                os.fchmod(descriptor, 0o700)
            except OSError:
                # Some mounted filesystems do not implement POSIX permission changes.
                pass
            return opened.st_dev, opened.st_ino
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _open_tasks_directory(self) -> int:
        parent_descriptor = self._open_root_directory()
        try:
            return self._open_child_directory(
                parent_descriptor,
                "tasks",
                self._tasks_identity,
            )
        finally:
            os.close(parent_descriptor)

    def _ensure_transaction_lock(self) -> Tuple[int, int]:
        """Create or validate the stable inode used for local process locking."""

        parent_descriptor = self._open_root_directory()
        descriptor = None
        try:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(
                    self._LOCK_FILENAME,
                    flags | os.O_CREAT | os.O_EXCL,
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except FileExistsError:
                descriptor = os.open(
                    self._LOCK_FILENAME,
                    flags,
                    dir_fd=parent_descriptor,
                )
            opened = os.fstat(descriptor)
            metadata = self._file_metadata(parent_descriptor, self._LOCK_FILENAME)
            if (
                metadata is None
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise ValidationError("state lock changed during validation")
            return opened.st_dev, opened.st_ino
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError("could not create state lock") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _open_parent(self, path: Path) -> int:
        if path.parent == self.root:
            return self._open_root_directory()
        if path.parent == self.tasks_dir:
            return self._open_tasks_directory()
        raise ValidationError("state file is outside the managed state directories")

    @staticmethod
    def _file_metadata(parent_descriptor: int, name: str) -> Optional[os.stat_result]:
        try:
            metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise ValidationError("could not inspect state file: %s" % name) from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("state file must be a regular file, not a symlink: %s" % name)
        return metadata

    def _state_file_exists(self, path: Path) -> bool:
        parent_descriptor = self._open_parent(path)
        try:
            return self._file_metadata(parent_descriptor, path.name) is not None
        finally:
            os.close(parent_descriptor)

    @contextmanager
    def _exclusive_transaction(self) -> Iterator[None]:
        """Serialize one complete read-modify-write state transaction."""

        parent_descriptor = self._open_root_directory()
        descriptor = None
        locked = False
        try:
            flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            try:
                descriptor = os.open(
                    self._LOCK_FILENAME,
                    flags,
                    dir_fd=parent_descriptor,
                )
                opened = os.fstat(descriptor)
            except OSError as exc:
                raise ValidationError("could not open state lock") from exc
            metadata = self._file_metadata(parent_descriptor, self._LOCK_FILENAME)
            if (
                metadata is None
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or self._lock_identity != (opened.st_dev, opened.st_ino)
            ):
                raise ValidationError("state lock changed during validation")
            while True:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_EX)
                    break
                except InterruptedError:
                    continue
                except OSError as exc:
                    raise ValidationError("could not acquire state lock") from exc
            locked = True
            metadata = self._file_metadata(parent_descriptor, self._LOCK_FILENAME)
            if (
                metadata is None
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
                or self._lock_identity != (opened.st_dev, opened.st_ino)
            ):
                raise ValidationError("state lock was replaced while waiting")
            yield
        finally:
            if locked and descriptor is not None:
                try:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                except OSError:
                    pass
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)

    def _read_json(self, path: Path) -> Dict[str, Any]:
        parent_descriptor = self._open_parent(path)
        descriptor = None
        try:
            metadata = self._file_metadata(parent_descriptor, path.name)
            if metadata is None:
                raise NotFoundError("state file was not found: %s" % path.name)
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(path.name, flags, dir_fd=parent_descriptor)
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_dev != metadata.st_dev
                or opened.st_ino != metadata.st_ino
            ):
                raise ValidationError("state file changed during validation: %s" % path.name)
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
                descriptor = None
                value = json.load(handle)
        except NotFoundError:
            raise
        except ValidationError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("state file is unreadable or invalid: %s" % path.name) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_descriptor)
        if not isinstance(value, dict):
            raise ValidationError("state file must contain a JSON object: %s" % path.name)
        return value

    def _atomic_write(self, path: Path, value: Dict[str, Any]) -> None:
        parent_descriptor = self._open_parent(path)
        temporary_name = ".%s.%s.tmp" % (path.name, uuid.uuid4().hex)
        descriptor = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            flags |= getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(temporary_name, flags, 0o600, dir_fd=parent_descriptor)
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                descriptor = None
                json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._file_metadata(parent_descriptor, path.name)
            os.replace(
                temporary_name,
                path.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
            )
            temporary_name = ""
            try:
                os.fsync(parent_descriptor)
            except OSError:
                # The replacement completed; some filesystems cannot sync directories.
                pass
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError("could not persist state file: %s" % path.name) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary_name:
                try:
                    os.unlink(temporary_name, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
            os.close(parent_descriptor)

    def _read_registry(self) -> Dict[str, Any]:
        if not self._state_file_exists(self.registry_path):
            return {"schema_version": SCHEMA_VERSION, "agents": {}}
        registry = self._read_json(self.registry_path)
        if registry.get("schema_version") != SCHEMA_VERSION:
            raise ValidationError("unsupported agent registry schema_version")
        if not isinstance(registry.get("agents"), dict):
            raise ValidationError("agent registry must contain an agents object")
        return registry

    def register_agent(self, spec: AgentSpec, replace: bool = False) -> AgentSpec:
        with self._exclusive_transaction():
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

    def _read_health(self) -> Dict[str, Any]:
        if not self._state_file_exists(self.health_path):
            return {"schema_version": HEALTH_SCHEMA_VERSION, "agents": {}}
        health = self._read_json(self.health_path)
        if health.get("schema_version") != HEALTH_SCHEMA_VERSION:
            raise ValidationError("unsupported health registry schema_version")
        agents = health.get("agents")
        if not isinstance(agents, dict):
            raise ValidationError("health registry must contain an agents object")
        if len(agents) > MAX_HEALTH_RECORDS:
            raise ValidationError("health registry exceeds the maximum record count")
        for agent_id, value in agents.items():
            record = AgentHealthRecord.from_dict(value)
            if agent_id != record.agent_id:
                raise ValidationError("health registry key does not match record agent_id")
        return health

    def set_agent_health(self, record: AgentHealthRecord) -> AgentHealthRecord:
        with self._exclusive_transaction():
            health = self._read_health()
            agents = health["agents"]
            if record.agent_id not in agents and len(agents) >= MAX_HEALTH_RECORDS:
                raise ValidationError("health registry exceeds the maximum record count")
            agents[record.agent_id] = record.to_dict()
            self._atomic_write(self.health_path, health)
        return record

    def get_agent_health(self, agent_id: str) -> Optional[AgentHealthRecord]:
        if not isinstance(agent_id, str) or not agent_id:
            raise ValidationError("agent_id must be a non-empty string")
        value = self._read_health()["agents"].get(agent_id)
        return AgentHealthRecord.from_dict(value) if value is not None else None

    def list_agent_health(self) -> List[AgentHealthRecord]:
        health = self._read_health()
        return [
            AgentHealthRecord.from_dict(value)
            for _, value in sorted(health["agents"].items())
        ]

    def clear_agent_health(self, agent_id: str) -> bool:
        with self._exclusive_transaction():
            health = self._read_health()
            if agent_id not in health["agents"]:
                return False
            del health["agents"][agent_id]
            self._atomic_write(self.health_path, health)
            return True

    def _task_path(self, task_id: str) -> Path:
        if not isinstance(task_id, str) or not task_id or not task_id.isalnum() or len(task_id) > 64:
            raise ValidationError("task_id must be an alphanumeric identifier")
        return self.tasks_dir / (task_id + ".json")

    def create_task(self, checkpoint: TaskCheckpoint) -> TaskCheckpoint:
        path = self._task_path(checkpoint.task_id)
        with self._exclusive_transaction():
            if self._state_file_exists(path):
                raise ConflictError("task already exists: %s" % checkpoint.task_id)
            self._atomic_write(path, checkpoint.to_dict())
        return checkpoint

    def get_task(self, task_id: str) -> TaskCheckpoint:
        path = self._task_path(task_id)
        if not self._state_file_exists(path):
            raise NotFoundError("task not found: %s" % task_id)
        return TaskCheckpoint.from_dict(self._read_json(path))

    def save_task(self, checkpoint: TaskCheckpoint, expected_revision: int) -> TaskCheckpoint:
        path = self._task_path(checkpoint.task_id)
        with self._exclusive_transaction():
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
        directory_descriptor = self._open_tasks_directory()
        try:
            names = sorted(os.listdir(directory_descriptor))
        except OSError as exc:
            raise ValidationError("could not list state tasks") from exc
        finally:
            os.close(directory_descriptor)
        for name in names:
            if name.endswith(".json"):
                path = self.tasks_dir / name
                checkpoints.append(TaskCheckpoint.from_dict(self._read_json(path)))
        return checkpoints
