"""Bounded Git workspace snapshots and review-safe change summaries."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Iterable, Optional, Tuple

from .errors import ValidationError


MAX_GIT_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_WORKSPACE_PATHS = 2_000
MAX_HASHED_FILE_BYTES = 128 * 1024 * 1024
MAX_HASHED_TOTAL_BYTES = 512 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 15
WORKSPACE_SNAPSHOT_VERSION = "2"
LEGACY_WORKSPACE_SNAPSHOT_VERSION = "1"
SUPPORTED_WORKSPACE_SNAPSHOT_VERSIONS = frozenset(
    (LEGACY_WORKSPACE_SNAPSHOT_VERSION, WORKSPACE_SNAPSHOT_VERSION)
)
REVIEW_STATUSES = frozenset(("pending", "accepted", "rolled-back", "clean", "unavailable"))
ROLLBACK_GUIDANCE = (
    "Inspect git status, unstaged diff, staged diff, and recent commits before deciding.",
    "Do not discard pre-existing changes; restore only tracked paths confirmed to belong to this action.",
    "Remove untracked paths only after confirming this action created them; Relay never deletes them automatically.",
    "If HEAD or the branch changed, inspect the Git log and recover manually; Relay never resets Git history.",
)


@dataclass(frozen=True)
class WorkspaceFileState:
    """Content and index state for one currently dirty workspace path."""

    kind: str
    size: int
    mode: int
    digest: str
    tracked: bool
    staged: bool
    index_digest: str = ""
    index_entry_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "kind": self.kind,
            "size": self.size,
            "mode": self.mode,
            "digest": self.digest,
            "tracked": self.tracked,
            "staged": self.staged,
            "index_digest": self.index_digest,
            "index_entry_count": self.index_entry_count,
        }

    def to_legacy_dict(self) -> Dict[str, Any]:
        """Return the v1 representation used to validate safe legacy reviews."""

        return {
            "kind": self.kind,
            "size": self.size,
            "mode": self.mode,
            "digest": self.digest,
            "tracked": self.tracked,
            "staged": self.staged,
        }


@dataclass(frozen=True)
class WorkspaceIndexEntry:
    """Content-free identity for one path and stage in the Git index."""

    mode: str
    object_id: str
    stage: int

    def to_dict(self) -> Dict[str, Any]:
        return {
            "mode": self.mode,
            "object_id": self.object_id,
            "stage": self.stage,
        }


@dataclass(frozen=True)
class WorkspaceSnapshot:
    """Private in-memory snapshot used to compare one bounded Git workspace."""

    root: str
    head: str
    branch: Optional[str]
    files: Dict[str, WorkspaceFileState]
    digest: str
    legacy_digest: str
    snapshot_version: str = WORKSPACE_SNAPSHOT_VERSION

    @property
    def dirty_paths(self) -> Tuple[str, ...]:
        return tuple(sorted(self.files))


@dataclass(frozen=True)
class WorkspaceReview:
    """Persistable, content-free summary of effects observed around one agent run."""

    status: str
    workspace_root: str
    before_digest: str
    after_digest: Optional[str]
    before_head: str
    after_head: Optional[str]
    before_branch: Optional[str]
    after_branch: Optional[str]
    preexisting_paths: Tuple[str, ...] = ()
    introduced_paths: Tuple[str, ...] = ()
    modified_paths: Tuple[str, ...] = ()
    removed_paths: Tuple[str, ...] = ()
    final_dirty_paths: Tuple[str, ...] = ()
    error_code: Optional[str] = None
    reviewed_at: Optional[str] = None
    snapshot_version: str = WORKSPACE_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        if self.status not in REVIEW_STATUSES:
            raise ValidationError("workspace review status is invalid")
        if self.snapshot_version not in SUPPORTED_WORKSPACE_SNAPSHOT_VERSIONS:
            raise ValidationError("workspace review snapshot_version is invalid")
        if (
            not isinstance(self.workspace_root, str)
            or len(self.workspace_root) > 4_096
            or any(character in self.workspace_root for character in ("\x00", "\r", "\n"))
        ):
            raise ValidationError("workspace review root must be an absolute path")
        root = Path(self.workspace_root)
        if not root.is_absolute():
            raise ValidationError("workspace review root must be absolute")
        for digest_name, digest in (
            ("before_digest", self.before_digest),
            ("after_digest", self.after_digest),
        ):
            if digest is not None and (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise ValidationError("workspace review %s is invalid" % digest_name)
        for head_name, head in (
            ("before_head", self.before_head),
            ("after_head", self.after_head),
        ):
            if head is not None and (
                not isinstance(head, str)
                or len(head) not in (40, 64)
                or any(character not in "0123456789abcdef" for character in head)
            ):
                raise ValidationError("workspace review %s is invalid" % head_name)
        for branch_name, branch in (
            ("before_branch", self.before_branch),
            ("after_branch", self.after_branch),
        ):
            if branch is not None and (
                not isinstance(branch, str)
                or not branch
                or len(branch) > 1_024
                or any(character in branch for character in ("\x00", "\r", "\n"))
            ):
                raise ValidationError("workspace review %s is invalid" % branch_name)
        for name in (
            "preexisting_paths",
            "introduced_paths",
            "modified_paths",
            "removed_paths",
            "final_dirty_paths",
        ):
            paths = getattr(self, name)
            if not isinstance(paths, tuple) or len(paths) > MAX_WORKSPACE_PATHS:
                raise ValidationError("workspace review %s is invalid" % name)
            if not all(self._safe_relative_path(path) for path in paths):
                raise ValidationError("workspace review paths must be non-empty strings")
            if paths != tuple(sorted(set(paths))):
                raise ValidationError("workspace review paths must be sorted and unique")
        if self.error_code is not None and (
            not isinstance(self.error_code, str) or len(self.error_code) > 128
        ):
            raise ValidationError("workspace review error_code is invalid")
        if self.reviewed_at is not None and (
            not isinstance(self.reviewed_at, str)
            or not self.reviewed_at
            or len(self.reviewed_at) > 64
            or any(character in self.reviewed_at for character in ("\x00", "\r", "\n"))
        ):
            raise ValidationError("workspace review reviewed_at is invalid")
        if self.status == "unavailable":
            if self.after_digest is not None or self.after_head is not None:
                raise ValidationError("unavailable workspace review cannot have an after snapshot")
            if self.error_code is None:
                raise ValidationError("unavailable workspace review must have an error_code")
        elif self.after_digest is None or self.after_head is None:
            raise ValidationError("workspace review is missing its after snapshot")
        elif self.error_code is not None:
            raise ValidationError("available workspace review cannot have an error_code")
        if self.status in {"accepted", "rolled-back"} and self.reviewed_at is None:
            raise ValidationError("resolved workspace review must have reviewed_at")

    @staticmethod
    def _safe_relative_path(value: Any) -> bool:
        if not isinstance(value, str) or not value or len(value) > 4_096:
            return False
        path = PurePosixPath(value)
        return not path.is_absolute() and bool(path.parts) and ".." not in path.parts

    @property
    def has_changes(self) -> bool:
        return self.after_digest is not None and self.before_digest != self.after_digest

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "workspace_root": self.workspace_root,
            "before_digest": self.before_digest,
            "after_digest": self.after_digest,
            "before_head": self.before_head,
            "after_head": self.after_head,
            "before_branch": self.before_branch,
            "after_branch": self.after_branch,
            "head_changed": (
                self.after_head is not None and self.before_head != self.after_head
            ),
            "branch_changed": (
                self.after_digest is not None and self.before_branch != self.after_branch
            ),
            "preexisting_paths": list(self.preexisting_paths),
            "introduced_paths": list(self.introduced_paths),
            "modified_paths": list(self.modified_paths),
            "removed_paths": list(self.removed_paths),
            "final_dirty_paths": list(self.final_dirty_paths),
            "error_code": self.error_code,
            "reviewed_at": self.reviewed_at,
            "snapshot_version": self.snapshot_version,
            "rollback_guidance": list(ROLLBACK_GUIDANCE),
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "WorkspaceReview":
        if not isinstance(value, dict):
            raise ValidationError("workspace review must be an object")
        list_fields = (
            "preexisting_paths",
            "introduced_paths",
            "modified_paths",
            "removed_paths",
            "final_dirty_paths",
        )
        for field_name in list_fields:
            if not isinstance(value.get(field_name, []), list):
                raise ValidationError("workspace review %s must be a list" % field_name)
        return cls(
            status=value.get("status"),
            workspace_root=value.get("workspace_root"),
            before_digest=value.get("before_digest"),
            after_digest=value.get("after_digest"),
            before_head=value.get("before_head"),
            after_head=value.get("after_head"),
            before_branch=value.get("before_branch"),
            after_branch=value.get("after_branch"),
            preexisting_paths=tuple(value.get("preexisting_paths", [])),
            introduced_paths=tuple(value.get("introduced_paths", [])),
            modified_paths=tuple(value.get("modified_paths", [])),
            removed_paths=tuple(value.get("removed_paths", [])),
            final_dirty_paths=tuple(value.get("final_dirty_paths", [])),
            error_code=value.get("error_code"),
            reviewed_at=value.get("reviewed_at"),
            snapshot_version=value.get(
                "snapshot_version",
                LEGACY_WORKSPACE_SNAPSHOT_VERSION,
            ),
        )


class WorkspaceInspector:
    """Validate one Git root and compare bounded, content-free workspace snapshots."""

    def __init__(self, git_executable: Optional[str] = None) -> None:
        self.git_executable = git_executable or shutil.which("git")

    @staticmethod
    def _environment() -> Dict[str, str]:
        environment = {
            name: os.environ[name]
            for name in (
                "HOME",
                "LANG",
                "LC_ALL",
                "PATH",
                "PATHEXT",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "TMPDIR",
                "USERPROFILE",
                "WINDIR",
            )
            if name in os.environ
        }
        environment.update(
            {
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": os.devnull,
                "GIT_EXTERNAL_DIFF": "",
                "GIT_OPTIONAL_LOCKS": "0",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        return environment

    def _run_git(
        self,
        root: Path,
        arguments: Iterable[str],
        allowed_return_codes: Tuple[int, ...] = (0,),
    ) -> bytes:
        if self.git_executable is None:
            raise ValidationError("git executable is required for workspace review")
        try:
            completed = subprocess.run(
                [
                    self.git_executable,
                    "-c",
                    "core.fsmonitor=false",
                    "-c",
                    "core.hooksPath=%s" % os.devnull,
                    "-C",
                    str(root),
                ]
                + list(arguments),
                cwd=str(root),
                env=self._environment(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired) as exc:
            raise ValidationError("could not inspect the Git workspace") from exc
        if (
            len(completed.stdout) > MAX_GIT_OUTPUT_BYTES
            or len(completed.stderr) > MAX_GIT_OUTPUT_BYTES
        ):
            raise ValidationError("Git workspace inspection output is too large")
        if completed.returncode not in allowed_return_codes:
            raise ValidationError("Git workspace inspection failed")
        return completed.stdout

    def validate_root(self, workspace_root: Path) -> Path:
        candidate = Path(workspace_root).expanduser().resolve()
        candidate_text = str(candidate)
        if len(candidate_text) > 4_096 or any(
            character in candidate_text for character in ("\x00", "\r", "\n")
        ):
            raise ValidationError("workspace_root contains an invalid character")
        if not candidate.is_dir():
            raise ValidationError("workspace_root must be an existing directory")
        raw_root = self._run_git(candidate, ("rev-parse", "--show-toplevel"))
        try:
            reported = raw_root.decode("utf-8").rstrip("\n")
        except UnicodeDecodeError as exc:
            raise ValidationError("Git workspace root must be valid UTF-8") from exc
        if not reported:
            raise ValidationError("workspace_root must be a Git repository")
        git_root = Path(reported).resolve()
        if candidate != git_root:
            raise ValidationError("workspace_root must be the Git repository top level")
        return candidate

    def validate_working_directory(
        self,
        working_directory: Path,
        authorized_root: str,
    ) -> Path:
        root = self.validate_root(Path(authorized_root))
        working = Path(working_directory).expanduser().resolve()
        if working != root:
            raise ValidationError("write execution cwd must equal the authorized workspace root")
        return root

    @staticmethod
    def _decode_paths(raw: bytes) -> Tuple[str, ...]:
        try:
            values = [item.decode("utf-8") for item in raw.split(b"\0") if item]
        except UnicodeDecodeError as exc:
            raise ValidationError("workspace paths must be valid UTF-8") from exc
        if len(values) > MAX_WORKSPACE_PATHS:
            raise ValidationError("workspace has too many changed paths to review safely")
        normalized = []
        for value in values:
            if not WorkspaceReview._safe_relative_path(value):
                raise ValidationError("Git returned an unsafe workspace path")
            normalized.append(value)
        return tuple(sorted(set(normalized)))

    @staticmethod
    def _validate_index_visibility(raw: bytes) -> None:
        """Reject index flags that intentionally hide tracked worktree changes."""

        for record in raw.split(b"\0"):
            if not record:
                continue
            if len(record) < 3 or record[1:2] != b" ":
                raise ValidationError("Git returned invalid index visibility metadata")
            tag = record[0:1]
            try:
                path = record[2:].decode("utf-8")
            except UnicodeDecodeError as exc:
                raise ValidationError("workspace paths must be valid UTF-8") from exc
            if not WorkspaceReview._safe_relative_path(path):
                raise ValidationError("Git returned an unsafe workspace path")
            if b"a" <= tag <= b"z":
                raise ValidationError(
                    "workspace contains assume-unchanged index entries; clear them before review"
                )
            if tag == b"S":
                raise ValidationError(
                    "workspace contains skip-worktree index entries; use a full checkout for review"
                )
            if tag not in {b"H", b"M", b"R", b"C", b"K", b"?"}:
                raise ValidationError("Git returned unsupported index visibility metadata")

    @staticmethod
    def _decode_index_entries(
        raw: bytes,
        included_paths: Iterable[str],
    ) -> Dict[str, Tuple[WorkspaceIndexEntry, ...]]:
        """Decode bounded stage, mode, and object identities for relevant paths."""

        included = set(included_paths)
        entries: Dict[str, list] = {}
        for record in raw.split(b"\0"):
            if not record:
                continue
            metadata, separator, raw_path = record.partition(b"\t")
            if not separator:
                raise ValidationError("Git returned invalid index entry metadata")
            try:
                path = raw_path.decode("utf-8")
                mode_raw, object_id_raw, stage_raw = metadata.split(b" ")
                mode = mode_raw.decode("ascii")
                object_id = object_id_raw.decode("ascii")
                stage_text = stage_raw.decode("ascii")
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValidationError("Git returned invalid index entry metadata") from exc
            if not WorkspaceReview._safe_relative_path(path):
                raise ValidationError("Git returned an unsafe workspace path")
            if path not in included:
                continue
            if len(mode) != 6 or any(character not in "01234567" for character in mode):
                raise ValidationError("Git returned an invalid index mode")
            if len(object_id) not in (40, 64) or any(
                character not in "0123456789abcdef" for character in object_id
            ):
                raise ValidationError("Git returned an invalid index object id")
            if stage_text not in {"0", "1", "2", "3"}:
                raise ValidationError("Git returned an invalid index stage")
            entries.setdefault(path, []).append(
                WorkspaceIndexEntry(mode, object_id, int(stage_text))
            )

        normalized: Dict[str, Tuple[WorkspaceIndexEntry, ...]] = {}
        for path, path_entries in entries.items():
            ordered = tuple(
                sorted(
                    path_entries,
                    key=lambda entry: (entry.stage, entry.mode, entry.object_id),
                )
            )
            if len({entry.stage for entry in ordered}) != len(ordered):
                raise ValidationError("Git returned duplicate index stages for one path")
            normalized[path] = ordered
        return normalized

    @staticmethod
    def _index_digest(entries: Tuple[WorkspaceIndexEntry, ...]) -> str:
        encoded = json.dumps(
            [entry.to_dict() for entry in entries],
            sort_keys=True,
            separators=(",", ":"),
        ).encode("ascii")
        return hashlib.sha256(encoded).hexdigest()

    @staticmethod
    def _path_within_root(root: Path, relative_path: str) -> Path:
        candidate = root.joinpath(*PurePosixPath(relative_path).parts)
        parent = candidate.parent.resolve()
        try:
            parent.relative_to(root)
        except ValueError as exc:
            raise ValidationError("workspace path escapes the authorized root") from exc
        return candidate

    def _file_state(
        self,
        root: Path,
        relative_path: str,
        tracked: bool,
        staged: bool,
        index_entries: Tuple[WorkspaceIndexEntry, ...],
        hashed_bytes: int,
    ) -> Tuple[WorkspaceFileState, int]:
        candidate = self._path_within_root(root, relative_path)
        index_digest = self._index_digest(index_entries)
        index_entry_count = len(index_entries)
        try:
            metadata = candidate.lstat()
        except FileNotFoundError:
            return (
                WorkspaceFileState(
                    "missing",
                    0,
                    0,
                    "",
                    tracked,
                    staged,
                    index_digest,
                    index_entry_count,
                ),
                hashed_bytes,
            )
        except OSError as exc:
            raise ValidationError("could not inspect a changed workspace path") from exc

        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISLNK(metadata.st_mode):
            try:
                target = os.fsencode(os.readlink(str(candidate)))
            except OSError as exc:
                raise ValidationError("could not inspect a workspace symlink") from exc
            return (
                WorkspaceFileState(
                    "symlink",
                    len(target),
                    mode,
                    hashlib.sha256(target).hexdigest(),
                    tracked,
                    staged,
                    index_digest,
                    index_entry_count,
                ),
                hashed_bytes + len(target),
            )
        if not stat.S_ISREG(metadata.st_mode):
            raise ValidationError("changed workspace paths must be regular files or symlinks")
        descriptor = None
        try:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(str(candidate), flags)
            opened_metadata = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened_metadata.st_mode)
                or opened_metadata.st_dev != metadata.st_dev
                or opened_metadata.st_ino != metadata.st_ino
            ):
                raise ValidationError("changed workspace file changed during inspection")
            if opened_metadata.st_size > MAX_HASHED_FILE_BYTES:
                raise ValidationError("changed workspace file exceeds the review size limit")
            if hashed_bytes + opened_metadata.st_size > MAX_HASHED_TOTAL_BYTES:
                raise ValidationError("changed workspace content exceeds the review size limit")
            digest = hashlib.sha256()
            with os.fdopen(descriptor, "rb", closefd=True) as handle:
                descriptor = None
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
        except ValidationError:
            raise
        except OSError as exc:
            raise ValidationError("could not hash a changed workspace file") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
        return (
            WorkspaceFileState(
                "file",
                opened_metadata.st_size,
                mode,
                digest.hexdigest(),
                tracked,
                staged,
                index_digest,
                index_entry_count,
            ),
            hashed_bytes + opened_metadata.st_size,
        )

    def snapshot(self, workspace_root: Path) -> WorkspaceSnapshot:
        root = self.validate_root(workspace_root)
        index_visibility_raw = self._run_git(root, ("ls-files", "-v", "-z"))
        self._validate_index_visibility(index_visibility_raw)
        head_raw = self._run_git(root, ("rev-parse", "--verify", "HEAD^{commit}"))
        branch_raw = self._run_git(
            root,
            ("symbolic-ref", "--quiet", "--short", "HEAD"),
            allowed_return_codes=(0, 1),
        )
        tracked_raw = self._run_git(
            root,
            ("diff", "--no-ext-diff", "--no-renames", "--name-only", "-z", "HEAD", "--"),
        )
        staged_raw = self._run_git(
            root,
            (
                "diff",
                "--cached",
                "--no-ext-diff",
                "--no-renames",
                "--name-only",
                "-z",
                "HEAD",
                "--",
            ),
        )
        untracked_raw = self._run_git(
            root,
            ("ls-files", "--others", "--exclude-standard", "-z"),
        )
        ignored_raw = self._run_git(
            root,
            ("ls-files", "--others", "--ignored", "--exclude-standard", "-z"),
        )
        index_entries_raw = self._run_git(root, ("ls-files", "--stage", "-z"))
        try:
            head = head_raw.decode("ascii").strip()
            branch_value = branch_raw.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise ValidationError("Git revision metadata is invalid") from exc
        if len(head) not in (40, 64) or any(
            character not in "0123456789abcdef" for character in head
        ):
            raise ValidationError("Git HEAD is invalid")
        branch = branch_value or None

        tracked_paths = set(self._decode_paths(tracked_raw))
        staged_paths = set(self._decode_paths(staged_raw))
        untracked_paths = set(self._decode_paths(untracked_raw))
        ignored_paths = set(self._decode_paths(ignored_raw))
        untracked_paths.update(ignored_paths)
        paths = tracked_paths.union(staged_paths, untracked_paths)
        if len(paths) > MAX_WORKSPACE_PATHS:
            raise ValidationError("workspace has too many changed paths to review safely")
        index_entries = self._decode_index_entries(index_entries_raw, paths)

        files: Dict[str, WorkspaceFileState] = {}
        hashed_bytes = 0
        for relative_path in sorted(paths):
            file_state, hashed_bytes = self._file_state(
                root,
                relative_path,
                relative_path not in untracked_paths,
                relative_path in staged_paths,
                index_entries.get(relative_path, ()),
                hashed_bytes,
            )
            files[relative_path] = file_state

        legacy_digest_value = {
            "root": str(root),
            "head": head,
            "branch": branch,
            "files": {path: files[path].to_legacy_dict() for path in sorted(files)},
        }
        legacy_digest = hashlib.sha256(
            json.dumps(
                legacy_digest_value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        digest_value = {
            "snapshot_version": WORKSPACE_SNAPSHOT_VERSION,
            "root": str(root),
            "head": head,
            "branch": branch,
            "files": {path: files[path].to_dict() for path in sorted(files)},
        }
        digest = hashlib.sha256(
            json.dumps(
                digest_value,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()
        return WorkspaceSnapshot(
            str(root),
            head,
            branch,
            files,
            digest,
            legacy_digest,
        )

    @staticmethod
    def compare(before: WorkspaceSnapshot, after: WorkspaceSnapshot) -> WorkspaceReview:
        if before.root != after.root:
            raise ValidationError("workspace snapshots have different roots")
        if before.snapshot_version != after.snapshot_version:
            raise ValidationError("workspace snapshots have different versions")
        before_paths = set(before.files)
        after_paths = set(after.files)
        introduced = tuple(sorted(after_paths.difference(before_paths)))
        removed = tuple(sorted(before_paths.difference(after_paths)))
        modified = tuple(
            sorted(
                path
                for path in before_paths.intersection(after_paths)
                if before.files[path] != after.files[path]
            )
        )
        status = "clean" if before.digest == after.digest else "pending"
        return WorkspaceReview(
            status=status,
            workspace_root=before.root,
            before_digest=before.digest,
            after_digest=after.digest,
            before_head=before.head,
            after_head=after.head,
            before_branch=before.branch,
            after_branch=after.branch,
            preexisting_paths=before.dirty_paths,
            introduced_paths=introduced,
            modified_paths=modified,
            removed_paths=removed,
            final_dirty_paths=after.dirty_paths,
            snapshot_version=before.snapshot_version,
        )

    @staticmethod
    def unavailable(before: WorkspaceSnapshot, error_code: str) -> WorkspaceReview:
        return WorkspaceReview(
            status="unavailable",
            workspace_root=before.root,
            before_digest=before.digest,
            after_digest=None,
            before_head=before.head,
            after_head=None,
            before_branch=before.branch,
            after_branch=None,
            preexisting_paths=before.dirty_paths,
            error_code=error_code,
            snapshot_version=before.snapshot_version,
        )
