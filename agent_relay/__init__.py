"""Agent Relay: portable checkpoints and safe handoffs between AI agents."""

from .adapters import AdapterRegistry, AntigravityCliAdapter, SessionAgentAdapter
from .app_server import CodexAppServerAdapter
from .failures import FailureClassification, FailureClassifier
from .health import AgentHealthRecord, CooldownPolicy, StructuredRetryHintParser
from .models import AgentSpec, StructuredAgentResult, TaskCheckpoint, TaskState
from .results import StructuredResultExtractor
from .service import RelayService, ResultPreview, RouteAttempt, RouteOutcome, RouteStatus
from .storage import RelayStore
from .version import VERSION

__all__ = [
    "AdapterRegistry",
    "AntigravityCliAdapter",
    "AgentSpec",
    "AgentHealthRecord",
    "CooldownPolicy",
    "CodexAppServerAdapter",
    "FailureClassification",
    "FailureClassifier",
    "RelayService",
    "RelayStore",
    "ResultPreview",
    "RouteAttempt",
    "RouteOutcome",
    "RouteStatus",
    "SessionAgentAdapter",
    "StructuredAgentResult",
    "StructuredRetryHintParser",
    "StructuredResultExtractor",
    "TaskCheckpoint",
    "TaskState",
]
__version__ = VERSION
