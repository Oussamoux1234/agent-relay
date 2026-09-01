"""Redacted provider health records and conservative cooldown policy."""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .adapters import AgentExecutionResult
from .errors import ValidationError
from .failures import FailureClassification
from .models import AGENT_ID_PATTERN, AgentSpec


HEALTH_SCHEMA_VERSION = "1.0"
COOLDOWN_CATEGORIES = frozenset(("quota_exhausted", "rate_limited", "unavailable"))
RETRY_SOURCES = frozenset(("provider_hint", "default_policy", "manual_recovery"))
DEFAULT_RATE_LIMIT_SECONDS = 60
DEFAULT_UNAVAILABLE_SECONDS = 300
MAX_PROVIDER_HINT_SECONDS = 31_536_000
MAX_HEALTH_RECORDS = 1_000
_PROTOBUF_DURATION = re.compile(r"^(?P<seconds>[0-9]+)(?:\.(?P<fraction>[0-9]{1,9}))?s$")


def utc_datetime_now() -> datetime:
    """Return an aware UTC datetime for cooldown decisions."""

    return datetime.now(timezone.utc)


def format_utc(value: datetime) -> str:
    """Serialize an aware datetime as a stable UTC timestamp."""

    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ValidationError("cooldown timestamps must be timezone-aware datetimes")
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(value: Any, field_name: str) -> datetime:
    """Parse and validate one persisted timezone-qualified timestamp."""

    if not isinstance(value, str) or not value or len(value) > 64:
        raise ValidationError("%s must be a timezone-qualified timestamp" % field_name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("%s must be a valid timestamp" % field_name) from exc
    if parsed.tzinfo is None:
        raise ValidationError("%s must include a timezone" % field_name)
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class RetryHint:
    """A documented machine-readable provider delay, never raw provider text."""

    delay_seconds: Optional[int]
    signal_code: str


@dataclass(frozen=True)
class AgentHealthRecord:
    """A redaction-safe cooldown for one configured provider instance."""

    agent_id: str
    provider_id: str
    category: str
    evidence_code: str
    retry_source: str
    observed_at: str
    cooldown_until: Optional[str]
    source_task_id: str
    source_action_id: str
    retry_signal_code: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.agent_id, str) or AGENT_ID_PATTERN.fullmatch(self.agent_id) is None:
            raise ValidationError("health agent_id is invalid")
        if not isinstance(self.provider_id, str) or AGENT_ID_PATTERN.fullmatch(self.provider_id) is None:
            raise ValidationError("health provider_id is invalid")
        if self.category not in COOLDOWN_CATEGORIES:
            raise ValidationError("health category is invalid")
        if self.retry_source not in RETRY_SOURCES:
            raise ValidationError("health retry_source is invalid")
        for value, field_name, maximum in (
            (self.evidence_code, "health evidence_code", 120),
            (self.source_task_id, "health source_task_id", 64),
            (self.source_action_id, "health source_action_id", 64),
        ):
            if not isinstance(value, str) or not value or len(value) > maximum:
                raise ValidationError("%s is invalid" % field_name)
        if not self.source_task_id.isalnum():
            raise ValidationError("health source_task_id is invalid")
        parse_utc(self.observed_at, "health observed_at")
        if self.cooldown_until is not None:
            if parse_utc(self.cooldown_until, "health cooldown_until") <= parse_utc(
                self.observed_at,
                "health observed_at",
            ):
                raise ValidationError("health cooldown_until must be after observed_at")
        if self.retry_source == "manual_recovery" and self.cooldown_until is not None:
            raise ValidationError("manual recovery health must not have cooldown_until")
        if self.retry_source != "manual_recovery" and self.cooldown_until is None:
            raise ValidationError("finite cooldown health requires cooldown_until")
        if self.retry_signal_code is not None:
            if (
                not isinstance(self.retry_signal_code, str)
                or not self.retry_signal_code
                or len(self.retry_signal_code) > 120
            ):
                raise ValidationError("health retry_signal_code is invalid")

    def is_active(self, observed_at: datetime) -> bool:
        if not isinstance(observed_at, datetime) or observed_at.tzinfo is None:
            raise ValidationError("health observation time must be timezone-aware")
        if self.cooldown_until is None:
            return True
        return parse_utc(self.cooldown_until, "health cooldown_until") > observed_at.astimezone(
            timezone.utc
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "provider_id": self.provider_id,
            "category": self.category,
            "evidence_code": self.evidence_code,
            "retry_source": self.retry_source,
            "retry_signal_code": self.retry_signal_code,
            "observed_at": self.observed_at,
            "cooldown_until": self.cooldown_until,
            "source_task_id": self.source_task_id,
            "source_action_id": self.source_action_id,
        }

    def to_status_dict(self, observed_at: datetime) -> Dict[str, Any]:
        value = self.to_dict()
        active = self.is_active(observed_at)
        value["active"] = active
        if self.cooldown_until is None:
            value["remaining_seconds"] = None
            value["recovery"] = "clear health, then recover the route if this is an earlier entry"
        else:
            remaining = (
                parse_utc(self.cooldown_until, "health cooldown_until")
                - observed_at.astimezone(timezone.utc)
            ).total_seconds()
            value["remaining_seconds"] = max(0, int(math.ceil(remaining)))
            value["recovery"] = "automatic when cooldown expires"
        return value

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "AgentHealthRecord":
        if not isinstance(value, dict):
            raise ValidationError("health record must be an object")
        allowed_fields = {
            "agent_id",
            "provider_id",
            "category",
            "evidence_code",
            "retry_source",
            "retry_signal_code",
            "observed_at",
            "cooldown_until",
            "source_task_id",
            "source_action_id",
        }
        unknown_fields = sorted(set(value).difference(allowed_fields))
        if unknown_fields:
            raise ValidationError(
                "health record contains unknown fields: %s" % ", ".join(unknown_fields)
            )
        return cls(
            agent_id=value.get("agent_id"),
            provider_id=value.get("provider_id"),
            category=value.get("category"),
            evidence_code=value.get("evidence_code"),
            retry_source=value.get("retry_source"),
            retry_signal_code=value.get("retry_signal_code"),
            observed_at=value.get("observed_at"),
            cooldown_until=value.get("cooldown_until"),
            source_task_id=value.get("source_task_id"),
            source_action_id=value.get("source_action_id"),
        )


class StructuredRetryHintParser:
    """Read only documented retry fields from complete JSON or JSON-lines output."""

    _GOOGLE_TYPE_SUFFIX = "google.rpc.RetryInfo"

    @staticmethod
    def _json_documents(outputs: Iterable[str]) -> Tuple[Any, ...]:
        documents: List[Any] = []
        seen = set()
        for output in outputs:
            if not isinstance(output, str) or not output.strip():
                continue
            candidates = [output.strip()]
            candidates.extend(line.strip() for line in output.splitlines() if line.strip())
            for candidate in candidates:
                if candidate in seen:
                    continue
                seen.add(candidate)
                try:
                    documents.append(json.loads(candidate))
                except (json.JSONDecodeError, RecursionError):
                    continue
        return tuple(documents)

    @staticmethod
    def _seconds(value: Any, protobuf_duration: bool = False) -> Optional[int]:
        if isinstance(value, bool):
            return None
        seconds: Optional[float]
        if isinstance(value, (int, float)):
            seconds = float(value)
        elif isinstance(value, str):
            if protobuf_duration:
                match = _PROTOBUF_DURATION.fullmatch(value.strip())
                if match is None:
                    return None
                seconds = float(match.group("seconds"))
                fraction = match.group("fraction")
                if fraction:
                    seconds += float("0." + fraction)
            else:
                try:
                    seconds = float(value.strip())
                except ValueError:
                    return None
        else:
            return None
        if not math.isfinite(seconds) or seconds <= 0:
            return None
        return int(math.ceil(seconds))

    def parse(self, execution: AgentExecutionResult) -> Optional[RetryHint]:
        candidates: List[Tuple[int, str]] = []
        for document in self._json_documents((execution.stdout, execution.stderr)):
            stack = [(document, True)]
            visited = 0
            while stack and visited < 10_000:
                node, is_header_container = stack.pop()
                visited += 1
                if isinstance(node, dict):
                    for key, value in node.items():
                        if (
                            is_header_container
                            and isinstance(key, str)
                            and key.lower() == "retry-after"
                        ):
                            seconds = self._seconds(value)
                            if seconds is not None:
                                candidates.append((seconds, "retry-after-seconds"))
                    type_value = node.get("@type")
                    if (
                        isinstance(type_value, str)
                        and type_value.endswith(self._GOOGLE_TYPE_SUFFIX)
                    ):
                        for key in ("retryDelay", "retry_delay"):
                            if key in node:
                                seconds = self._seconds(node[key], protobuf_duration=True)
                                if seconds is not None:
                                    candidates.append(
                                        (seconds, "google.rpc.RetryInfo.retry_delay")
                                    )
                    for key, value in node.items():
                        child_is_header_container = (
                            isinstance(key, str)
                            and key.lower() in {"headers", "response_headers"}
                        )
                        stack.append((value, child_is_header_container))
                elif isinstance(node, list):
                    stack.extend((value, False) for value in node)
        if not candidates:
            return None
        delay_seconds, signal_code = max(candidates, key=lambda item: (item[0], item[1]))
        if delay_seconds > MAX_PROVIDER_HINT_SECONDS:
            return RetryHint(None, signal_code + "-over-bound")
        return RetryHint(delay_seconds, signal_code)


class CooldownPolicy:
    """Create finite or manual cooldowns without retrying inside the relay."""

    def __init__(self, hint_parser: Optional[StructuredRetryHintParser] = None) -> None:
        self.hint_parser = hint_parser or StructuredRetryHintParser()

    def create_record(
        self,
        spec: AgentSpec,
        classification: FailureClassification,
        execution: AgentExecutionResult,
        task_id: str,
        action_id: str,
        observed_at: datetime,
    ) -> Optional[AgentHealthRecord]:
        if classification.category not in COOLDOWN_CATEGORIES:
            return None
        if spec.provider_id is None:
            raise ValidationError("cooldown records require a provider_id")

        hint = self.hint_parser.parse(execution)
        retry_signal_code = hint.signal_code if hint is not None else None
        if hint is not None and hint.delay_seconds is not None:
            retry_source = "provider_hint"
            cooldown_until = format_utc(observed_at + timedelta(seconds=hint.delay_seconds))
        elif hint is not None or classification.category == "quota_exhausted":
            retry_source = "manual_recovery"
            cooldown_until = None
        else:
            retry_source = "default_policy"
            default_seconds = (
                DEFAULT_UNAVAILABLE_SECONDS
                if classification.category == "unavailable"
                else DEFAULT_RATE_LIMIT_SECONDS
            )
            cooldown_until = format_utc(observed_at + timedelta(seconds=default_seconds))

        return AgentHealthRecord(
            agent_id=spec.agent_id,
            provider_id=spec.provider_id,
            category=classification.category,
            evidence_code=classification.evidence_code,
            retry_source=retry_source,
            retry_signal_code=retry_signal_code,
            observed_at=format_utc(observed_at),
            cooldown_until=cooldown_until,
            source_task_id=task_id,
            source_action_id=action_id,
        )
