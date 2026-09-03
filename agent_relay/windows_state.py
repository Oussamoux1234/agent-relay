"""Native Windows state operations with fail-closed reparse-point checks."""

from __future__ import annotations

import json
import os
import stat
import time
import uuid
import ctypes
import errno
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple

from .errors import NotFoundError, ValidationError


FILE_ATTRIBUTE_REPARSE_POINT = 0x00000400
Identity = Tuple[int, int]


def _is_reparse_point(metadata: os.stat_result) -> bool:
    attributes = getattr(metadata, "st_file_attributes", 0)
    return bool(attributes & FILE_ATTRIBUTE_REPARSE_POINT) or stat.S_ISLNK(
        metadata.st_mode
    )


def _identity(metadata: os.stat_result) -> Identity:
    return metadata.st_dev, metadata.st_ino


class WindowsStateBackend:
    """Path-safe JSON state backend for native Windows Python."""

    def __init__(self, root: Path, lock_filename: str) -> None:
        if os.name != "nt":
            raise RuntimeError("WindowsStateBackend is available only on Windows")
        self.root = root
        self.tasks_dir = root / "tasks"
        self.lock_path = root / lock_filename
        self._root_identity = self._ensure_directory(self.root, "state root")
        self._validate_local_volume(self.root)
        self._tasks_identity = self._ensure_directory(
            self.tasks_dir,
            "tasks",
            validate_root=True,
        )
        self._lock_identity = self._ensure_lock_file()

    @staticmethod
    def _validate_local_volume(path: Path) -> None:
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetVolumePathNameW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
        )
        kernel32.GetVolumePathNameW.restype = wintypes.BOOL
        kernel32.GetDriveTypeW.argtypes = (wintypes.LPCWSTR,)
        kernel32.GetDriveTypeW.restype = wintypes.UINT
        kernel32.GetVolumeInformationW.argtypes = (
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            wintypes.DWORD,
        )
        kernel32.GetVolumeInformationW.restype = wintypes.BOOL
        volume_path = ctypes.create_unicode_buffer(32_768)
        if not kernel32.GetVolumePathNameW(
            str(path),
            volume_path,
            len(volume_path),
        ):
            raise ValidationError("could not validate the Windows state volume")
        if kernel32.GetDriveTypeW(volume_path.value) != 3:
            raise ValidationError("Windows state must use a local fixed drive")
        filesystem = ctypes.create_unicode_buffer(256)
        if not kernel32.GetVolumeInformationW(
            volume_path.value,
            None,
            0,
            None,
            None,
            None,
            filesystem,
            len(filesystem),
        ):
            raise ValidationError("could not validate the Windows state filesystem")
        if filesystem.value.upper() not in {"NTFS", "REFS"}:
            raise ValidationError("Windows state requires a local NTFS or ReFS volume")

    @staticmethod
    def _metadata(
        path: Path,
        kind: str,
        missing_ok: bool = False,
    ) -> Optional[os.stat_result]:
        try:
            metadata = path.lstat()
        except FileNotFoundError:
            if missing_ok:
                return None
            raise ValidationError("state %s is unavailable: %s" % (kind, path.name))
        except OSError as exc:
            raise ValidationError("could not inspect state %s: %s" % (kind, path.name)) from exc
        if _is_reparse_point(metadata):
            if kind in {"root", "state root", "tasks"}:
                message = (
                    "state directory must not be a symlink, junction, or reparse point: %s"
                    % path.name
                )
            else:
                message = (
                    "state file must be a regular file, not a symlink, junction, or "
                    "reparse point: %s" % path.name
                )
            raise ValidationError(message)
        return metadata

    @classmethod
    def _ensure_directory(
        cls,
        path: Path,
        kind: str,
        validate_root: bool = False,
    ) -> Identity:
        if validate_root:
            cls._validate_directory(path.parent, "state root", None)
        try:
            path.mkdir(mode=0o700, parents=not validate_root, exist_ok=True)
        except OSError as exc:
            raise ValidationError("could not create state directory: %s" % path.name) from exc
        return cls._validate_directory(path, kind, None)

    @classmethod
    def _validate_directory(
        cls,
        path: Path,
        kind: str,
        expected: Optional[Identity],
    ) -> Identity:
        metadata = cls._metadata(path, kind)
        if metadata is None or not stat.S_ISDIR(metadata.st_mode):
            raise ValidationError("state directory is unavailable: %s" % path.name)
        actual = _identity(metadata)
        if expected is not None and actual != expected:
            raise ValidationError("managed state directory was replaced: %s" % path.name)
        return actual

    def _validate_root(self) -> None:
        self._validate_directory(self.root, "root", self._root_identity)

    def _validate_tasks(self) -> None:
        self._validate_root()
        self._validate_directory(self.tasks_dir, "tasks", self._tasks_identity)

    def _validate_parent(self, path: Path) -> None:
        if path.parent == self.root:
            self._validate_root()
            return
        if path.parent == self.tasks_dir:
            self._validate_tasks()
            return
        raise ValidationError("state file is outside the managed state directories")

    @classmethod
    def _open_regular(
        cls,
        path: Path,
        flags: int,
        expected: Optional[Identity] = None,
    ) -> Tuple[int, os.stat_result]:
        before = cls._metadata(path, "file")
        if before is None or not stat.S_ISREG(before.st_mode):
            raise ValidationError(
                "state file must be a regular file, not a reparse point: %s" % path.name
            )
        descriptor = None
        try:
            descriptor = os.open(
                str(path),
                flags
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0),
            )
            opened = os.fstat(descriptor)
        except OSError as exc:
            if descriptor is not None:
                os.close(descriptor)
            raise ValidationError("could not open state file: %s" % path.name) from exc
        opened_identity = _identity(opened)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened_identity != _identity(before)
            or (expected is not None and opened_identity != expected)
        ):
            os.close(descriptor)
            raise ValidationError("state file changed during validation: %s" % path.name)
        return descriptor, opened

    def _ensure_lock_file(self) -> Identity:
        self._validate_root()
        descriptor = None
        try:
            flags = (
                os.O_RDWR
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0)
            )
            try:
                descriptor = os.open(str(self.lock_path), flags, 0o600)
                opened = os.fstat(descriptor)
            except FileExistsError:
                descriptor, opened = self._open_regular(self.lock_path, os.O_RDWR)
            if opened.st_size == 0:
                os.write(descriptor, b"\0")
                os.fsync(descriptor)
                opened = os.fstat(descriptor)
            identity = _identity(opened)
            current = self._metadata(self.lock_path, "lock")
            if current is None or identity != _identity(current):
                raise ValidationError("state lock changed during validation")
            return identity
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError("could not create state lock") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def state_file_exists(self, path: Path) -> bool:
        self._validate_parent(path)
        metadata = self._metadata(path, "file", missing_ok=True)
        if metadata is None:
            return False
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError(
                "state file must be a regular file, not a reparse point: %s" % path.name
            )
        return True

    @contextmanager
    def exclusive_transaction(self) -> Iterator[None]:
        import msvcrt

        self._validate_root()
        descriptor, _ = self._open_regular(
            self.lock_path,
            os.O_RDWR,
            expected=self._lock_identity,
        )
        locked = False
        try:
            while True:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
                    locked = True
                    break
                except OSError as exc:
                    if exc.errno not in {errno.EACCES, errno.EDEADLK}:
                        raise ValidationError("could not acquire state lock") from exc
                    time.sleep(0.05)
            self._validate_root()
            current = self._metadata(self.lock_path, "lock")
            if current is None or _identity(current) != self._lock_identity:
                raise ValidationError("state lock was replaced while waiting")
            yield
        finally:
            if locked:
                try:
                    os.lseek(descriptor, 0, os.SEEK_SET)
                    msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            os.close(descriptor)

    def read_json(self, path: Path) -> Dict[str, Any]:
        self._validate_parent(path)
        descriptor = None
        opened_identity = None
        try:
            descriptor, opened = self._open_regular(path, os.O_RDONLY)
            opened_identity = _identity(opened)
            with os.fdopen(descriptor, "r", encoding="utf-8", closefd=True) as handle:
                descriptor = None
                value = json.load(handle)
            self._validate_parent(path)
            current = self._metadata(path, "file")
            if current is None or _identity(current) != opened_identity:
                raise ValidationError("state file changed during validation: %s" % path.name)
        except ValidationError:
            raise
        except (OSError, json.JSONDecodeError) as exc:
            raise ValidationError("state file is unreadable or invalid: %s" % path.name) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        if not isinstance(value, dict):
            raise ValidationError("state file must contain a JSON object: %s" % path.name)
        return value

    def atomic_write(self, path: Path, value: Dict[str, Any]) -> None:
        self._validate_parent(path)
        temporary = path.parent / (".%s.%s.tmp" % (path.name, uuid.uuid4().hex))
        descriptor = None
        temporary_identity = None
        try:
            flags = (
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_BINARY", 0)
                | getattr(os, "O_NOINHERIT", 0)
            )
            descriptor = os.open(str(temporary), flags, 0o600)
            temporary_identity = _identity(os.fstat(descriptor))
            with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
                descriptor = None
                json.dump(value, handle, indent=2, sort_keys=True, ensure_ascii=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            self._validate_parent(path)
            self._metadata(path, "file", missing_ok=True)
            os.replace(str(temporary), str(path))
            temporary = None
            self._validate_parent(path)
            current = self._metadata(path, "file")
            if current is None or _identity(current) != temporary_identity:
                raise ValidationError("state file changed during atomic replacement: %s" % path.name)
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError("could not persist state file: %s" % path.name) from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def list_task_names(self) -> Tuple[str, ...]:
        self._validate_tasks()
        try:
            names = tuple(sorted(os.listdir(str(self.tasks_dir))))
        except OSError as exc:
            raise ValidationError("could not list state tasks") from exc
        self._validate_tasks()
        return names
