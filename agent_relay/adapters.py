"""Agent runtime adapters for the local-first MVP."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Optional, Protocol, Tuple, runtime_checkable

from .errors import ConflictError, NotFoundError, ValidationError
from .models import AgentSpec
from .path_security import open_regular_read_only
from .process_control import (
    release_process_tree,
    spawn_process,
    terminate_process_tree,
    validate_executable,
)


BASE_ENVIRONMENT_NAMES = (
    "APPDATA",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "SYSTEMROOT",
    "TEMP",
    "TERM",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
)
MAX_CAPTURED_OUTPUT_BYTES = 64 * 1024
MAX_ARGUMENT_PROMPT_CHARACTERS = 32_000
MAX_COPILOT_AUTH_STATE_BYTES = 2 * 1024 * 1024
MAX_COPILOT_AUTH_ACCOUNTS = 64
MAX_COPILOT_AUTH_IDENTITY_CHARACTERS = 1_024
MAX_COPILOT_AUTH_TOKEN_CHARACTERS = 32_768

COPILOT_CONTAINMENT_ARGUMENTS = (
    "-s",
    "--available-tools=view,glob,grep",
    "--deny-tool=write,create,edit,shell,powershell,url,memory,task,web_fetch",
    "--disable-builtin-mcps",
    "--no-custom-instructions",
    "--experimental",
    "--sandbox",
    "--no-remote",
    "--no-remote-export",
    "--disallow-temp-dir",
    "--no-ask-user",
    "--no-auto-update",
    "--no-bash-env",
    "--no-color",
    "--log-level=none",
    "--output-format=json",
)


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
        if os.name == "nt":
            return False
        from .process_control import _process_group_exists

        return _process_group_exists(process_group_id)

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen) -> None:
        terminate_process_tree(process)

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
        validate_executable(
            spec.command,
            resolved_directory,
            CliAgentAdapter._environment(spec),
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
                process = spawn_process(
                    argv,
                    working_directory,
                    self._environment(spec),
                    subprocess.PIPE if stdin_payload is not None else subprocess.DEVNULL,
                    stdout_file,
                    stderr_file,
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

            release_process_tree(process)

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


class CopilotCliAdapter(CliAgentAdapter):
    """Run Copilot with ephemeral configuration and a fail-closed read sandbox."""

    adapter_type = "copilot-cli"

    @staticmethod
    def validate_execution(spec: AgentSpec, prompt: str, working_directory: Path) -> Path:
        resolved = CliAgentAdapter.validate_execution(spec, prompt, working_directory)
        if spec.prompt_transport != "stdin":
            raise ValidationError("GitHub Copilot CLI requires stdin prompt transport")
        if spec.provider_id != "github-copilot":
            raise ValidationError("Copilot adapter requires the reviewed provider preset")
        if spec.command[1:] != COPILOT_CONTAINMENT_ARGUMENTS:
            raise ValidationError("Copilot containment arguments do not match the reviewed preset")
        if spec.capabilities != ("repo-read",):
            raise ValidationError("Copilot containment requires repo-read capability only")
        if spec.permission_profile != "sandbox-read-contained-preview":
            raise ValidationError("Copilot containment requires its reviewed permission profile")
        if spec.env_allowlist:
            raise ValidationError("Copilot containment does not allow inherited environment values")
        if spec.config_home is not None and spec.config_home[0] != "COPILOT_HOME":
            raise ValidationError("Copilot source configuration must use COPILOT_HOME")
        return resolved

    @staticmethod
    def _source_config_home(spec: AgentSpec) -> Optional[Path]:
        if spec.config_home is not None:
            return Path(spec.config_home[1])
        home = os.environ.get("HOME") or os.environ.get("USERPROFILE")
        if not home:
            return None
        return Path(home) / ".copilot"

    @staticmethod
    def _strip_jsonc_comments(value: str) -> str:
        output = []
        index = 0
        in_string = False
        escaped = False
        while index < len(value):
            character = value[index]
            if in_string:
                output.append(character)
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                index += 1
                continue
            if character == '"':
                in_string = True
                output.append(character)
                index += 1
                continue
            if character == "/" and index + 1 < len(value):
                following = value[index + 1]
                if following == "/":
                    index += 2
                    while index < len(value) and value[index] not in "\r\n":
                        index += 1
                    continue
                if following == "*":
                    index += 2
                    while index + 1 < len(value):
                        if value[index] == "*" and value[index + 1] == "/":
                            index += 2
                            break
                        index += 1
                    continue
            output.append(character)
            index += 1
        return "".join(output)

    @staticmethod
    def _auth_identity(value: object) -> Optional[Dict[str, str]]:
        if not isinstance(value, dict):
            return None
        identity = {}
        for key in ("host", "login"):
            item = value.get(key)
            if not isinstance(item, str) or not item:
                return None
            if len(item) > MAX_COPILOT_AUTH_IDENTITY_CHARACTERS:
                return None
            identity[key] = item
        return identity

    @classmethod
    def _sanitized_auth_state(cls, payload: bytes) -> Optional[bytes]:
        try:
            decoded = payload.decode("utf-8")
            parsed = json.loads(cls._strip_jsonc_comments(decoded))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, dict):
            return None

        sanitized: Dict[str, object] = {}
        last_user = cls._auth_identity(parsed.get("lastLoggedInUser"))
        if last_user is not None:
            sanitized["lastLoggedInUser"] = last_user

        logged_in_users = parsed.get("loggedInUsers")
        if isinstance(logged_in_users, list):
            identities = []
            for value in logged_in_users[:MAX_COPILOT_AUTH_ACCOUNTS]:
                identity = cls._auth_identity(value)
                if identity is not None:
                    identities.append(identity)
            if identities:
                sanitized["loggedInUsers"] = identities

        tokens = parsed.get("copilotTokens")
        if isinstance(tokens, dict):
            safe_tokens = {}
            for key, value in list(tokens.items())[:MAX_COPILOT_AUTH_ACCOUNTS]:
                if (
                    isinstance(key, str)
                    and isinstance(value, str)
                    and key
                    and value
                    and len(key) <= MAX_COPILOT_AUTH_IDENTITY_CHARACTERS
                    and len(value) <= MAX_COPILOT_AUTH_TOKEN_CHARACTERS
                ):
                    safe_tokens[key] = value
            if safe_tokens:
                sanitized["copilotTokens"] = safe_tokens

        if not sanitized:
            return None
        return json.dumps(sanitized, sort_keys=True).encode("utf-8")

    @classmethod
    def _copy_auth_state(cls, spec: AgentSpec, isolated_home: Path) -> None:
        source_home = CopilotCliAdapter._source_config_home(spec)
        if source_home is None:
            return
        source = source_home / "config.json"
        descriptor = open_regular_read_only(source)
        if descriptor is None:
            return
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                return
            if metadata.st_size > MAX_COPILOT_AUTH_STATE_BYTES:
                return
            chunks = []
            remaining = MAX_COPILOT_AUTH_STATE_BYTES + 1
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            payload = b"".join(chunks)
            if len(payload) > MAX_COPILOT_AUTH_STATE_BYTES:
                return
        finally:
            os.close(descriptor)

        payload = cls._sanitized_auth_state(payload)
        if payload is None:
            return
        target = isolated_home / "config.json"
        target.write_bytes(payload)
        target.chmod(0o600)

    @staticmethod
    def _settings(working_directory: Path) -> Dict[str, object]:
        return {
            "disableAllHooks": True,
            "experimental": True,
            "remote": "off",
            "remoteExport": False,
            "permissions": {"disableBypassPermissionsMode": "disable"},
            "sandbox": {
                "enabled": True,
                "failIfUnavailable": True,
                "allowBypass": False,
                "addCurrentWorkingDirectory": False,
                "allowDevToolAccess": False,
                "sandboxMcpServers": True,
                "sandboxLspServers": True,
                "auth": {"git": False, "gh": False},
                "userPolicy": {
                    "filesystem": {
                        "readwritePaths": [],
                        "readonlyPaths": [str(working_directory)],
                        "deniedPaths": [],
                        "clearPolicyOnExit": True,
                    },
                    "network": {
                        "allowOutbound": False,
                        "allowLocalNetwork": False,
                    },
                    "seatbelt": {"keychainAccess": False},
                },
            },
        }

    @staticmethod
    def _environment(spec: AgentSpec) -> Dict[str, str]:
        environment = CliAgentAdapter._environment(spec)
        if spec.config_home is None:
            raise ValidationError("Copilot containment requires an isolated configuration home")
        isolated_home = Path(spec.config_home[1])
        environment.update(
            {
                "COPILOT_ALLOW_ALL": "false",
                "COPILOT_AUTO_UPDATE": "false",
                "COPILOT_CACHE_HOME": str(isolated_home / "cache"),
                "GITHUB_COPILOT_PROMPT_MODE_EXTENSIONS": "false",
                "GITHUB_COPILOT_PROMPT_MODE_REPO_HOOKS": "false",
                "GITHUB_COPILOT_PROMPT_MODE_WORKSPACE_MCP": "false",
                "PLUGINS_DASHBOARD": "false",
            }
        )
        return environment

    def execute(self, spec: AgentSpec, prompt: str, working_directory: Path) -> AgentExecutionResult:
        working_directory = self.validate_execution(spec, prompt, working_directory)
        try:
            with tempfile.TemporaryDirectory(prefix="agent-relay-copilot-") as temporary:
                isolated_home = Path(temporary)
                isolated_home.chmod(0o700)
                cache_home = isolated_home / "cache"
                cache_home.mkdir(mode=0o700)
                self._copy_auth_state(spec, isolated_home)
                settings_path = isolated_home / "settings.json"
                settings_path.write_text(
                    json.dumps(self._settings(working_directory), sort_keys=True),
                    encoding="utf-8",
                )
                settings_path.chmod(0o600)
                isolated_spec = AgentSpec(
                    agent_id=spec.agent_id,
                    display_name=spec.display_name,
                    command=spec.command,
                    prompt_transport=spec.prompt_transport,
                    timeout_seconds=spec.timeout_seconds,
                    capabilities=spec.capabilities,
                    env_allowlist=spec.env_allowlist,
                    adapter_type=spec.adapter_type,
                    config_home=("COPILOT_HOME", str(isolated_home)),
                    provider_id=spec.provider_id,
                    permission_profile=spec.permission_profile,
                )
                return super().execute(
                    isolated_spec,
                    prompt,
                    working_directory,
                )
        except OSError as exc:
            return AgentExecutionResult(
                status="failed",
                return_code=None,
                stdout="",
                stderr="",
                elapsed_ms=0,
                started=False,
                error="could not prepare isolated Copilot configuration: %s"
                % type(exc).__name__,
            )
