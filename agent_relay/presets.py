"""Maintained agent presets built on top of the generic adapter contract."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .adapters import COPILOT_CONTAINMENT_ARGUMENTS
from .errors import NotFoundError, ValidationError
from .models import AgentSpec


@dataclass(frozen=True)
class AgentPreset:
    """A reviewed invocation template for one supported agent runtime."""

    preset_id: str
    default_agent_id: str
    display_name: str
    executable_name: str
    fixed_arguments: Tuple[str, ...]
    prompt_transport: str
    capabilities: Tuple[str, ...]
    permission_profile: str
    adapter_type: str = "cli"
    config_home_environment: Optional[str] = None
    minimum_version: Optional[str] = None


CHECKPOINT_STDIN_INSTRUCTION = (
    "Continue the task using the Agent Relay checkpoint provided on stdin. "
    "Analyze the repository and return the next response without modifying files."
)

WORKSPACE_WRITE_CHECKPOINT_STDIN_INSTRUCTION = (
    "Continue the task using the Agent Relay checkpoint provided on stdin. "
    "Make only the requested repository changes inside the current workspace and "
    "return the next response."
)


# Command sandbox network controls do not contain Codex-hosted tools such as web
# search, MCP servers, apps, browser/computer use, or hooks. Keep those surfaces
# off independently for every reviewed Codex preset. Explicit command-line
# overrides take precedence over a user's normal Codex configuration.
CODEX_CONTAINMENT_ARGUMENTS = (
    "-c",
    'web_search="disabled"',
    "-c",
    "mcp_servers={}",
    "--disable",
    "apps",
    "--disable",
    "plugins",
    "--disable",
    "remote_plugin",
    "--disable",
    "hooks",
    "--disable",
    "browser_use",
    "--disable",
    "browser_use_external",
    "--disable",
    "browser_use_full_cdp_access",
    "--disable",
    "computer_use",
    "--disable",
    "in_app_browser",
    "--disable",
    "image_generation",
    "--disable",
    "skill_mcp_dependency_install",
    "--disable",
    "tool_suggest",
)

CODEX_READ_PERMISSION_ARGUMENTS = (
    "-c",
    'default_permissions="agent-relay-read"',
    "-c",
    'permissions.agent-relay-read={filesystem={":minimal"="read",'
    '":workspace_roots"={"."="read"}},network={enabled=false}}',
)

CODEX_WRITE_PERMISSION_ARGUMENTS = (
    "-c",
    'default_permissions="agent-relay-write"',
    "-c",
    'permissions.agent-relay-write={extends=":workspace",filesystem={'
    '":root"="deny",":minimal"="read",":tmpdir"="deny",'
    '":slash_tmp"="deny",":workspace_roots"={"."="write"}},'
    "network={enabled=false}}",
)


PRESETS = {
    "antigravity-cli": AgentPreset(
        preset_id="antigravity-cli",
        default_agent_id="antigravity-cli",
        display_name="Google Antigravity CLI",
        executable_name="agy",
        fixed_arguments=(
            "--mode=plan",
            "--input-format",
            "stream-json",
            "--output-format",
            "stream-json",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="manual-plan-uncontained",
        adapter_type="antigravity-cli",
    ),
    "claude-code": AgentPreset(
        preset_id="claude-code",
        default_agent_id="claude-code",
        display_name="Anthropic Claude Code",
        executable_name="claude",
        fixed_arguments=(
            "-p",
            CHECKPOINT_STDIN_INSTRUCTION,
            "--safe-mode",
            "--restricted",
            "--permission-mode",
            "plan",
            "--tools",
            "Read,Glob,Grep",
            "--disallowedTools",
            "mcp__*",
            "--no-chrome",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format",
            "json",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="plan-read-only",
        config_home_environment="CLAUDE_CONFIG_DIR",
        minimum_version="2.1.248",
    ),
    "claude-code-write": AgentPreset(
        preset_id="claude-code-write",
        default_agent_id="claude-code-write",
        display_name="Anthropic Claude Code (workspace write)",
        executable_name="claude",
        fixed_arguments=(
            "-p",
            WORKSPACE_WRITE_CHECKPOINT_STDIN_INSTRUCTION,
            "--safe-mode",
            "--restricted",
            "--permission-mode",
            "acceptEdits",
            "--tools",
            "Read,Edit,Write,Glob,Grep",
            "--disallowedTools",
            "mcp__*",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--output-format",
            "json",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read", "repo-write"),
        permission_profile="workspace-write",
        config_home_environment="CLAUDE_CONFIG_DIR",
        minimum_version="2.1.248",
    ),
    "codex-cli": AgentPreset(
        preset_id="codex-cli",
        default_agent_id="codex-cli",
        display_name="OpenAI Codex CLI",
        executable_name="codex",
        fixed_arguments=CODEX_CONTAINMENT_ARGUMENTS
        + CODEX_READ_PERMISSION_ARGUMENTS
        + (
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--color",
            "never",
            "-",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="sandbox-read-only",
        config_home_environment="CODEX_HOME",
    ),
    "codex-cli-write": AgentPreset(
        preset_id="codex-cli-write",
        default_agent_id="codex-cli-write",
        display_name="OpenAI Codex CLI (workspace write)",
        executable_name="codex",
        fixed_arguments=(
            "--ask-for-approval",
            "on-request",
        )
        + CODEX_CONTAINMENT_ARGUMENTS
        + CODEX_WRITE_PERMISSION_ARGUMENTS
        + (
            "exec",
            "--ignore-user-config",
            "--ignore-rules",
            "--ephemeral",
            "--color",
            "never",
            "-",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read", "repo-write"),
        permission_profile="workspace-write",
        config_home_environment="CODEX_HOME",
    ),
    "codex-app-server": AgentPreset(
        preset_id="codex-app-server",
        default_agent_id="codex-app",
        display_name="OpenAI Codex App Server",
        executable_name="codex",
        fixed_arguments=CODEX_CONTAINMENT_ARGUMENTS
        + ("app-server", "--listen", "stdio://"),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="app-server-read-only",
        adapter_type="codex-app-server",
        config_home_environment="CODEX_HOME",
    ),
    "codex-app-server-write": AgentPreset(
        preset_id="codex-app-server-write",
        default_agent_id="codex-app-write",
        display_name="OpenAI Codex App Server (workspace write)",
        executable_name="codex",
        fixed_arguments=CODEX_CONTAINMENT_ARGUMENTS
        + ("app-server", "--listen", "stdio://"),
        prompt_transport="stdin",
        capabilities=("repo-read", "repo-write"),
        permission_profile="workspace-write",
        adapter_type="codex-app-server",
        config_home_environment="CODEX_HOME",
    ),
    "gemini-cli": AgentPreset(
        preset_id="gemini-cli",
        default_agent_id="gemini-cli",
        display_name="Google Gemini CLI",
        executable_name="gemini",
        fixed_arguments=(
            "--approval-mode",
            "plan",
            "--output-format",
            "json",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="manual-plan-uncontained",
    ),
    "github-copilot": AgentPreset(
        preset_id="github-copilot",
        default_agent_id="github-copilot",
        display_name="GitHub Copilot CLI",
        executable_name="copilot",
        fixed_arguments=COPILOT_CONTAINMENT_ARGUMENTS,
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="sandbox-read-contained-preview",
        adapter_type="copilot-cli",
        config_home_environment="COPILOT_HOME",
        minimum_version="1.0.79",
    )
}


def _resolve_executable(name: str, explicit: Optional[str] = None) -> Optional[str]:
    candidate = explicit or name
    if os.sep in candidate or (os.altsep is not None and os.altsep in candidate):
        path = Path(candidate).expanduser().resolve()
        if path.is_file() and os.access(str(path), os.X_OK):
            return str(path)
        return None
    return shutil.which(candidate)


def list_preset_statuses() -> Tuple[Dict[str, Any], ...]:
    statuses = []
    for preset_id, preset in sorted(PRESETS.items()):
        executable = _resolve_executable(preset.executable_name)
        statuses.append(
            {
                "preset_id": preset_id,
                "display_name": preset.display_name,
                "available": executable is not None,
                "executable": executable,
                "adapter_type": preset.adapter_type,
                "capabilities": list(preset.capabilities),
                "permission_profile": preset.permission_profile,
                "minimum_version": preset.minimum_version,
            }
        )
    return tuple(statuses)


def build_preset(
    preset_id: str,
    agent_id: Optional[str] = None,
    executable: Optional[str] = None,
    timeout_seconds: int = 900,
    config_home: Optional[str] = None,
) -> AgentSpec:
    preset = PRESETS.get(preset_id)
    if preset is None:
        raise NotFoundError("agent preset not found: %s" % preset_id)
    resolved = _resolve_executable(preset.executable_name, explicit=executable)
    if resolved is None:
        raise ValidationError(
            "%s executable was not found; install and authenticate it using the official "
            "provider instructions, then retry" % preset.executable_name
        )
    resolved_config_home = None
    if config_home is not None:
        if preset.config_home_environment is None:
            raise ValidationError("%s does not support an isolated config home" % preset_id)
        candidate = Path(config_home).expanduser()
        if not candidate.is_absolute():
            raise ValidationError("config_home must be an absolute path")
        resolved_config_home = (
            preset.config_home_environment,
            str(candidate.resolve()),
        )
    return AgentSpec(
        agent_id=agent_id or preset.default_agent_id,
        display_name=preset.display_name,
        command=tuple([resolved] + list(preset.fixed_arguments)),
        adapter_type=preset.adapter_type,
        prompt_transport=preset.prompt_transport,
        timeout_seconds=timeout_seconds,
        capabilities=preset.capabilities,
        config_home=resolved_config_home,
        provider_id=preset.preset_id,
        permission_profile=preset.permission_profile,
    )
