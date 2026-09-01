"""Agent Relay: portable checkpoints and safe handoffs between AI agents."""

from .adapters import AdapterRegistry, AntigravityCliAdapter
from .failures import FailureClassification, FailureClassifier
from .models import AgentSpec, StructuredAgentResult, TaskCheckpoint, TaskState
from .results import StructuredResultExtractor
from .service import RelayService, ResultPreview, RouteAttempt, RouteOutcome
from .storage import RelayStore

__all__ = [
    "AdapterRegistry",
    "AntigravityCliAdapter",
    "AgentSpec",
    "FailureClassification",
    "FailureClassifier",
    "RelayService",
    "RelayStore",
    "ResultPreview",
    "RouteAttempt",
    "RouteOutcome",
    "StructuredAgentResult",
    "StructuredResultExtractor",
    "TaskCheckpoint",
    "TaskState",
]
__version__ = "0.4.0"
