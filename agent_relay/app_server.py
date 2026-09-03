"""Documented stdio integration for the local Codex App Server."""

from __future__ import annotations

import json
import queue
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .adapters import (
    MAX_CAPTURED_OUTPUT_BYTES,
    AgentExecutionResult,
    CliAgentAdapter,
)
from .errors import ValidationError
from .models import AgentSpec
from .process_control import spawn_process
from .version import VERSION


MAX_PROTOCOL_LINE_BYTES = 2 * 1024 * 1024
MAX_PROTOCOL_EVENTS = 10_000
MAX_SESSION_ID_CHARACTERS = 512


class AppServerProtocolError(Exception):
    """A bounded, user-safe error raised for invalid app-server behavior."""


class _PipeReader:
    """Read a subprocess pipe with deadlines on both POSIX and Windows."""

    def __init__(self, stream: Any) -> None:
        self._stream = stream
        self._queue: queue.Queue = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        try:
            read = getattr(self._stream, "read1", self._stream.read)
            while True:
                chunk = read(64 * 1024)
                if not chunk:
                    self._queue.put(("eof", None))
                    return
                self._queue.put(("data", chunk))
        except (OSError, ValueError) as exc:
            self._queue.put(("error", exc))

    def read(self, deadline: float) -> bytes:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError
        try:
            kind, value = self._queue.get(timeout=remaining)
        except queue.Empty as exc:
            raise TimeoutError from exc
        if kind == "data":
            return value
        if kind == "error":
            raise AppServerProtocolError("could not read app-server output") from value
        return b""

    def join(self) -> None:
        self._thread.join(timeout=1)


class CodexAppServerAdapter:
    """Send one explicit checkpoint through Codex App Server's stdio protocol."""

    adapter_type = "codex-app-server"

    @staticmethod
    def validate_session_id(session_id: Optional[str]) -> Optional[str]:
        if session_id is None:
            return None
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValidationError("session_id must be a non-empty string")
        normalized = session_id.strip()
        if normalized != session_id:
            raise ValidationError("session_id must not have surrounding whitespace")
        if len(normalized) > MAX_SESSION_ID_CHARACTERS:
            raise ValidationError("session_id exceeds the maximum length")
        if any(character in normalized for character in ("\x00", "\r", "\n")):
            raise ValidationError("session_id contains an invalid character")
        return normalized

    @staticmethod
    def validate_execution(spec: AgentSpec, prompt: str, working_directory: Path) -> Path:
        resolved = CliAgentAdapter.validate_execution(spec, prompt, working_directory)
        if spec.prompt_transport != "stdin":
            raise ValidationError("Codex App Server requires stdin protocol transport")
        is_read_only = (
            spec.capabilities == ("repo-read",)
            and spec.permission_profile != "workspace-write"
        )
        is_workspace_write = (
            spec.capabilities == ("repo-read", "repo-write")
            and spec.permission_profile == "workspace-write"
        )
        if not (is_read_only or is_workspace_write):
            raise ValidationError("Codex App Server capability contract is invalid")
        return resolved

    @staticmethod
    def _execution_policy(
        spec: AgentSpec,
        working_directory: Path,
    ) -> Tuple[str, str, Dict[str, Any]]:
        restricted_reads = {
            "type": "restricted",
            "includePlatformDefaults": True,
            "readableRoots": [str(working_directory)],
        }
        if spec.permission_profile == "workspace-write":
            return (
                "on-request",
                "workspace-write",
                {
                    "type": "workspaceWrite",
                    "writableRoots": [str(working_directory)],
                    "readOnlyAccess": restricted_reads,
                    "networkAccess": False,
                    "excludeTmpdirEnvVar": True,
                    "excludeSlashTmp": True,
                },
            )
        return (
            "never",
            "read-only",
            {
                "type": "readOnly",
                "access": restricted_reads,
                "networkAccess": False,
            },
        )

    @staticmethod
    def _send(process: subprocess.Popen, message: Dict[str, Any]) -> None:
        if process.stdin is None:
            raise AppServerProtocolError("app-server stdin is unavailable")
        try:
            encoded = (json.dumps(message, ensure_ascii=False) + "\n").encode("utf-8")
            process.stdin.write(encoded)
            process.stdin.flush()
        except (BrokenPipeError, OSError, UnicodeError, ValueError) as exc:
            raise AppServerProtocolError("app-server closed its input") from exc

    @staticmethod
    def _record_event(event_types: List[str], method: str) -> None:
        if (
            0 < len(method) <= 128
            and all(character.isprintable() for character in method)
            and method not in event_types
            and len(event_types) < 128
        ):
            event_types.append(method)

    def _handle_server_request(
        self,
        process: subprocess.Popen,
        message: Dict[str, Any],
    ) -> None:
        request_id = message.get("id")
        method = message.get("method")
        if method in {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        }:
            self._send(process, {"id": request_id, "result": {"decision": "decline"}})
            return
        if method == "mcpServer/elicitation/request":
            self._send(process, {"id": request_id, "result": {"action": "cancel"}})
            return
        self._send(
            process,
            {
                "id": request_id,
                "error": {
                    "code": -32601,
                    "message": "Agent Relay does not authorize this server request",
                },
            },
        )

    def _read_message(
        self,
        reader: _PipeReader,
        deadline: float,
        buffer: bytearray,
    ) -> Dict[str, Any]:
        while True:
            newline = buffer.find(b"\n")
            if newline >= 0:
                line = bytes(buffer[:newline])
                del buffer[: newline + 1]
                if not line.strip():
                    continue
                try:
                    decoded = json.loads(line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as exc:
                    raise AppServerProtocolError("app-server emitted invalid JSONL") from exc
                if not isinstance(decoded, dict):
                    raise AppServerProtocolError("app-server message must be an object")
                return decoded

            chunk = reader.read(deadline)
            if not chunk:
                if buffer:
                    raise AppServerProtocolError("app-server ended with an incomplete JSONL message")
                raise AppServerProtocolError("app-server exited before completing the turn")
            buffer.extend(chunk)
            if len(buffer) > MAX_PROTOCOL_LINE_BYTES:
                raise AppServerProtocolError("app-server message exceeded the size limit")

    def _receive_until(
        self,
        process: subprocess.Popen,
        reader: _PipeReader,
        deadline: float,
        buffer: bytearray,
        event_types: List[str],
        response_id: Optional[int] = None,
        terminal_turn_id: Optional[str] = None,
        message_output: Optional[bytearray] = None,
    ) -> Dict[str, Any]:
        for _ in range(MAX_PROTOCOL_EVENTS):
            message = self._read_message(reader, deadline, buffer)
            method = message.get("method")
            if isinstance(method, str):
                self._record_event(event_types, method)
                if "id" in message:
                    self._handle_server_request(process, message)
                    continue
                params = message.get("params")
                if isinstance(params, dict):
                    if method == "item/completed" and message_output is not None:
                        self._capture_agent_message(params.get("item"), message_output)
                    if method == "turn/completed":
                        turn = params.get("turn")
                        if isinstance(turn, dict) and turn.get("id") == terminal_turn_id:
                            return message
                continue

            if response_id is not None and message.get("id") == response_id:
                if "error" in message:
                    raise AppServerProtocolError("app-server rejected %s" % response_id)
                if "result" not in message:
                    raise AppServerProtocolError("app-server response has no result")
                return message
        raise AppServerProtocolError("app-server event count exceeded the limit")

    @staticmethod
    def _capture_agent_message(item: Any, message_output: bytearray) -> None:
        if not isinstance(item, dict) or item.get("type") != "agentMessage":
            return
        text = item.get("text")
        if isinstance(text, str) and text:
            if message_output:
                message_output.extend(b"\n")
            message_output.extend(text.encode("utf-8", errors="replace"))
            if len(message_output) > MAX_CAPTURED_OUTPUT_BYTES:
                del message_output[:-MAX_CAPTURED_OUTPUT_BYTES]

    @staticmethod
    def _identifier(container: Any, name: str) -> str:
        if not isinstance(container, dict):
            raise AppServerProtocolError("app-server %s response is invalid" % name)
        value = container.get("id")
        if not isinstance(value, str) or not value or len(value) > MAX_SESSION_ID_CHARACTERS:
            raise AppServerProtocolError("app-server returned an invalid %s id" % name)
        if any(character in value for character in ("\x00", "\r", "\n")):
            raise AppServerProtocolError("app-server returned an invalid %s id" % name)
        return value

    @staticmethod
    def _bounded_output(message_output: bytearray) -> str:
        return bytes(message_output).decode("utf-8", errors="replace")

    def execute(self, spec: AgentSpec, prompt: str, working_directory: Path) -> AgentExecutionResult:
        return self.execute_session(spec, prompt, working_directory, None)

    def execute_session(
        self,
        spec: AgentSpec,
        prompt: str,
        working_directory: Path,
        session_id: Optional[str],
    ) -> AgentExecutionResult:
        working_directory = self.validate_execution(spec, prompt, working_directory)
        session_id = self.validate_session_id(session_id)
        start = time.monotonic()
        event_types: List[str] = []
        thread_id = session_id
        turn_id = None
        protocol_status = None
        approval_policy, sandbox_mode, turn_sandbox_policy = self._execution_policy(
            spec,
            working_directory,
        )

        with tempfile.TemporaryFile(mode="w+b") as stderr_file:
            try:
                process = spawn_process(
                    list(spec.command),
                    working_directory,
                    CliAgentAdapter._environment(spec),
                    subprocess.PIPE,
                    subprocess.PIPE,
                    stderr_file,
                )
            except (FileNotFoundError, PermissionError, OSError) as exc:
                return AgentExecutionResult(
                    status="failed",
                    return_code=None,
                    stdout="",
                    stderr="",
                    elapsed_ms=int((time.monotonic() - start) * 1000),
                    started=False,
                    error="could not launch configured executable: %s" % type(exc).__name__,
                )

            if process.stdout is None:
                CliAgentAdapter._terminate_process_group(process)
                raise AppServerProtocolError("app-server stdout is unavailable")
            reader = _PipeReader(process.stdout)
            deadline = start + spec.timeout_seconds
            buffer = bytearray()
            message_output = bytearray()
            timed_out = False
            protocol_error = None
            try:
                self._send(
                    process,
                    {
                        "id": 1,
                        "method": "initialize",
                        "params": {
                            "clientInfo": {
                                "name": "agent_relay",
                                "title": "Agent Relay",
                                "version": VERSION,
                            }
                        },
                    },
                )
                self._receive_until(
                    process,
                    reader,
                    deadline,
                    buffer,
                    event_types,
                    response_id=1,
                )
                self._send(process, {"method": "initialized"})

                request_id = 2
                if thread_id is None:
                    method = "thread/start"
                    params = {
                        "cwd": str(working_directory),
                        "approvalPolicy": approval_policy,
                        "approvalsReviewer": "user",
                        "sandbox": sandbox_mode,
                        "serviceName": "agent_relay",
                    }
                else:
                    method = "thread/resume"
                    params = {
                        "threadId": thread_id,
                        "cwd": str(working_directory),
                        "approvalPolicy": approval_policy,
                        "approvalsReviewer": "user",
                        "sandbox": sandbox_mode,
                    }
                self._send(process, {"id": request_id, "method": method, "params": params})
                response = self._receive_until(
                    process,
                    reader,
                    deadline,
                    buffer,
                    event_types,
                    response_id=request_id,
                )
                result = response.get("result")
                thread_id = self._identifier(
                    result.get("thread") if isinstance(result, dict) else None,
                    "thread",
                )

                self._send(
                    process,
                    {
                        "id": 3,
                        "method": "turn/start",
                        "params": {
                            "threadId": thread_id,
                            "input": [{"type": "text", "text": prompt}],
                            "cwd": str(working_directory),
                            "approvalPolicy": approval_policy,
                            "approvalsReviewer": "user",
                            "sandboxPolicy": turn_sandbox_policy,
                        },
                    },
                )
                response = self._receive_until(
                    process,
                    reader,
                    deadline,
                    buffer,
                    event_types,
                    response_id=3,
                    message_output=message_output,
                )
                result = response.get("result")
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = self._identifier(turn, "turn")
                completed = self._receive_until(
                    process,
                    reader,
                    deadline,
                    buffer,
                    event_types,
                    terminal_turn_id=turn_id,
                    message_output=message_output,
                )
                params = completed.get("params")
                completed_turn = params.get("turn") if isinstance(params, dict) else None
                if isinstance(completed_turn, dict):
                    if not message_output:
                        for item in completed_turn.get("items", []):
                            self._capture_agent_message(item, message_output)
                    status_value = completed_turn.get("status")
                    if isinstance(status_value, str) and len(status_value) <= 64:
                        protocol_status = status_value
                if protocol_status is None and isinstance(params, dict):
                    status_value = params.get("status")
                    if isinstance(status_value, str) and len(status_value) <= 64:
                        protocol_status = status_value
                if protocol_status != "completed":
                    raise AppServerProtocolError("Codex turn did not complete successfully")
            except TimeoutError:
                timed_out = True
                protocol_error = "app-server turn exceeded its configured timeout"
            except AppServerProtocolError as exc:
                protocol_error = str(exc)
            finally:
                CliAgentAdapter._terminate_process_group(process)
                if process.stdin is not None:
                    try:
                        process.stdin.close()
                    except (BrokenPipeError, OSError):
                        pass
                if process.stdout is not None:
                    process.stdout.close()
                reader.join()

            elapsed_ms = int((time.monotonic() - start) * 1000)
            stderr = CliAgentAdapter._read_tail(stderr_file)
            status = "completed" if protocol_error is None else "unknown"
            return AgentExecutionResult(
                status=status,
                # App Server is a long-lived transport that Relay stops after the turn;
                # its teardown signal is not the Codex turn's return code.
                return_code=None,
                stdout=self._bounded_output(message_output),
                stderr=stderr,
                elapsed_ms=elapsed_ms,
                started=True,
                timed_out=timed_out,
                error=protocol_error,
                session_id=thread_id,
                turn_id=turn_id,
                protocol_status=protocol_status,
                event_types=tuple(event_types),
            )
