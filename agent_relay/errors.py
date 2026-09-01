"""Domain errors exposed by Agent Relay."""


class RelayError(Exception):
    """Base class for errors that are safe to present to a CLI user."""


class ValidationError(RelayError):
    """Raised when a public input does not satisfy the Relay contract."""


class NotFoundError(RelayError):
    """Raised when an agent, task, or action cannot be found."""


class ConflictError(RelayError):
    """Raised when state changed concurrently or an operation is unsafe."""


class ExecutionError(RelayError):
    """Raised when an agent process cannot be prepared or executed."""
