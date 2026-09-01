from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

from agent_relay.errors import ConflictError, ValidationError
from agent_relay.models import ActionRecord, AgentSpec, StructuredAgentResult, utc_now
from agent_relay.results import (
    MAX_RESULT_SOURCE_BYTES,
    RESULT_BEGIN_MARKER,
    RESULT_END_MARKER,
    StructuredResultExtractor,
)
from agent_relay.service import RelayService
from agent_relay.storage import RelayStore


RESULT_AGENT_CODE = (
    "import json, sys; "
    "prompt = sys.stdin.read(); "
    "payload = json.loads(prompt[prompt.index('{'):]); "
    "contract = payload['handoff']['result_contract']; "
    "result = {"
    "'schema_version': '1.0', "
    "'task_id': contract['schema']['task_id'], "
    "'source_action_id': contract['schema']['source_action_id'], "
    "'summary': 'Analysis complete', "
    "'decisions': ['Keep API stable', 'Use result envelopes', 'Use result envelopes'], "
    "'constraints': ['Remain read-only'], "
    "'files_changed': ['agent_relay/results.py'], "
    "'tests': ['unit suite passed'], "
    "'next_steps': ['Review and accept']}; "
    "print('provider explanation that must not be persisted'); "
    "print(contract['begin_marker']); "
    "print(json.dumps(result)); "
    "print(contract['end_marker'])"
)


def result_value(
    task_id: str = "task123",
    action_id: str = "action123",
    summary: str = "Verified summary",
):
    return {
        "schema_version": "1.0",
        "task_id": task_id,
        "source_action_id": action_id,
        "summary": summary,
        "decisions": ["Use JSON"],
        "constraints": [],
        "files_changed": ["agent_relay/results.py"],
        "tests": ["tests passed"],
        "next_steps": ["Review"],
    }


def marked_result(value) -> str:
    return "%s\n%s\n%s" % (
        RESULT_BEGIN_MARKER,
        json.dumps(value),
        RESULT_END_MARKER,
    )


class StructuredResultExtractorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.extractor = StructuredResultExtractor()

    def test_extracts_plain_json_and_provider_wrapped_json(self) -> None:
        value = result_value()
        cases = (
            marked_result(value),
            json.dumps({"type": "result", "result": marked_result(value)}),
            json.dumps({"response": marked_result(value), "stats": {"tokens": 12}}),
            "\n".join(
                (
                    json.dumps({"event": "progress", "message": "working"}),
                    json.dumps(
                        {
                            "event": "result",
                            "result": {
                                "status": "SUCCESS",
                                "response": marked_result(value),
                            },
                        }
                    ),
                )
            ),
        )

        for stdout in cases:
            with self.subTest(stdout=stdout[:30]):
                extraction = self.extractor.extract(stdout, "task123", "action123")
                self.assertEqual(extraction.status, "ready")
                self.assertEqual(extraction.result.summary, "Verified summary")

    def test_rejects_wrong_binding_unknown_fields_and_malformed_json(self) -> None:
        cases = []
        wrong_task = result_value(task_id="other")
        cases.append((marked_result(wrong_task), "task-mismatch"))
        wrong_action = result_value(action_id="other")
        cases.append((marked_result(wrong_action), "action-mismatch"))
        unknown_field = result_value()
        unknown_field["reasoning"] = "private reasoning must not be imported"
        cases.append((marked_result(unknown_field), "invalid-schema"))
        cases.append(
            (
                "%s\n{not-json}\n%s" % (RESULT_BEGIN_MARKER, RESULT_END_MARKER),
                "invalid-json",
            )
        )

        for stdout, error_code in cases:
            with self.subTest(error_code=error_code):
                extraction = self.extractor.extract(stdout, "task123", "action123")
                self.assertEqual(extraction.status, "invalid")
                self.assertEqual(extraction.error_code, error_code)
                self.assertIsNone(extraction.result)

    def test_rejects_ambiguous_and_oversized_output(self) -> None:
        first = marked_result(result_value(summary="First"))
        second = marked_result(result_value(summary="Second"))

        ambiguous = self.extractor.extract(
            first + "\n" + second,
            "task123",
            "action123",
        )
        oversized = self.extractor.extract(
            "x" * (MAX_RESULT_SOURCE_BYTES + 1),
            "task123",
            "action123",
        )

        self.assertEqual(ambiguous.error_code, "ambiguous-results")
        self.assertEqual(oversized.error_code, "output-too-large")

    def test_missing_and_unclosed_envelopes_are_distinct(self) -> None:
        missing = self.extractor.extract("ordinary answer", "task123", "action123")
        unclosed = self.extractor.extract(
            RESULT_BEGIN_MARKER + "{}",
            "task123",
            "action123",
        )

        self.assertEqual(missing.status, "missing")
        self.assertEqual(missing.error_code, "result-envelope-missing")
        self.assertEqual(unclosed.status, "invalid")
        self.assertEqual(unclosed.error_code, "malformed-markers")

    def test_result_model_bounds_fields_and_total_content(self) -> None:
        with self.assertRaises(ValidationError):
            StructuredAgentResult.from_dict(
                result_value(summary="x" * 20_001)
            )
        oversized = result_value(summary="summary")
        for field_name in (
            "decisions",
            "constraints",
            "files_changed",
            "tests",
            "next_steps",
        ):
            oversized[field_name] = ["x" * 13_000]
        with self.assertRaises(ValidationError):
            StructuredAgentResult.from_dict(oversized)
        invalid_list = result_value()
        invalid_list["tests"] = "not-a-list"
        with self.assertRaises(ValidationError):
            StructuredAgentResult.from_dict(invalid_list)
        invalid_unicode = result_value(summary="\ud800")
        with self.assertRaises(ValidationError):
            StructuredAgentResult.from_dict(invalid_unicode)
        invalid_output = self.extractor.extract("\ud800", "task123", "action123")
        self.assertEqual(invalid_output.error_code, "invalid-unicode")


class ResultAcceptanceTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = RelayStore(self.root / "state")
        self.service = RelayService(self.store)
        self.register("codex-primary", RESULT_AGENT_CODE)
        self.register("codex-backup", "print('backup')")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def register(self, agent_id: str, code: str) -> None:
        self.service.register_agent(
            AgentSpec(
                agent_id=agent_id,
                display_name=agent_id,
                command=(sys.executable, "-c", code),
                timeout_seconds=5,
                capabilities=("repo-read",),
                provider_id="codex-cli",
            )
        )

    def run_result_agent(self):
        task = self.service.create_task(
            "Structured result",
            "Carry fresh verified facts",
            active_agent="codex-primary",
            summary="Initial summary",
        )
        task = self.service.add_task_notes(
            task.task_id,
            decisions=["Keep API stable"],
        )
        self.service.configure_route(
            task.task_id,
            ["codex-primary", "codex-backup"],
        )
        return self.service.run_route(task.task_id, self.root)

    def test_route_creates_proposal_then_preview_and_accept_update_memory(self) -> None:
        outcome = self.run_result_agent()
        attempt = outcome.attempts[0]

        self.assertEqual(attempt.result_status, "pending")
        self.assertEqual(attempt.result.summary, "Analysis complete")
        self.assertEqual(outcome.task.state.summary, "Initial summary")
        source_action = outcome.task.actions[-1]
        self.assertEqual(source_action.details["result_status"], "pending")
        self.assertIn("result_proposal", source_action.details)

        preview = self.service.preview_result(outcome.task.task_id, attempt.action_id)

        self.assertEqual(preview.task.revision, outcome.task.revision)
        self.assertEqual(preview.changes["summary_before"], "Initial summary")
        self.assertEqual(preview.changes["summary_after"], "Analysis complete")
        self.assertEqual(
            preview.changes["additions"]["decisions"],
            ["Use result envelopes"],
        )
        self.assertEqual(
            self.store.get_task(outcome.task.task_id).state.summary,
            "Initial summary",
        )

        with self.assertRaises(ConflictError):
            self.service.accept_result(
                outcome.task.task_id,
                attempt.action_id,
                preview.task.revision - 1,
            )

        accepted = self.service.accept_result(
            outcome.task.task_id,
            attempt.action_id,
            preview.task.revision,
        )

        self.assertEqual(accepted.state.summary, "Analysis complete")
        self.assertEqual(
            accepted.state.decisions,
            ["Keep API stable", "Use result envelopes"],
        )
        self.assertIn("agent_relay/results.py", accepted.state.files_changed)
        self.assertEqual(accepted.actions[-1].kind, "result-accept")
        self.assertEqual(accepted.actions[-1].details["applied_counts"]["decisions"], 1)
        accepted_source = next(
            action for action in accepted.actions if action.action_id == attempt.action_id
        )
        self.assertEqual(accepted_source.details["result_status"], "accepted")
        self.assertNotIn("result_proposal", accepted_source.details)
        persisted = (
            self.root / "state" / "tasks" / (accepted.task_id + ".json")
        ).read_text(encoding="utf-8")
        self.assertNotIn("provider explanation that must not be persisted", persisted)
        self.assertNotIn(RESULT_BEGIN_MARKER, persisted)

        with self.assertRaises(ConflictError):
            self.service.preview_result(accepted.task_id, attempt.action_id)

    def test_pending_proposal_is_redacted_from_later_agent_prompts(self) -> None:
        outcome = self.run_result_agent()

        preview = self.service.preview_route(outcome.task.task_id)

        self.assertNotIn("Analysis complete", preview.prompt)
        self.assertNotIn("result_proposal\"", preview.prompt)
        self.assertIn("result_proposal_redacted", preview.prompt)

    def test_checkpoint_change_after_preview_prevents_acceptance(self) -> None:
        outcome = self.run_result_agent()
        attempt = outcome.attempts[0]
        preview = self.service.preview_result(outcome.task.task_id, attempt.action_id)
        self.service.add_task_notes(outcome.task.task_id, summary="Human update")

        with self.assertRaises(ConflictError):
            self.service.accept_result(
                outcome.task.task_id,
                attempt.action_id,
                preview.task.revision,
            )

        self.assertEqual(
            self.store.get_task(outcome.task.task_id).state.summary,
            "Human update",
        )

    def test_later_agent_execution_makes_older_proposal_stale(self) -> None:
        outcome = self.run_result_agent()
        first_attempt = outcome.attempts[0]
        second = self.service.run_route(outcome.task.task_id, self.root)

        with self.assertRaises(ConflictError):
            self.service.accept_result(
                outcome.task.task_id,
                first_attempt.action_id,
                second.task.revision,
            )

    def test_malformed_success_is_audited_without_mutating_memory(self) -> None:
        malformed_code = (
            "print(%r); print('{bad-json}'); print(%r)"
            % (RESULT_BEGIN_MARKER, RESULT_END_MARKER)
        )
        self.register("gemini-primary", malformed_code)
        self.register("gemini-backup", "print('backup')")
        task = self.service.create_task(
            "Malformed",
            "Reject malformed output",
            active_agent="gemini-primary",
            summary="Keep me",
        )
        self.service.configure_route(
            task.task_id,
            ["gemini-primary", "gemini-backup"],
        )

        outcome = self.service.run_route(task.task_id, self.root)

        self.assertEqual(outcome.attempts[0].result_status, "invalid")
        self.assertEqual(outcome.attempts[0].result_error_code, "invalid-json")
        self.assertEqual(outcome.task.state.summary, "Keep me")
        self.assertNotIn("result_proposal", outcome.task.actions[-1].details)
        with self.assertRaises(ConflictError):
            self.service.preview_result(
                outcome.task.task_id,
                outcome.attempts[0].action_id,
            )

    def test_preview_rejects_a_merge_that_would_overflow_checkpoint_limits(self) -> None:
        task = self.service.create_task(
            "Full checkpoint",
            "Keep bounded state",
            active_agent="codex-primary",
        )
        task = self.service.add_task_notes(
            task.task_id,
            decisions=["decision-%d" % index for index in range(1_000)],
        )
        self.service.configure_route(
            task.task_id,
            ["codex-primary", "codex-backup"],
        )
        outcome = self.service.run_route(task.task_id, self.root)

        with self.assertRaises(ValidationError):
            self.service.preview_result(
                task.task_id,
                outcome.attempts[0].action_id,
            )

        self.assertEqual(len(self.store.get_task(task.task_id).state.decisions), 1_000)

    def test_legacy_completed_route_action_without_result_fields_still_loads(self) -> None:
        task = self.service.create_task(
            "Legacy",
            "Load old actions",
            active_agent="codex-primary",
        )
        now = utc_now()
        task.actions.append(
            ActionRecord(
                action_id="legacyaction",
                kind="route-run",
                agent_id="codex-primary",
                status="completed",
                started_at=now,
                finished_at=now,
                details={"classification": "success"},
            )
        )
        saved = self.store.save_task(task, task.revision)

        loaded = self.store.get_task(saved.task_id)

        self.assertEqual(loaded.actions[-1].details["classification"], "success")
        with self.assertRaises(ConflictError):
            self.service.preview_result(loaded.task_id, "legacyaction")


if __name__ == "__main__":
    unittest.main()
