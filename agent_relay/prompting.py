"""Portable, explicit checkpoint rendering for a target agent."""

from __future__ import annotations

import json

from .errors import ValidationError
from .models import TaskCheckpoint


MAX_PROMPT_CHARACTERS = 200_000


class CheckpointPromptRenderer:
    """Renders facts and action state without pretending to transfer hidden reasoning."""

    def render(self, checkpoint: TaskCheckpoint, target_agent: str) -> str:
        payload = {
            "checkpoint": checkpoint.to_dict(),
            "handoff": {
                "target_agent": target_agent,
                "rules": [
                    "Inspect the current workspace before changing files.",
                    "Treat pending or unknown actions as unresolved and never repeat them automatically.",
                    "Preserve the recorded constraints and call out unsupported capabilities.",
                    "Continue from the recorded next steps and report any conflict with the workspace.",
                ],
            },
        }
        serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        prompt = (
            "You are continuing an existing task through Agent Relay. The JSON below is an "
            "auditable checkpoint, not hidden chain-of-thought. Use it as task state.\n\n"
            + serialized
        )
        if len(prompt) > MAX_PROMPT_CHARACTERS:
            raise ValidationError("rendered checkpoint exceeds the maximum prompt size")
        return prompt
