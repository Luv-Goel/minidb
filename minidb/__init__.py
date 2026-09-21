"""minidb: a small but real SQL database engine."""

from .engine import Database, Result
from .errors import DatabaseError, ParseError, ExecutionError, SchemaError

__version__ = "1.0.0"
__all__ = ["Database", "Result", "DatabaseError", "ParseError",
           "ExecutionError", "SchemaError", "__version__"]
