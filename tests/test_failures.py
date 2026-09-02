from __future__ import annotations

import unittest

from agent_relay.adapters import AgentExecutionResult
from agent_relay.failures import FailureClassifier
from agent_relay.models import AgentSpec


class FailureClassifierTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = FailureClassifier()

    @staticmethod
    def spec(provider_id: str = "codex-cli") -> AgentSpec:
        return AgentSpec(
            agent_id="agent",
            display_name="Agent",
            command=("agent",),
            capabilities=("repo-read",),
            provider_id=provider_id,
        )

    @staticmethod
    def result(
        stderr: str = "",
        *,
        status: str = "unknown",
        started: bool = True,
        timed_out: bool = False,
    ) -> AgentExecutionResult:
        return AgentExecutionResult(
            status=status,
            return_code=0 if status == "completed" else 1,
            stdout="",
            stderr=stderr,
            elapsed_ms=12,
            started=started,
            timed_out=timed_out,
        )

    def test_documented_provider_limit_signals_allow_fallback(self) -> None:
        cases = (
            ("codex-cli", "HTTP 429: RateLimitError", "rate_limited"),
            ("codex-cli", "error code: credit_balance_exhausted", "quota_exhausted"),
            ("claude-code", '{"type":"rate_limit_error"}', "rate_limited"),
            ("gemini-cli", "429 RESOURCE_EXHAUSTED", "rate_limited"),
            ("antigravity-cli", "status=RESOURCE_EXHAUSTED", "rate_limited"),
            ("github-copilot", "You've hit a rate limit", "rate_limited"),
            (
                "github-copilot",
                "Your included AI credits are exhausted",
                "quota_exhausted",
            ),
            (
                "github-copilot",
                "session_limits_exhausted.requested",
                "quota_exhausted",
            ),
        )

        for provider_id, message, category in cases:
            with self.subTest(provider_id=provider_id, message=message):
                classification = self.classifier.classify(
                    self.spec(provider_id),
                    self.result(message),
                )
                self.assertEqual(classification.category, category)
                self.assertTrue(classification.safe_to_fallback)

    def test_authentication_wins_over_limit_text_and_stops(self) -> None:
        classification = self.classifier.classify(
            self.spec("claude-code"),
            self.result("401 authentication_error after rate limit check"),
        )

        self.assertEqual(classification.category, "authentication")
        self.assertFalse(classification.safe_to_fallback)

    def test_copilot_authentication_signals_fail_closed(self) -> None:
        for message in (
            "No authentication information found",
            "Access denied by policy settings",
            "Classic personal access tokens are not supported",
        ):
            with self.subTest(message=message):
                classification = self.classifier.classify(
                    self.spec("github-copilot"),
                    self.result(message),
                )

                self.assertEqual(classification.category, "authentication")
                self.assertFalse(classification.safe_to_fallback)

    def test_timeout_overload_and_unknown_fail_closed(self) -> None:
        cases = (
            (self.result("", timed_out=True), "timeout"),
            (self.result("529 overloaded_error"), "overloaded"),
            (self.result("unexpected provider response"), "unknown"),
        )

        for result, category in cases:
            with self.subTest(category=category):
                classification = self.classifier.classify(self.spec(), result)
                self.assertEqual(classification.category, category)
                self.assertFalse(classification.safe_to_fallback)

    def test_process_that_never_started_can_be_skipped(self) -> None:
        classification = self.classifier.classify(
            self.spec(),
            self.result(status="failed", started=False),
        )

        self.assertEqual(classification.category, "unavailable")
        self.assertTrue(classification.safe_to_fallback)

    def test_success_is_never_reclassified_from_its_output(self) -> None:
        classification = self.classifier.classify(
            self.spec(),
            self.result("The task mentions HTTP 429", status="completed"),
        )

        self.assertEqual(classification.category, "success")
        self.assertFalse(classification.safe_to_fallback)


if __name__ == "__main__":
    unittest.main()
