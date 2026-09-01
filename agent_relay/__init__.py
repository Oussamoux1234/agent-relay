"""Agent Relay: portable checkpoints and safe handoffs between AI agents."""

from .adapters import AdapterRegistry, AntigravityCliAdapter
from .failures import FailureClassification, FailureClassifier
from .health import AgentHealthRecord, CooldownPolicy, StructuredRetryHintParser
from .models import AgentSpec, StructuredAgentResult, TaskCheckpoint, TaskState
from .results import StructuredResultExtractor
from .service import RelayService, ResultPreview, RouteAttempt, RouteOutcome, RouteStatus
from .storage import RelayStore

__all__ = [
    "AdapterRegistry",
    "AntigravityCliAdapter",
    "AgentSpec",
    "AgentHealthRecord",
    "CooldownPolicy",
    "FailureClassification",
    "FailureClassifier",
    "RelayService",
    "RelayStore",
    "ResultPreview",
    "RouteAttempt",
    "RouteOutcome",
    "RouteStatus",
    "StructuredAgentResult",
    "StructuredRetryHintParser",
    "StructuredResultExtractor",
    "TaskCheckpoint",
    "TaskState",
]
__version__ = "0.5.0"
