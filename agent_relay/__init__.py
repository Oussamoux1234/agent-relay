"""Agent Relay: portable checkpoints and safe handoffs between AI agents."""

from .adapters import AdapterRegistry, AntigravityCliAdapter
from .failures import FailureClassification, FailureClassifier
from .models import AgentSpec, TaskCheckpoint, TaskState
from .service import RelayService, RouteAttempt, RouteOutcome
from .storage import RelayStore

__all__ = [
    "AdapterRegistry",
    "AntigravityCliAdapter",
    "AgentSpec",
    "FailureClassification",
    "FailureClassifier",
    "RelayService",
    "RelayStore",
    "RouteAttempt",
    "RouteOutcome",
    "TaskCheckpoint",
    "TaskState",
]
__version__ = "0.3.0"
