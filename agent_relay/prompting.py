"""Portable, explicit checkpoint rendering for a target agent."""

from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .errors import ValidationError
from .models import TaskCheckpoint
from .results import result_contract


MAX_PROMPT_CHARACTERS = 200_000


class CheckpointPromptRenderer:
    """Renders facts and action state without pretending to transfer hidden reasoning."""

    def render(
        self,
        checkpoint: TaskCheckpoint,
        target_agent: str,
        workspace_policy: Optional[Dict[str, Any]] = None,
    ) -> str:
        pending_action = next(
            (
                action
                for action in reversed(checkpoint.actions)
                if action.agent_id == target_agent
                and action.kind
                in {
                    "route-run",
                    "session-turn",
                    "workspace-write",
                    "session-workspace-write",
                }
                and action.status == "pending"
            ),
            None,
        )
        checkpoint_value = checkpoint.to_dict()
        for action_value in checkpoint_value["actions"]:
            details = action_value["details"]
            if "result_proposal" in details:
                details.pop("result_proposal")
                details["result_proposal_redacted"] = True
        payload = {
            "checkpoint": checkpoint_value,
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
        if pending_action is not None:
            payload["handoff"]["result_contract"] = result_contract(
                checkpoint.task_id,
                pending_action.action_id,
            )
        if workspace_policy is not None:
            payload["handoff"]["workspace_policy"] = dict(workspace_policy)
            payload["handoff"]["rules"].extend(
                [
                    "Modify files only inside workspace_policy.workspace_root.",
                    "Do not change Git history or repository metadata.",
                    "Do not use network access; ask the user if external access is required.",
                    "A post-run snapshot and explicit user review are required before more execution.",
                ]
            )
        serialized = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False)
        prompt = (
            "You are continuing an existing task through Agent Relay. The JSON below is an "
            "auditable checkpoint, not hidden chain-of-thought. Use it as task state.\n\n"
            + serialized
        )
        if len(prompt) > MAX_PROMPT_CHARACTERS:
            raise ValidationError("rendered checkpoint exceeds the maximum prompt size")
        return prompt
