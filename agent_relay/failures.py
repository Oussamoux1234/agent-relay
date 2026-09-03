"""Conservative provider failure classification for safe routing decisions."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Pattern, Tuple

from .adapters import AgentExecutionResult
from .models import AgentSpec


MAX_STRUCTURED_FAILURE_DOCUMENTS = 128
STRUCTURED_OUTPUT_FORMATS = frozenset(("json", "stream-json"))


@dataclass(frozen=True)
class FailureClassification:
    """A redaction-safe reason for continuing a route or stopping it."""

    category: str
    safe_to_fallback: bool
    evidence_code: str


def _compile(patterns: Iterable[Tuple[str, str]]) -> Tuple[Tuple[str, Pattern[str]], ...]:
    return tuple(
        (code, re.compile(pattern, re.IGNORECASE | re.MULTILINE))
        for code, pattern in patterns
    )


COMMON_AUTHENTICATION_PATTERNS = _compile(
    (
        ("authentication-error", r"\bauthentication_error\b"),
        ("permission-error", r"\bpermission_error\b"),
        ("billing-error", r"\bbilling_error\b"),
        ("http-auth-status", r"^\s*(?:http(?: status)?\s*[:=]?\s*)?(?:401|403)\b"),
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

PROVIDER_AUTHENTICATION_PATTERNS: Dict[
    str, Tuple[Tuple[str, Pattern[str]], ...]
] = {
    provider_id: COMMON_AUTHENTICATION_PATTERNS
    for provider_id in (
        "antigravity-cli",
        "claude-code",
        "codex-cli",
        "gemini-cli",
        "github-copilot",
    )
}

PROVIDER_QUOTA_PATTERNS: Dict[str, Tuple[Tuple[str, Pattern[str]], ...]] = {
    "codex-cli": _compile(
        (
            ("credit-balance-exhausted", r"\bcredit_balance_exhausted\b"),
            ("organization-spend-limit", r"\borganization_spend_limit_exceeded\b"),
            ("project-spend-limit", r"\bproject_spend_limit_exceeded\b"),
            ("organization-usage-limit", r"\borganization_usage_limit_exceeded\b"),
            ("insufficient-quota", r"\binsufficient_quota\b"),
            ("enforced-spend-limit", r"\benforced_spend_limit_reached\b"),
            ("current-quota-exceeded", r"\bexceeded your current quota\b"),
        )
    ),
    "claude-code": _compile(
        (
            (
                "claude-session-limit",
                r"^\s*you(?:'|’)ve hit your session limit(?:\s*[·-]\s*resets\b.*)?\s*$",
            ),
            (
                "claude-weekly-limit",
                r"^\s*you(?:'|’)ve hit your weekly limit(?:\s*[·-]\s*resets\b.*)?\s*$",
            ),
            (
                "claude-model-limit",
                r"^\s*you(?:'|’)ve hit your (?:weekly\s+)?"
                r"(?:claude\s+)?(?:opus|sonnet|haiku|fable)(?:\s+\d+(?:\.\d+)?)?"
                r"(?:\s+weekly)? limit(?:\s*[·-]\s*resets\b.*)?\s*$",
            ),
            (
                "claude-current-limit",
                r"^\s*you(?:'|’)ve hit your limit\s*[·-]\s*resets\b.*$",
            ),
            (
                "claude-monthly-spend-limit",
                r"^\s*you(?:'|’)ve hit your monthly spend limit\.?\s*$",
            ),
            (
                "claude-credit-balance-low",
                r"^\s*credit balance (?:is )?too low(?:\s*[·-]\s*add funds:\s*https?://\S+)?\s*$",
            ),
        )
    ),
    "github-copilot": _compile(
        (
            (
                "copilot-ai-credits-exhausted",
                r"^\s*(?:your\s+)?(?:included\s+)?ai credits?\s+(?:are\s+)?exhausted\s*$"
                r"|^\s*exhausted\s+(?:your\s+)?(?:included\s+)?ai credits?\s*$",
            ),
            (
                "copilot-session-limit-exhausted",
                r"\bsession_limits_exhausted\.requested\b",
            ),
        )
    ),
}

PROVIDER_RATE_LIMIT_PATTERNS: Dict[str, Tuple[Tuple[str, Pattern[str]], ...]] = {
    "codex-cli": _compile(
        (
            ("codex-http-429", r"^\s*(?:error:\s*)?http(?: status)?\s*[:=]?\s*429\b"),
            ("codex-rate-limit-error", r"\brate_limit_error\b"),
            ("codex-rate-limit-exceeded", r"\brate_limit_exceeded\b"),
            ("codex-rate-limit-class", r"\bratelimiterror\b"),
            ("codex-too-many-requests", r"^\s*(?:error:\s*)?too many requests\s*$"),
            ("codex-429-too-many-requests", r"^\s*429\s+too many requests\s*$"),
        )
    ),
    "claude-code": _compile(
        (
            ("claude-rate-limit-error", r"\brate_limit_error\b"),
            ("claude-http-429", r"^\s*(?:error:\s*)?http(?: status)?\s*[:=]?\s*429\b"),
        )
    ),
    "gemini-cli": _compile(
        (
            (
                "gemini-resource-exhausted",
                r"^\s*(?:error:\s*)?(?:429\s+)?resource_exhausted\b",
            ),
        )
    ),
    "antigravity-cli": _compile(
        (
            (
                "gemini-resource-exhausted",
                r"^\s*(?:error:\s*)?(?:429\s+)?(?:status=)?resource_exhausted\b",
            ),
        )
    ),
    "github-copilot": _compile(
        (
            (
                "copilot-rate-limit-hit",
                r"^\s*you(?:'|’)?ve hit a rate limit(?:\.|\s*[·-].*)?\s*$",
            ),
        )
    ),
}

PROVIDER_OVERLOAD_PATTERNS: Dict[str, Tuple[Tuple[str, Pattern[str]], ...]] = {
    provider_id: _compile(
        (
            ("overloaded-error", r"\boverloaded_error\b"),
            ("http-529", r"^\s*(?:http(?: status)?\s*[:=]?\s*)?529\b"),
            ("service-unavailable", r"^\s*(?:http(?: status)?\s*[:=]?\s*)?503\b"),
        )
    )
    for provider_id in (
        "antigravity-cli",
        "claude-code",
        "codex-cli",
        "gemini-cli",
        "github-copilot",
    )
}

STRUCTURED_FAILURE_CODES: Dict[str, Dict[str, frozenset[str]]] = {
    "codex-cli": {
        "authentication": frozenset(
            ("authentication_error", "permission_error", "invalid_api_key")
        ),
        "quota_exhausted": frozenset(
            (
                "billing_error",
                "credit_balance_exhausted",
                "enforced_spend_limit_reached",
                "insufficient_quota",
                "organization_spend_limit_exceeded",
                "organization_usage_limit_exceeded",
                "project_spend_limit_exceeded",
            )
        ),
        "rate_limited": frozenset(("rate_limit_error", "rate_limit_exceeded")),
        "overloaded": frozenset(("overloaded_error",)),
    },
    "claude-code": {
        "authentication": frozenset(("authentication_error", "permission_error")),
        "quota_exhausted": frozenset(
            (
                "billing_error",
                "credit_balance_exhausted",
                "credit_balance_too_low",
                "monthly_spend_limit",
                "session_limit",
                "weekly_limit",
                "model_limit",
            )
        ),
        "rate_limited": frozenset(("rate_limit_error", "rate_limit")),
        "overloaded": frozenset(("overloaded_error",)),
    },
    "gemini-cli": {
        "authentication": frozenset(("unauthenticated", "permission_denied")),
        "quota_exhausted": frozenset(("quota_exhausted",)),
        "rate_limited": frozenset(("resource_exhausted",)),
        "overloaded": frozenset(("unavailable",)),
    },
    "antigravity-cli": {
        "authentication": frozenset(("unauthenticated", "permission_denied")),
        "quota_exhausted": frozenset(("quota_exhausted",)),
        "rate_limited": frozenset(("resource_exhausted",)),
        "overloaded": frozenset(("unavailable",)),
    },
    "github-copilot": {
        "authentication": frozenset(
            ("authentication_error", "permission_error", "unauthorized")
        ),
        "quota_exhausted": frozenset(
            ("ai_credits_exhausted", "session_limits_exhausted.requested")
        ),
        "rate_limited": frozenset(("rate_limit_error", "rate_limit_exceeded")),
        "overloaded": frozenset(("service_unavailable",)),
    },
}


class FailureClassifier:
    """Recognize terminal provider signals without trusting model output."""

    @staticmethod
    def _match(
        text: str,
        patterns: Tuple[Tuple[str, Pattern[str]], ...],
    ) -> Optional[str]:
        for evidence_code, pattern in patterns:
            if pattern.search(text) is not None:
                return evidence_code
        return None

    @staticmethod
    def _structured_stdout_enabled(spec: AgentSpec) -> bool:
        if spec.provider_id not in STRUCTURED_FAILURE_CODES:
            return False
        arguments = spec.command[1:]
        for index, argument in enumerate(arguments):
            if argument == "--json" and spec.provider_id == "codex-cli":
                return True
            if argument.startswith("--output-format="):
                return argument.partition("=")[2] in STRUCTURED_OUTPUT_FORMATS
            if (
                argument == "--output-format"
                and index + 1 < len(arguments)
                and arguments[index + 1] in STRUCTURED_OUTPUT_FORMATS
            ):
                return True
        return False

    @staticmethod
    def _json_documents(text: str) -> Tuple[Dict[str, Any], ...]:
        if not isinstance(text, str) or not text.strip():
            return ()
        candidates = [text.strip()]
        candidates.extend(line.strip() for line in text.splitlines() if line.strip())
        documents: List[Dict[str, Any]] = []
        seen = set()
        for candidate in candidates:
            if candidate in seen:
                continue
            seen.add(candidate)
            try:
                decoded = json.loads(candidate)
            except (json.JSONDecodeError, RecursionError, UnicodeError):
                continue
            if isinstance(decoded, dict):
                documents.append(decoded)
            if len(documents) >= MAX_STRUCTURED_FAILURE_DOCUMENTS:
                break
        return tuple(documents)

    @staticmethod
    def _terminal_fields(
        provider_id: str,
        document: Dict[str, Any],
    ) -> Optional[Tuple[Tuple[str, ...], str]]:
        event_type = document.get("type")
        error = document.get("error")
        codes: List[str] = []
        messages: List[str] = []

        if event_type in {"error", "turn.failed"} or (
            event_type is None and isinstance(error, dict)
        ):
            if isinstance(error, dict):
                for name in ("type", "code", "status"):
                    value = error.get(name)
                    if isinstance(value, str) and 0 < len(value) <= 120:
                        codes.append(value)
                status = error.get("code")
                if isinstance(status, int) and not isinstance(status, bool):
                    codes.append(str(status))
                message = error.get("message")
                if isinstance(message, str) and len(message) <= 20_000:
                    messages.append(message)
            elif isinstance(error, str) and len(error) <= 20_000:
                messages.append(error)
            message = document.get("message")
            if isinstance(message, str) and len(message) <= 20_000:
                messages.append(message)
            code = document.get("code")
            if isinstance(code, (str, int)) and not isinstance(code, bool):
                codes.append(str(code))
        elif (
            provider_id == "claude-code"
            and event_type == "result"
            and document.get("is_error") is True
            and document.get("subtype")
            in {
                "error_during_execution",
                "error_max_budget_usd",
                "error_max_turns",
            }
        ):
            subtype = document.get("subtype")
            if isinstance(subtype, str):
                codes.append(subtype)
            errors = document.get("errors")
            if isinstance(errors, list):
                messages.extend(
                    value for value in errors if isinstance(value, str) and len(value) <= 20_000
                )
        else:
            return None

        normalized_codes = tuple(
            value.strip().lower().replace("-", "_")
            for value in codes
            if value.strip()
        )
        return normalized_codes, "\n".join(messages)

    def _classify_structured_document(
        self,
        provider_id: str,
        document: Dict[str, Any],
    ) -> Optional[FailureClassification]:
        terminal = self._terminal_fields(provider_id, document)
        if terminal is None:
            return None
        codes, message = terminal
        provider_codes = STRUCTURED_FAILURE_CODES.get(provider_id, {})
        for status_code, category in (
            ("401", "authentication"),
            ("403", "authentication"),
            ("429", "rate_limited"),
            ("503", "overloaded"),
            ("529", "overloaded"),
        ):
            if status_code in codes:
                return FailureClassification(
                    category,
                    category == "rate_limited",
                    "structured-http-%s" % status_code,
                )
        for category in ("authentication", "quota_exhausted", "rate_limited", "overloaded"):
            known_codes = provider_codes.get(category, frozenset())
            matched_code = next((code for code in codes if code in known_codes), None)
            if matched_code is not None:
                return FailureClassification(
                    category,
                    category in {"quota_exhausted", "rate_limited"},
                    "structured-%s" % matched_code.replace("_", "-"),
                )

        for category, patterns in (
            ("authentication", PROVIDER_AUTHENTICATION_PATTERNS.get(provider_id, ())),
            ("quota_exhausted", PROVIDER_QUOTA_PATTERNS.get(provider_id, ())),
            ("rate_limited", PROVIDER_RATE_LIMIT_PATTERNS.get(provider_id, ())),
            ("overloaded", PROVIDER_OVERLOAD_PATTERNS.get(provider_id, ())),
        ):
            evidence = self._match(message, patterns)
            if evidence is not None:
                return FailureClassification(
                    category,
                    category in {"quota_exhausted", "rate_limited"},
                    "structured-%s" % evidence,
                )
        return None

    def _structured_classification(
        self,
        spec: AgentSpec,
        execution: AgentExecutionResult,
    ) -> Optional[FailureClassification]:
        if spec.provider_id is None:
            return None
        documents = list(self._json_documents(execution.stderr))
        if self._structured_stdout_enabled(spec):
            documents.extend(self._json_documents(execution.stdout))
        classifications = []
        for document in documents[:MAX_STRUCTURED_FAILURE_DOCUMENTS]:
            classification = self._classify_structured_document(
                spec.provider_id,
                document,
            )
            if classification is not None:
                classifications.append(classification)
        if not classifications:
            return None
        # A fail-closed category always wins if a provider emits conflicting events.
        for category in ("authentication", "overloaded", "quota_exhausted", "rate_limited"):
            for classification in classifications:
                if classification.category == category:
                    return classification
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
        if execution.return_code is None:
            return FailureClassification("unknown", False, "ambiguous-termination")
        if execution.return_code < 0:
            return FailureClassification("unknown", False, "process-signal-exit")
        if execution.return_code == 0:
            return FailureClassification("unknown", False, "inconsistent-terminal-status")

        provider_id = spec.provider_id
        if provider_id is None:
            return FailureClassification("unknown", False, "unrecognized-failure")

        # Authentication evidence always blocks, even if another event claims a retryable
        # failure. Only stderr is a valid free-text provider failure channel.
        authentication = self._match(
            execution.stderr,
            PROVIDER_AUTHENTICATION_PATTERNS.get(provider_id, ()),
        )
        if authentication is not None:
            return FailureClassification("authentication", False, authentication)

        structured = self._structured_classification(spec, execution)
        if structured is not None:
            return structured

        for category, patterns in (
            ("quota_exhausted", PROVIDER_QUOTA_PATTERNS.get(provider_id, ())),
            ("rate_limited", PROVIDER_RATE_LIMIT_PATTERNS.get(provider_id, ())),
            ("overloaded", PROVIDER_OVERLOAD_PATTERNS.get(provider_id, ())),
        ):
            evidence = self._match(execution.stderr, patterns)
            if evidence is not None:
                return FailureClassification(
                    category,
                    category in {"quota_exhausted", "rate_limited"},
                    evidence,
                )

        return FailureClassification("unknown", False, "unrecognized-failure")
