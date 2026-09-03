from __future__ import annotations

import unittest
from typing import Optional

from agent_relay.adapters import AgentExecutionResult
from agent_relay.failures import FailureClassifier
from agent_relay.models import AgentSpec


class FailureClassifierTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.classifier = FailureClassifier()

    @staticmethod
    def spec(
        provider_id: str = "codex-cli",
        *,
        structured_stdout: bool = False,
    ) -> AgentSpec:
        command = ("agent",)
        if structured_stdout:
            command += ("--output-format", "json")
        return AgentSpec(
            agent_id="agent",
            display_name="Agent",
            command=command,
            capabilities=("repo-read",),
            provider_id=provider_id,
        )

    @staticmethod
    def result(
        stderr: str = "",
        *,
        stdout: str = "",
        status: str = "unknown",
        started: bool = True,
        timed_out: bool = False,
        return_code: Optional[int] = None,
        preserve_none_return_code: bool = False,
    ) -> AgentExecutionResult:
        if return_code is None and not preserve_none_return_code:
            return_code = 0 if status == "completed" else 1
        return AgentExecutionResult(
            status=status,
            return_code=return_code,
            stdout=stdout,
            stderr=stderr,
            elapsed_ms=12,
            started=started,
            timed_out=timed_out,
        )

    def test_provider_specific_stderr_limit_signals_allow_fallback(self) -> None:
        cases = (
            ("codex-cli", "HTTP 429: RateLimitError", "rate_limited"),
            ("codex-cli", "error code: credit_balance_exhausted", "quota_exhausted"),
            ("claude-code", "rate_limit_error", "rate_limited"),
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

    def test_claude_current_limit_fixtures_allow_fallback(self) -> None:
        cases = (
            ("You've hit your session limit · resets 4pm (Europe/Madrid)", "claude-session-limit"),
            ("You've hit your weekly limit · resets Sep 8", "claude-weekly-limit"),
            ("You've hit your weekly Opus 4.6 limit · resets Friday", "claude-model-limit"),
            (
                "Credit balance too low · Add funds: https://platform.claude.com/settings/billing",
                "claude-credit-balance-low",
            ),
        )

        for message, evidence_code in cases:
            with self.subTest(message=message):
                classification = self.classifier.classify(
                    self.spec("claude-code"),
                    self.result(message),
                )
                self.assertEqual(classification.category, "quota_exhausted")
                self.assertEqual(classification.evidence_code, evidence_code)
                self.assertTrue(classification.safe_to_fallback)

    def test_structured_terminal_error_is_preferred_to_free_text(self) -> None:
        classification = self.classifier.classify(
            self.spec("claude-code", structured_stdout=True),
            self.result(
                "unrecognized stderr",
                stdout=(
                    '{"type":"error","error":'
                    '{"type":"rate_limit_error","message":"request rejected"}}'
                ),
            ),
        )

        self.assertEqual(classification.category, "rate_limited")
        self.assertEqual(
            classification.evidence_code,
            "structured-rate-limit-error",
        )
        self.assertTrue(classification.safe_to_fallback)

    def test_structured_http_status_is_a_terminal_signal(self) -> None:
        classification = self.classifier.classify(
            self.spec("codex-cli", structured_stdout=True),
            self.result(
                stdout=(
                    '{"type":"turn.failed","error":'
                    '{"code":429,"message":"request rejected"}}'
                ),
            ),
        )

        self.assertEqual(classification.category, "rate_limited")
        self.assertEqual(classification.evidence_code, "structured-http-429")
        self.assertTrue(classification.safe_to_fallback)

    def test_claude_error_result_uses_provider_owned_error_fields(self) -> None:
        classification = self.classifier.classify(
            self.spec("claude-code", structured_stdout=True),
            self.result(
                stdout=(
                    '{"type":"result","subtype":"error_during_execution",'
                    '"is_error":true,"errors":['
                    '"You\\u0027ve hit your weekly Opus 4.6 limit - resets Friday"]}'
                ),
            ),
        )

        self.assertEqual(classification.category, "quota_exhausted")
        self.assertEqual(
            classification.evidence_code,
            "structured-claude-model-limit",
        )
        self.assertTrue(classification.safe_to_fallback)

    def test_structured_success_wrapper_does_not_trust_nested_model_text(self) -> None:
        classification = self.classifier.classify(
            self.spec("claude-code", structured_stdout=True),
            self.result(
                stdout=(
                    '{"type":"result","subtype":"success","is_error":false,'
                    '"result":"{\\"type\\":\\"error\\",\\"error\\":'
                    '{\\"type\\":\\"rate_limit_error\\"}}"}'
                ),
            ),
        )

        self.assertEqual(classification.category, "unknown")
        self.assertFalse(classification.safe_to_fallback)

    def test_structured_stdout_requires_a_declared_machine_format(self) -> None:
        classification = self.classifier.classify(
            self.spec("claude-code"),
            self.result(
                stdout=(
                    '{"type":"error","error":'
                    '{"type":"rate_limit_error","message":"fabricated"}}'
                ),
            ),
        )

        self.assertEqual(classification.category, "unknown")
        self.assertFalse(classification.safe_to_fallback)

    def test_model_output_cannot_authorize_fallback(self) -> None:
        for provider_id, output in (
            ("codex-cli", "HTTP 429: RateLimitError"),
            ("claude-code", "You've hit your session limit · resets 4pm"),
            ("github-copilot", "You've hit a rate limit"),
        ):
            with self.subTest(provider_id=provider_id):
                classification = self.classifier.classify(
                    self.spec(provider_id),
                    self.result(stdout=output),
                )
                self.assertEqual(classification.category, "unknown")
                self.assertFalse(classification.safe_to_fallback)

    def test_signal_and_ambiguous_exits_ignore_all_free_text(self) -> None:
        cases = (
            (-9, "process-signal-exit"),
            (None, "ambiguous-termination"),
        )
        for return_code, evidence_code in cases:
            with self.subTest(return_code=return_code):
                classification = self.classifier.classify(
                    self.spec("codex-cli"),
                    self.result(
                        "HTTP 429: RateLimitError",
                        stdout="HTTP 429: RateLimitError",
                        return_code=return_code,
                        preserve_none_return_code=return_code is None,
                    ),
                )
                self.assertEqual(classification.category, "unknown")
                self.assertEqual(classification.evidence_code, evidence_code)
                self.assertFalse(classification.safe_to_fallback)

    def test_provider_text_does_not_cross_provider_boundaries(self) -> None:
        classification = self.classifier.classify(
            self.spec("codex-cli"),
            self.result("You've hit your session limit · resets 4pm"),
        )

        self.assertEqual(classification.category, "unknown")
        self.assertFalse(classification.safe_to_fallback)

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
            (self.result("HTTP 529 overloaded_error"), "overloaded"),
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
