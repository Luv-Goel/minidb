"""minidb exception hierarchy."""


class DatabaseError(Exception):
    """Base class for all minidb errors."""


class ParseError(DatabaseError):
    """Raised for malformed SQL."""

    def __init__(self, message: str, position: int | None = None):
        self.position = position
        if position is not None:
            message = f"{message} (at position {position})"
        super().__init__(message)


class ExecutionError(DatabaseError):
    """Raised for semantic/runtime errors (unknown table, type mismatch, ...)."""


class SchemaError(DatabaseError):
    """Raised for DDL problems (duplicate table, unknown column, ...)."""
