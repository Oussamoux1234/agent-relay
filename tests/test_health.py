from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from agent_relay.adapters import AgentExecutionResult
from agent_relay.failures import FailureClassification
from agent_relay.health import CooldownPolicy, StructuredRetryHintParser
from agent_relay.models import AgentSpec
from agent_relay.storage import RelayStore


class StructuredRetryHintParserTestCase(unittest.TestCase):
    @staticmethod
    def execution(stdout: str = "", stderr: str = "") -> AgentExecutionResult:
        return AgentExecutionResult(
            status="unknown",
            return_code=1,
            stdout=stdout,
            stderr=stderr,
            elapsed_ms=1,
            started=True,
            error="agent exited non-zero; external effects may have occurred",
        )

    def test_accepts_only_documented_structured_retry_fields(self) -> None:
        parser = StructuredRetryHintParser()
        cases = (
            ('{"headers":{"retry-after":"25"}}', 25, "retry-after-seconds"),
            ('{"headers":{"Retry-After":12.1}}', 13, "retry-after-seconds"),
            (
                '{"details":[{"@type":"type.googleapis.com/google.rpc.RetryInfo",'
                '"retryDelay":"40.25s"}]}',
                41,
                "google.rpc.RetryInfo.retry_delay",
            ),
        )

        for payload, seconds, signal_code in cases:
            with self.subTest(payload=payload):
                hint = parser.parse(self.execution(stderr=payload))
                self.assertIsNotNone(hint)
                self.assertEqual(hint.delay_seconds, seconds)
                self.assertEqual(hint.signal_code, signal_code)

    def test_rejects_retry_prose_and_untyped_google_fields(self) -> None:
        parser = StructuredRetryHintParser()
        cases = (
            "retry-after: 25",
            '{"message":"retry-after: 25"}',
            '{"retryDelay":"25s"}',
            '{"retry_after":25}',
            '{"retry_after_ms":25000}',
            '{"error":{"retry-after":25}}',
            '{"@type":"example.RetryInfo","retryDelay":"25s"}',
            '{"retry-after":true}',
        )

        for payload in cases:
            with self.subTest(payload=payload):
                self.assertIsNone(parser.parse(self.execution(stderr=payload)))

    def test_uses_the_longest_of_multiple_structured_hints(self) -> None:
        parser = StructuredRetryHintParser()
        execution = self.execution(
            stdout='{"retry-after":3}\n{"retry-after":9}',
            stderr='{"Retry-After":4}',
        )

        hint = parser.parse(execution)

        self.assertIsNotNone(hint)
        self.assertEqual(hint.delay_seconds, 9)


class CooldownPolicyTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.spec = AgentSpec(
            agent_id="codex-primary",
            display_name="Codex Primary",
            command=("codex",),
            capabilities=("repo-read",),
            provider_id="codex-cli",
        )
        self.observed_at = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
        self.execution = AgentExecutionResult(
            status="unknown",
            return_code=1,
            stdout="",
            stderr='{"error":{"type":"rate_limit_error"},"retry-after":90}',
            elapsed_ms=1,
            started=True,
        )

    def test_persists_only_redacted_health_metadata(self) -> None:
        policy = CooldownPolicy()
        record = policy.create_record(
            self.spec,
            FailureClassification("rate_limited", True, "rate-limit-error"),
            self.execution,
            "a" * 32,
            "b" * 32,
            self.observed_at,
        )
        self.assertIsNotNone(record)

        with tempfile.TemporaryDirectory() as directory:
            store = RelayStore(Path(directory))
            store.set_agent_health(record)
            persisted = store.health_path.read_text(encoding="utf-8")

        self.assertIn('"retry_source": "provider_hint"', persisted)
        self.assertIn('"cooldown_until": "2026-09-01T12:01:30Z"', persisted)
        self.assertNotIn("rate_limit_error", persisted)
        self.assertNotIn("retry-after\":90", persisted)

    def test_quota_without_structured_reset_requires_manual_recovery(self) -> None:
        policy = CooldownPolicy()
        record = policy.create_record(
            self.spec,
            FailureClassification("quota_exhausted", True, "enforced-spend-limit"),
            self.execution.__class__(
                status="unknown",
                return_code=1,
                stdout="",
                stderr='{"error":{"details":{"error_code":"enforced_spend_limit_reached"}}}',
                elapsed_ms=1,
                started=True,
            ),
            "a" * 32,
            "b" * 32,
            self.observed_at,
        )

        self.assertIsNotNone(record)
        self.assertEqual(record.retry_source, "manual_recovery")
        self.assertIsNone(record.cooldown_until)
        self.assertTrue(record.is_active(datetime(2036, 1, 1, tzinfo=timezone.utc)))


if __name__ == "__main__":
    unittest.main()
