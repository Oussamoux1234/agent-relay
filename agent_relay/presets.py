"""Maintained agent presets built on top of the generic adapter contract."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

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


CHECKPOINT_STDIN_INSTRUCTION = (
    "Continue the task using the Agent Relay checkpoint provided on stdin. "
    "Analyze the repository and return the next response without modifying files."
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
        permission_profile="plan-read-only",
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
            "--permission-mode",
            "plan",
            "--output-format",
            "json",
        ),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="plan-read-only",
        config_home_environment="CLAUDE_CONFIG_DIR",
    ),
    "codex-cli": AgentPreset(
        preset_id="codex-cli",
        default_agent_id="codex-cli",
        display_name="OpenAI Codex CLI",
        executable_name="codex",
        fixed_arguments=(
            "exec",
            "--sandbox",
            "read-only",
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
        permission_profile="plan-read-only",
    ),
    "github-copilot": AgentPreset(
        preset_id="github-copilot",
        default_agent_id="github-copilot",
        display_name="GitHub Copilot CLI",
        executable_name="copilot",
        fixed_arguments=("-s", "--available-tools=read", "--disable-builtin-mcps"),
        prompt_transport="stdin",
        capabilities=("repo-read",),
        permission_profile="read-only",
    )
}


def _resolve_executable(name: str, explicit: Optional[str] = None) -> Optional[str]:
    candidate = explicit or name
    if os.sep in candidate:
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
    )
