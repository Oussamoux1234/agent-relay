"""Portable, explicit checkpoint rendering for a target agent."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .errors import ValidationError
from .models import ActionRecord, TaskCheckpoint
from .results import result_contract


MAX_PROMPT_CHARACTERS = 200_000
MAX_RENDERED_ACTIONS = 16
MAX_ACTION_DETAILS_CHARACTERS = 4_000
MAX_DETAIL_COLLECTION_ITEMS = 16
MAX_DETAIL_DEPTH = 4
MAX_DETAIL_STRING_CHARACTERS = 512
MAX_WORKSPACE_PATH_SAMPLE = 4
MAX_WORKSPACE_PATH_CHARACTERS = 128
WORKSPACE_PATH_FIELDS = (
    "preexisting_paths",
    "introduced_paths",
    "modified_paths",
    "removed_paths",
    "final_dirty_paths",
)


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


class CheckpointPromptRenderer:
    """Renders facts and action state without pretending to transfer hidden reasoning."""

    @staticmethod
    def _bounded_string(value: str, limit: int = MAX_DETAIL_STRING_CHARACTERS) -> Any:
        if len(value) <= limit:
            return value
        return {
            "prefix": value[:limit],
            "original_characters": len(value),
            "sha256": hashlib.sha256(value.encode("utf-8")).hexdigest(),
            "truncated": True,
        }

    @classmethod
    def _bounded_value(cls, value: Any, depth: int = 0) -> Any:
        if isinstance(value, str):
            return cls._bounded_string(value)
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if depth >= MAX_DETAIL_DEPTH:
            return {
                "sha256": _digest(value),
                "truncated": True,
                "reason": "maximum-detail-depth",
            }
        if isinstance(value, list):
            items = [
                cls._bounded_value(item, depth + 1)
                for item in value[:MAX_DETAIL_COLLECTION_ITEMS]
            ]
            if len(value) <= MAX_DETAIL_COLLECTION_ITEMS:
                return items
            return {
                "items": items,
                "total_count": len(value),
                "omitted_count": len(value) - len(items),
                "sha256": _digest(value),
                "truncated": True,
            }
        if isinstance(value, dict):
            keys = sorted(value)
            included = keys[:MAX_DETAIL_COLLECTION_ITEMS]
            projected = {
                key: cls._bounded_value(value[key], depth + 1) for key in included
            }
            if len(keys) > len(included):
                projected["_projection"] = {
                    "total_key_count": len(keys),
                    "omitted_key_count": len(keys) - len(included),
                    "sha256": _digest(value),
                    "truncated": True,
                }
            return projected
        return cls._bounded_string(str(value))

    @classmethod
    def _path_sample(cls, values_by_field: Iterable[Tuple[str, Any]]) -> Dict[str, Any]:
        counts: Dict[str, int] = {}
        candidates = []
        for field_name, raw_values in values_by_field:
            values = raw_values if isinstance(raw_values, list) else []
            counts[field_name] = len(values)
            for value in values:
                if isinstance(value, str):
                    candidates.append((field_name, value))
        candidates.sort()
        sample = [
            {
                "category": field_name,
                "path": cls._bounded_string(value, MAX_WORKSPACE_PATH_CHARACTERS),
            }
            for field_name, value in candidates[:MAX_WORKSPACE_PATH_SAMPLE]
        ]
        return {
            "path_counts": counts,
            "path_entry_count": sum(counts.values()),
            "path_sample": sample,
            "path_sample_count": len(sample),
            "path_sample_truncated": len(candidates) > len(sample),
        }

    @classmethod
    def _workspace_review_projection(
        cls,
        value: Any,
        task_id: str,
        action_id: str,
    ) -> Any:
        if not isinstance(value, dict):
            return cls._bounded_value(value)
        projected = {
            "status": cls._bounded_value(value.get("status")),
            "workspace_root": cls._bounded_string(value.get("workspace_root", ""), 256)
            if isinstance(value.get("workspace_root"), str)
            else cls._bounded_value(value.get("workspace_root")),
            "before_digest": cls._bounded_value(value.get("before_digest")),
            "after_digest": cls._bounded_value(value.get("after_digest")),
            "before_head": cls._bounded_value(value.get("before_head")),
            "after_head": cls._bounded_value(value.get("after_head")),
            "before_branch": cls._bounded_string(value.get("before_branch", ""), 128)
            if isinstance(value.get("before_branch"), str)
            else cls._bounded_value(value.get("before_branch")),
            "after_branch": cls._bounded_string(value.get("after_branch", ""), 128)
            if isinstance(value.get("after_branch"), str)
            else cls._bounded_value(value.get("after_branch")),
            "head_changed": cls._bounded_value(value.get("head_changed")),
            "branch_changed": cls._bounded_value(value.get("branch_changed")),
            "error_code": cls._bounded_value(value.get("error_code")),
            "reviewed_at": cls._bounded_value(value.get("reviewed_at")),
            "snapshot_version": cls._bounded_value(value.get("snapshot_version")),
        }
        projected.update(
            cls._path_sample((field, value.get(field, [])) for field in WORKSPACE_PATH_FIELDS)
        )
        projected.update(
            {
                "audit_record_retained": True,
                "full_review_command": "agent-relay workspace review %s %s"
                % (task_id, action_id),
                "requires_explicit_resolution": value.get("status")
                in {"pending", "unavailable"},
            }
        )
        return projected

    @classmethod
    def _workspace_preflight_projection(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return cls._bounded_value(value)
        projected = {
            key: cls._bounded_value(value.get(key))
            for key in (
                "before_digest",
                "before_head",
                "before_branch",
                "snapshot_version",
            )
        }
        projected.update(
            cls._path_sample(
                (("preexisting_paths", value.get("preexisting_paths", [])),)
            )
        )
        projected["audit_record_retained"] = True
        return projected

    @classmethod
    def _action_details_projection(
        cls,
        action: ActionRecord,
        task_id: str,
    ) -> Dict[str, Any]:
        details = action.details
        projected: Dict[str, Any] = {}
        prioritized_keys = (
            "workspace_review",
            "source_agent",
            "classification",
            "evidence_code",
            "result_status",
            "result_error_code",
            "workspace_root",
            "authorization",
            "resolved_by",
            "resolved_manually",
            "workspace_preflight",
        )
        ordered_keys = list(prioritized_keys) + [
            key for key in sorted(details) if key not in prioritized_keys
        ]
        omitted_keys: List[str] = []
        for key in ordered_keys:
            if key not in details:
                continue
            if key == "result_proposal":
                projected["result_proposal_redacted"] = True
                continue
            if key == "workspace_review":
                value = cls._workspace_review_projection(
                    details[key],
                    task_id,
                    action.action_id,
                )
            elif key == "workspace_preflight":
                value = cls._workspace_preflight_projection(details[key])
            else:
                value = cls._bounded_value(details[key])
            if key == "workspace_review":
                projected[key] = value
                continue
            candidate = dict(projected)
            candidate[key] = value
            if len(_stable_json(candidate)) <= MAX_ACTION_DETAILS_CHARACTERS:
                projected[key] = value
            else:
                omitted_keys.append(key)

        if omitted_keys:
            projected["_projection"] = {
                "truncated": True,
                "omitted_key_count": len(omitted_keys),
                "omitted_keys": [
                    cls._bounded_string(key, 128)
                    for key in omitted_keys[:MAX_DETAIL_COLLECTION_ITEMS]
                ],
                "details_sha256": _digest(details),
            }
        return projected

    @classmethod
    def _action_projection(cls, action: ActionRecord, task_id: str) -> Dict[str, Any]:
        return {
            "action_id": action.action_id,
            "kind": action.kind,
            "agent_id": action.agent_id,
            "status": action.status,
            "started_at": action.started_at,
            "finished_at": action.finished_at,
            "details": cls._action_details_projection(action, task_id),
        }

    @classmethod
    def _action_history_projection(
        cls,
        checkpoint: TaskCheckpoint,
        required_action: Optional[ActionRecord],
    ) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
        actions = checkpoint.actions
        required_index = None
        if required_action is not None:
            required_index = next(
                index for index, action in enumerate(actions) if action is required_action
            )

        priority_indices = []
        if required_index is not None:
            priority_indices.append(required_index)
        priority_indices.extend(
            index
            for index in range(len(actions) - 1, -1, -1)
            if actions[index].status in {"pending", "unknown"}
        )
        priority_indices.extend(range(len(actions) - 1, -1, -1))

        selected = []
        selected_set = set()
        for index in priority_indices:
            if index in selected_set:
                continue
            selected.append(index)
            selected_set.add(index)
            if len(selected) == MAX_RENDERED_ACTIONS:
                break
        selected.sort()

        omitted = [action for index, action in enumerate(actions) if index not in selected_set]
        omitted_kind_counter = Counter(action.kind for action in omitted)
        omitted_kind_items = sorted(omitted_kind_counter.items())
        included_kind_items = omitted_kind_items[:MAX_DETAIL_COLLECTION_ITEMS]
        history = {
            "total_count": len(actions),
            "included_count": len(selected),
            "omitted_count": len(omitted),
            "truncated": bool(omitted),
            "selection": "required target action, then newest unresolved, then newest actions",
            "omitted_status_counts": dict(
                sorted(Counter(action.status for action in omitted).items())
            ),
            "omitted_kind_counts": dict(included_kind_items),
            "omitted_kind_counts_truncated": len(omitted_kind_items)
            > len(included_kind_items),
            "omitted_kind_type_count": len(omitted_kind_items),
            "omitted_kind_action_count_not_itemized": sum(
                count for _, count in omitted_kind_items[MAX_DETAIL_COLLECTION_ITEMS:]
            ),
            "omitted_unresolved_count": sum(
                action.status in {"pending", "unknown"} for action in omitted
            ),
            "full_history_retained": True,
        }
        return (
            [cls._action_projection(actions[index], checkpoint.task_id) for index in selected],
            history,
        )

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
        actions, action_history = self._action_history_projection(checkpoint, pending_action)
        checkpoint_value = {
            "schema_version": checkpoint.schema_version,
            "task_id": checkpoint.task_id,
            "title": checkpoint.title,
            "goal": checkpoint.goal,
            "state": checkpoint.state.to_dict(),
            "active_agent": checkpoint.active_agent,
            "status": checkpoint.status,
            "revision": checkpoint.revision,
            "created_at": checkpoint.created_at,
            "updated_at": checkpoint.updated_at,
            "actions": actions,
            "action_history": action_history,
            "routing_order": list(checkpoint.routing_order),
        }
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
