from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.models import ActionRecord, AgentSpec, TaskCheckpoint
from agent_relay.prompting import (
    MAX_PROMPT_CHARACTERS,
    MAX_RENDERED_ACTIONS,
    MAX_WORKSPACE_PATH_SAMPLE,
    CheckpointPromptRenderer,
)
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore
from agent_relay.workspace import WorkspaceReview


DIGEST_A = "a" * 64
DIGEST_B = "b" * 64
HEAD_A = "1" * 40
HEAD_B = "2" * 40
TIMESTAMP = "2026-09-03T12:00:00Z"
WORKSPACE_ROOT = str(Path.cwd() / "workspace" / "project")


def prompt_payload(prompt: str):
    return json.loads(prompt.split("\n\n", 1)[1])


def review_with_paths(path_count: int, status: str = "accepted"):
    paths = tuple(
        "generated/%04d-%s.txt" % (index, "x" * 180) for index in range(path_count)
    )
    return WorkspaceReview(
        status=status,
        workspace_root=WORKSPACE_ROOT,
        before_digest=DIGEST_A,
        after_digest=DIGEST_B,
        before_head=HEAD_A,
        after_head=HEAD_B,
        before_branch="main",
        after_branch="main",
        introduced_paths=paths,
        final_dirty_paths=paths,
        reviewed_at=TIMESTAMP if status in {"accepted", "rolled-back"} else None,
    ).to_dict()


def action(index: int, status: str = "completed", path_count: int = 0):
    return ActionRecord(
        action_id="%032x" % (index + 1),
        kind="workspace-write",
        agent_id="writer",
        status=status,
        started_at=TIMESTAMP,
        finished_at=TIMESTAMP if status not in {"pending", "unknown"} else None,
        details={"workspace_review": review_with_paths(path_count)},
    )


class CheckpointPromptRendererTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.renderer = CheckpointPromptRenderer()

    def test_large_review_uses_counts_digests_and_a_stable_path_sample(self) -> None:
        checkpoint = TaskCheckpoint.create("Large review", "Continue after review")
        checkpoint.actions.append(action(0, path_count=1_200))
        original_review = checkpoint.actions[0].details["workspace_review"]

        first_prompt = self.renderer.render(checkpoint, "reader")
        second_prompt = self.renderer.render(checkpoint, "reader")

        payload = prompt_payload(first_prompt)
        projected = payload["checkpoint"]["actions"][0]["details"]["workspace_review"]
        self.assertEqual(first_prompt, second_prompt)
        self.assertLess(len(first_prompt), MAX_PROMPT_CHARACTERS)
        self.assertEqual(projected["status"], "accepted")
        self.assertEqual(projected["before_digest"], DIGEST_A)
        self.assertEqual(projected["after_digest"], DIGEST_B)
        self.assertEqual(projected["path_counts"]["introduced_paths"], 1_200)
        self.assertEqual(projected["path_counts"]["final_dirty_paths"], 1_200)
        self.assertEqual(projected["path_sample_count"], MAX_WORKSPACE_PATH_SAMPLE)
        self.assertTrue(projected["path_sample_truncated"])
        self.assertTrue(projected["audit_record_retained"])
        self.assertIn("agent-relay workspace review", projected["full_review_command"])
        self.assertEqual(len(original_review["introduced_paths"]), 1_200)
        self.assertNotIn("path_counts", original_review)

    def test_multiple_historic_reviews_have_independently_bounded_history(self) -> None:
        checkpoint = TaskCheckpoint.create("History", "Continue with bounded history")
        checkpoint.actions.extend(action(index, path_count=1_200) for index in range(40))
        for index, historic_action in enumerate(checkpoint.actions):
            historic_action.kind = "workspace-write-%02d" % index

        prompt = self.renderer.render(checkpoint, "reader")

        payload = prompt_payload(prompt)["checkpoint"]
        history = payload["action_history"]
        included_ids = [item["action_id"] for item in payload["actions"]]
        self.assertLess(len(prompt), MAX_PROMPT_CHARACTERS)
        self.assertEqual(len(payload["actions"]), MAX_RENDERED_ACTIONS)
        self.assertEqual(history["total_count"], 40)
        self.assertEqual(history["included_count"], MAX_RENDERED_ACTIONS)
        self.assertEqual(history["omitted_count"], 40 - MAX_RENDERED_ACTIONS)
        self.assertEqual(
            history["omitted_status_counts"],
            {"completed": 40 - MAX_RENDERED_ACTIONS},
        )
        self.assertEqual(len(history["omitted_kind_counts"]), 16)
        self.assertTrue(history["omitted_kind_counts_truncated"])
        self.assertEqual(history["omitted_kind_type_count"], 24)
        self.assertEqual(history["omitted_kind_action_count_not_itemized"], 8)
        self.assertTrue(history["truncated"])
        self.assertTrue(history["full_history_retained"])
        self.assertEqual(included_ids, ["%032x" % index for index in range(25, 41)])
        self.assertEqual(len(checkpoint.actions), 40)

    def test_omitted_unresolved_actions_are_reported_not_silently_lost(self) -> None:
        checkpoint = TaskCheckpoint.create("Blocked", "Preserve unresolved status")
        checkpoint.status = "blocked"
        checkpoint.actions.extend(action(index, status="unknown") for index in range(25))

        prompt = self.renderer.render(checkpoint, "reader")

        payload = prompt_payload(prompt)["checkpoint"]
        history = payload["action_history"]
        self.assertEqual(payload["status"], "blocked")
        self.assertEqual(history["omitted_unresolved_count"], 9)
        self.assertEqual(history["omitted_status_counts"], {"unknown": 9})
        self.assertTrue(all(item["status"] == "unknown" for item in payload["actions"]))

    def test_next_handoff_runs_after_large_persisted_review_history(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = RelayStore(root / "state")
            service = RelayService(store)
            service.register_agent(
                AgentSpec(
                    agent_id="source",
                    display_name="Source",
                    command=(sys.executable, "-c", "print('source')"),
                )
            )
            service.register_agent(
                AgentSpec(
                    agent_id="target",
                    display_name="Target",
                    command=(
                        sys.executable,
                        "-c",
                        "import sys; print('received=' + str(len(sys.stdin.read())))",
                    ),
                )
            )
            checkpoint = service.create_task(
                "Persisted history",
                "Launch the next handoff",
                active_agent="source",
            )
            expected_revision = checkpoint.revision
            checkpoint.actions.extend(
                action(index, path_count=1_200) for index in range(40)
            )
            store.save_task(checkpoint, expected_revision)

            outcome = service.handoff(checkpoint.task_id, "target", root)

            self.assertEqual(outcome.execution.status, "completed")
            self.assertLess(len(outcome.prompt), MAX_PROMPT_CHARACTERS)
            self.assertIn("received=", outcome.execution.stdout)
            persisted = store.get_task(checkpoint.task_id)
            self.assertEqual(len(persisted.actions), 41)
            self.assertEqual(
                len(persisted.actions[0].details["workspace_review"]["introduced_paths"]),
                1_200,
            )


if __name__ == "__main__":
    unittest.main()
