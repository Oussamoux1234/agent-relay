"""Conservative provider failure classification for safe routing decisions."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, Iterable, Optional, Pattern, Tuple

from .adapters import AgentExecutionResult
from .models import AgentSpec


@dataclass(frozen=True)
class FailureClassification:
    """A redaction-safe reason for continuing a route or stopping it."""

    category: str
    safe_to_fallback: bool
    evidence_code: str


def _compile(patterns: Iterable[Tuple[str, str]]) -> Tuple[Tuple[str, Pattern[str]], ...]:
    return tuple((code, re.compile(pattern, re.IGNORECASE)) for code, pattern in patterns)


AUTHENTICATION_PATTERNS = _compile(
    (
        ("authentication-error", r"\bauthentication_error\b"),
        ("permission-error", r"\bpermission_error\b"),
        ("billing-error", r"\bbilling_error\b"),
        ("http-auth-status", r"\b(?:http(?: status)?\s*[:=]?\s*)?(?:401|403)\b"),
        ("invalid-api-key", r"\b(?:incorrect|invalid|expired|revoked) api key\b"),
        ("login-required", r"\b(?:not logged in|please (?:log|sign) in)\b"),
        ("authentication-missing", r"\bno authentication information found\b"),
        ("authentication-policy-denied", r"\baccess denied by policy settings\b"),
        (
            "unsupported-classic-token",
            r"\bclassic personal access tokens? (?:are|is) not supported\b",
        ),
        ("unauthorized", r"\b(?:unauthorized|forbidden)\b"),
    )
)

QUOTA_PATTERNS = _compile(
    (
        ("credit-balance-exhausted", r"\bcredit_balance_exhausted\b|\bcredit balance exhausted\b"),
        (
            "organization-spend-limit",
            r"\borganization_spend_limit_exceeded\b|\borganization spend limit reached\b",
        ),
        (
            "project-spend-limit",
            r"\bproject_spend_limit_exceeded\b|\bproject spend limit reached\b",
        ),
        (
            "organization-usage-limit",
            r"\borganization_usage_limit_exceeded\b|\borganization usage limit reached\b",
        ),
        ("insufficient-quota", r"\binsufficient_quota\b"),
        ("enforced-spend-limit", r"\benforced_spend_limit_reached\b"),
        ("quota-exhausted", r"\bquota_exhausted\b|\bquota (?:has been )?exceeded\b"),
        ("current-quota-exceeded", r"\bexceeded your current quota\b"),
        ("capacity-exhausted", r"\b(?:you have )?exhausted your capacity\b"),
        ("usage-limit-reached", r"\busage limit (?:has been )?reached\b"),
        ("user-limit-hit", r"\b(?:you have|you've) hit your (?:usage )?limit\b"),
    )
)

RATE_LIMIT_PATTERNS = _compile(
    (
        ("http-429", r"\b(?:http(?: status)?\s*[:=]?\s*)?429\b"),
        ("rate-limit-error", r"\brate_limit_error\b"),
        ("rate-limit-exceeded", r"\brate_limit_exceeded\b"),
        ("rate-limit-class", r"\bratelimiterror\b"),
        ("too-many-requests", r"\btoo many requests\b"),
        ("rate-limit-text", r"\brate limit(?:ed| reached| exceeded)?\b"),
    )
)

PROVIDER_PATTERNS: Dict[str, Tuple[Tuple[str, Pattern[str]], ...]] = {
    "antigravity-cli": _compile((("gemini-resource-exhausted", r"\bresource_exhausted\b"),)),
    "gemini-cli": _compile((("gemini-resource-exhausted", r"\bresource_exhausted\b"),)),
    "github-copilot": _compile(
        (("copilot-rate-limit-hit", r"\byou(?:'|’)?ve hit a rate limit\b"),)
    ),
}

PROVIDER_QUOTA_PATTERNS: Dict[str, Tuple[Tuple[str, Pattern[str]], ...]] = {
    "github-copilot": _compile(
        (
            (
                "copilot-ai-credits-exhausted",
                r"\b(?:your\s+)?(?:included\s+)?ai credits?\s+(?:are\s+)?exhausted\b"
                r"|\bexhausted\s+(?:your\s+)?(?:included\s+)?ai credits?\b",
            ),
            (
                "copilot-session-limit-exhausted",
                r"\bsession_limits_exhausted\.requested\b",
            ),
        )
    ),
}

OVERLOAD_PATTERNS = _compile(
    (
        ("overloaded-error", r"\boverloaded_error\b"),
        ("http-529", r"\b(?:http(?: status)?\s*[:=]?\s*)?529\b"),
        ("service-unavailable", r"\b503\s+(?:service )?unavailable\b"),
    )
)


class FailureClassifier:
    """Recognize documented signals without persisting raw provider output."""

    @staticmethod
    def _match(
        text: str,
        patterns: Tuple[Tuple[str, Pattern[str]], ...],
    ) -> Optional[str]:
        for evidence_code, pattern in patterns:
            if pattern.search(text) is not None:
                return evidence_code
        return None

    def classify(
        self,
        spec: AgentSpec,
        execution: AgentExecutionResult,
    ) -> FailureClassification:
        if execution.status == "completed":
            return FailureClassification("success", False, "process-completed")
        if execution.timed_out:
            return FailureClassification("timeout", False, "adapter-timeout")
        if not execution.started:
            return FailureClassification("unavailable", True, "process-not-started")

        text = "\n".join((execution.stdout, execution.stderr, execution.error or ""))

        evidence = self._match(text, AUTHENTICATION_PATTERNS)
        if evidence is not None:
            return FailureClassification("authentication", False, evidence)

        if spec.provider_id is not None:
            evidence = self._match(
                text,
                PROVIDER_QUOTA_PATTERNS.get(spec.provider_id, ()),
            )
            if evidence is not None:
                return FailureClassification("quota_exhausted", True, evidence)

        evidence = self._match(text, QUOTA_PATTERNS)
        if evidence is not None:
            return FailureClassification("quota_exhausted", True, evidence)

        if spec.provider_id is not None:
            evidence = self._match(text, PROVIDER_PATTERNS.get(spec.provider_id, ()))
            if evidence is not None:
                return FailureClassification("rate_limited", True, evidence)

        evidence = self._match(text, RATE_LIMIT_PATTERNS)
        if evidence is not None:
            return FailureClassification("rate_limited", True, evidence)

        evidence = self._match(text, OVERLOAD_PATTERNS)
        if evidence is not None:
            return FailureClassification("overloaded", False, evidence)

        return FailureClassification("unknown", False, "unrecognized-failure")
