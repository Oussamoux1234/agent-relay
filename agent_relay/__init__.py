"""Agent Relay: portable checkpoints and safe handoffs between AI agents."""

from .adapters import AdapterRegistry, AntigravityCliAdapter
from .models import AgentSpec, TaskCheckpoint, TaskState
from .service import RelayService
from .storage import RelayStore

__all__ = [
    "AdapterRegistry",
    "AntigravityCliAdapter",
    "AgentSpec",
    "RelayService",
    "RelayStore",
    "TaskCheckpoint",
    "TaskState",
]
__version__ = "0.2.0"
