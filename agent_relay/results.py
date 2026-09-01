"""Strict structured-result extraction and redaction-safe audit helpers."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Set

from .errors import ValidationError
from .models import RESULT_SCHEMA_VERSION, StructuredAgentResult


RESULT_BEGIN_MARKER = "<<<AGENT_RELAY_RESULT>>>"
RESULT_END_MARKER = "<<<END_AGENT_RELAY_RESULT>>>"
MAX_RESULT_SOURCE_BYTES = 128 * 1024
MAX_RESULT_SEGMENTS = 16
MAX_JSON_NODES = 1_000
MAX_JSON_DEPTH = 8


@dataclass(frozen=True)
class ResultExtraction:
    """A proposal extraction result that never contains raw provider output."""

    status: str
    result: Optional[StructuredAgentResult] = None
    error_code: Optional[str] = None


def result_contract(task_id: str, source_action_id: str) -> Dict[str, Any]:
    """Return the exact response envelope requested from an agent."""

    return {
        "instruction": (
            "End your final answer with exactly one result envelope using the markers below. "
            "Put one JSON object matching the schema between the markers. Include only verified "
            "task facts, never hidden reasoning, credentials, or secrets."
        ),
        "begin_marker": RESULT_BEGIN_MARKER,
        "end_marker": RESULT_END_MARKER,
        "schema": {
            "schema_version": RESULT_SCHEMA_VERSION,
            "task_id": task_id,
            "source_action_id": source_action_id,
            "summary": "required non-empty string",
            "decisions": [],
            "constraints": [],
            "files_changed": [],
            "tests": [],
            "next_steps": [],
        },
    }


def result_digest(result: StructuredAgentResult) -> str:
    """Hash a canonical proposal for audit without retaining duplicate content."""

    encoded = json.dumps(
        result.to_dict(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class StructuredResultExtractor:
    """Find one action-bound result in plain, JSON, or JSON-lines provider output."""

    @staticmethod
    def _walk_strings(value: Any) -> Iterable[str]:
        stack = [(value, 0)]
        visited = 0
        while stack and visited < MAX_JSON_NODES:
            current, depth = stack.pop()
            visited += 1
            if isinstance(current, str):
                yield current
            elif depth < MAX_JSON_DEPTH and isinstance(current, dict):
                stack.extend((item, depth + 1) for item in current.values())
            elif depth < MAX_JSON_DEPTH and isinstance(current, list):
                stack.extend((item, depth + 1) for item in current)

    @staticmethod
    def _decode_json(value: str) -> Optional[Any]:
        try:
            return json.loads(value)
        except (json.JSONDecodeError, RecursionError, UnicodeError):
            return None

    def _candidate_texts(self, stdout: str) -> Iterable[str]:
        yield stdout
        decoded = self._decode_json(stdout)
        if decoded is not None:
            yield from self._walk_strings(decoded)
            return
        for line in stdout.splitlines():
            decoded_line = self._decode_json(line)
            if decoded_line is not None:
                yield from self._walk_strings(decoded_line)

    @staticmethod
    def _segments(text: str) -> Iterable[str]:
        offset = 0
        count = 0
        while count < MAX_RESULT_SEGMENTS:
            start = text.find(RESULT_BEGIN_MARKER, offset)
            if start < 0:
                return
            content_start = start + len(RESULT_BEGIN_MARKER)
            end = text.find(RESULT_END_MARKER, content_start)
            if end < 0:
                return
            yield text[content_start:end].strip()
            offset = end + len(RESULT_END_MARKER)
            count += 1

    def extract(
        self,
        stdout: str,
        task_id: str,
        source_action_id: str,
    ) -> ResultExtraction:
        if not isinstance(stdout, str):
            return ResultExtraction("invalid", error_code="non-text-output")
        try:
            source_size = len(stdout.encode("utf-8"))
        except UnicodeEncodeError:
            return ResultExtraction("invalid", error_code="invalid-unicode")
        if source_size > MAX_RESULT_SOURCE_BYTES:
            return ResultExtraction("invalid", error_code="output-too-large")

        saw_begin_marker = False
        saw_end_marker = False
        saw_json_error = False
        saw_schema_error = False
        saw_task_mismatch = False
        saw_action_mismatch = False
        valid_results: Dict[str, StructuredAgentResult] = {}
        seen_segments: Set[str] = set()

        for candidate in self._candidate_texts(stdout):
            saw_begin_marker = saw_begin_marker or RESULT_BEGIN_MARKER in candidate
            saw_end_marker = saw_end_marker or RESULT_END_MARKER in candidate
            for segment in self._segments(candidate):
                if segment in seen_segments:
                    continue
                seen_segments.add(segment)
                decoded = self._decode_json(segment)
                if not isinstance(decoded, dict):
                    saw_json_error = True
                    continue
                try:
                    result = StructuredAgentResult.from_dict(decoded)
                except ValidationError:
                    saw_schema_error = True
                    continue
                if result.task_id != task_id:
                    saw_task_mismatch = True
                    continue
                if result.source_action_id != source_action_id:
                    saw_action_mismatch = True
                    continue
                digest = result_digest(result)
                valid_results[digest] = result

        if len(valid_results) == 1:
            return ResultExtraction("ready", result=next(iter(valid_results.values())))
        if len(valid_results) > 1:
            return ResultExtraction("invalid", error_code="ambiguous-results")
        if not saw_begin_marker and not saw_end_marker:
            return ResultExtraction("missing", error_code="result-envelope-missing")
        if saw_begin_marker != saw_end_marker:
            return ResultExtraction("invalid", error_code="malformed-markers")

        error_codes: List[str] = []
        if saw_task_mismatch:
            error_codes.append("task-mismatch")
        if saw_action_mismatch:
            error_codes.append("action-mismatch")
        if saw_schema_error:
            error_codes.append("invalid-schema")
        if saw_json_error:
            error_codes.append("invalid-json")
        return ResultExtraction(
            "invalid",
            error_code=error_codes[0] if error_codes else "empty-envelope",
        )
