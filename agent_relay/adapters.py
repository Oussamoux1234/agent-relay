"""Agent runtime adapters for the local-first MVP."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

from .errors import ConflictError, NotFoundError, ValidationError
from .models import AgentSpec


BASE_ENVIRONMENT_NAMES = ("HOME", "LANG", "LC_ALL", "PATH", "TERM", "TMPDIR")
MAX_CAPTURED_OUTPUT_BYTES = 64 * 1024
MAX_ARGUMENT_PROMPT_CHARACTERS = 32_000


@dataclass(frozen=True)
class AgentExecutionResult:
    """A bounded process result with explicit effect uncertainty."""

    status: str
    return_code: Optional[int]
    stdout: str
    stderr: str
    elapsed_ms: int
    started: bool
    timed_out: bool = False
    error: Optional[str] = None
    session_id: Optional[str] = None
    turn_id: Optional[str] = None
    protocol_status: Optional[str] = None
    event_types: Tuple[str, ...] = ()


class AgentAdapter(Protocol):
    """Runtime boundary implemented by CLI, API, and future app connectors."""

    adapter_type: str

    def validate_execution(
        self,
        spec: AgentSpec,
        prompt: str,
        working_directory: Path,
    ) -> Path:
        ...

    def execute(
        self,
        spec: AgentSpec,
        prompt: str,
        working_directory: Path,
    ) -> AgentExecutionResult:
        ...


@runtime_checkable
class SessionAgentAdapter(AgentAdapter, Protocol):
    """Runtime contract for adapters that can resume an external conversation."""

    def validate_session_id(self, session_id: Optional[str]) -> Optional[str]:
        ...

    def execute_session(
        self,
        spec: AgentSpec,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str],
    ) -> AgentExecutionResult:
        ...


class AdapterRegistry:
    """Maps versioned agent specifications to runtime implementations."""

    def __init__(self) -> None:
        self._adapters: Dict[str, AgentAdapter] = {}

    def register(self, adapter: AgentAdapter, replace: bool = False) -> None:
        adapter_type = getattr(adapter, "adapter_type", None)
        if not isinstance(adapter_type, str) or not adapter_type:
            raise ValidationError("adapter must expose a non-empty adapter_type")
        if adapter_type in self._adapters and not replace:
            raise ConflictError("adapter is already registered: %s" % adapter_type)
        self._adapters[adapter_type] = adapter

    def get(self, adapter_type: str) -> AgentAdapter:
        adapter = self._adapters.get(adapter_type)
        if adapter is None:
            raise NotFoundError("runtime adapter is not available: %s" % adapter_type)
        return adapter

    def available_types(self) -> Tuple[str, ...]:
        return tuple(sorted(self._adapters))


class CliAgentAdapter:
    """Executes a configured argv list without invoking a shell."""

    adapter_type = "cli"

    @staticmethod
    def _environment(spec: AgentSpec) -> Dict[str, str]:
        allowed_names = set(BASE_ENVIRONMENT_NAMES).union(spec.env_allowlist)
        environment = {name: os.environ[name] for name in allowed_names if name in os.environ}
        if spec.config_home is not None:
            environment[spec.config_home[0]] = spec.config_home[1]
        return environment

    @staticmethod
    def _read_tail(handle: object, max_bytes: int = MAX_CAPTURED_OUTPUT_BYTES) -> str:
        handle.seek(0, os.SEEK_END)
        size = handle.tell()
        handle.seek(max(0, size - max_bytes))
        value = handle.read(max_bytes)
        return value.decode("utf-8", errors="replace")

    @staticmethod
    def _process_group_exists(process_group_id: int) -> bool:
        try:
            os.killpg(process_group_id, 0)
            return True
        except ProcessLookupError:
            return False
        except PermissionError:
            return True

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        # The session leader may have exited while descendants remain alive. Signal the
        # known process-group ID even when poll() already observed the leader's exit.
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError:
            try:
                process.terminate()
            except OSError:
                pass
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass
        except (ChildProcessError, OSError):
            pass

        if CliAgentAdapter._process_group_exists(process.pid):
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except OSError:
                try:
                    process.kill()
                except OSError:
                    pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                pass
            except (ChildProcessError, OSError):
                pass

            deadline = time.monotonic() + 2
            while (
                CliAgentAdapter._process_group_exists(process.pid)
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)

    @staticmethod
    def validate_execution(spec: AgentSpec, prompt: str, working_directory: Path) -> Path:
        """Validate everything that can fail safely before an action is persisted."""

        resolved_directory = Path(working_directory).expanduser().resolve()
        if not resolved_directory.is_dir():
            raise ValidationError("working_directory must be an existing directory")
        if "\x00" in prompt:
            raise ValidationError("prompt must not contain a null byte")
        if spec.prompt_transport == "argument" and len(prompt) > MAX_ARGUMENT_PROMPT_CHARACTERS:
            raise ValidationError(
                "argument prompt exceeds %d characters; use stdin transport"
                % MAX_ARGUMENT_PROMPT_CHARACTERS
            )
        return resolved_directory

    @staticmethod
    def _prepare_invocation(spec: AgentSpec, prompt: str) -> Tuple[list, Optional[bytes]]:
        argv = list(spec.command)
        if spec.prompt_transport == "argument":
            argv.append(prompt)
            return argv, None
        return argv, prompt.encode("utf-8")

    def execute(self, spec: AgentSpec, prompt: str, working_directory: Path) -> AgentExecutionResult:
        working_directory = self.validate_execution(spec, prompt, working_directory)
        argv, stdin_payload = self._prepare_invocation(spec, prompt)

        start = time.monotonic()
        with tempfile.TemporaryFile(mode="w+b") as stdout_file, tempfile.TemporaryFile(
            mode="w+b"
        ) as stderr_file:
            try:
                process = subprocess.Popen(
                    argv,
                    cwd=str(working_directory),
                    env=self._environment(spec),
                    stdin=subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                    stdout=stdout_file,
                    stderr=stderr_file,
                    start_new_session=True,
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                elapsed_ms = int((time.monotonic() - start) * 1000)
                return AgentExecutionResult(
                    status="failed",
                    return_code=None,
                    stdout="",
                    stderr="",
                    elapsed_ms=elapsed_ms,
                    started=False,
                    error="could not launch configured executable: %s" % type(exc).__name__,
                )

            try:
                process.communicate(input=stdin_payload, timeout=spec.timeout_seconds)
                timed_out = False
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process_group(process)
            except BaseException:
                # Includes KeyboardInterrupt/SystemExit: never let a detached provider
                # survive an interruption delivered to Relay.
                self._terminate_process_group(process)
                raise
            finally:
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass

            elapsed_ms = int((time.monotonic() - start) * 1000)
            stdout = self._read_tail(stdout_file)
            stderr = self._read_tail(stderr_file)
            if timed_out:
                status = "unknown"
                error = "agent exceeded its configured timeout"
            elif process.returncode == 0:
                status = "completed"
                error = None
            else:
                # A process may have changed files or invoked tools before returning non-zero.
                status = "unknown"
                error = "agent exited non-zero; external effects may have occurred"
            return AgentExecutionResult(
                status=status,
                return_code=process.returncode,
                stdout=stdout,
                stderr=stderr,
                elapsed_ms=elapsed_ms,
                started=True,
                timed_out=timed_out,
                error=error,
            )


class AntigravityCliAdapter(CliAgentAdapter):
    """Encodes checkpoints using Antigravity's documented JSON-lines protocol."""

    adapter_type = "antigravity-cli"

    @staticmethod
    def validate_execution(spec: AgentSpec, prompt: str, working_directory: Path) -> Path:
        resolved_directory = CliAgentAdapter.validate_execution(spec, prompt, working_directory)
        if spec.prompt_transport != "stdin":
            raise ValidationError("Antigravity CLI requires stdin prompt transport")
        return resolved_directory

    @staticmethod
    def _prepare_invocation(spec: AgentSpec, prompt: str) -> Tuple[list, Optional[bytes]]:
        message = {"event": "user", "message": {"content": prompt}}
        payload = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
        return list(spec.command), payload
