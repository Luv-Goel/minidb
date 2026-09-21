"""Abstract syntax tree for minidb's SQL dialect."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

# --------------------------------------------------------------------- #
# expressions
# --------------------------------------------------------------------- #

class Expr:
    pass


@dataclass
class Literal(Expr):
    value: Any                      # int | float | str | None


@dataclass
class Column(Expr):
    name: str                       # 'id' or 'users.id'
    table: Optional[str] = None     # set for qualified refs


@dataclass
class BinOp(Expr):
    op: str                         # =, !=, <, <=, >, >=, +, -, *, /, %, ||
    left: Expr
    right: Expr


@dataclass
class And(Expr):
    left: Expr
    right: Expr


@dataclass
class Or(Expr):
    left: Expr
    right: Expr


@dataclass
class Not(Expr):
    operand: Expr


@dataclass
class IsNull(Expr):
    operand: Expr
    negated: bool = False


@dataclass
class Like(Expr):
    operand: Expr
    pattern: Expr
    negated: bool = False


@dataclass
class In(Expr):
    operand: Expr
    options: list[Expr]
    negated: bool = False


@dataclass
class Between(Expr):
    operand: Expr
    low: Expr
    high: Expr
    negated: bool = False


@dataclass
class FuncCall(Expr):
    name: str                       # count, sum, avg, min, max, length, ...
    args: list[Expr]


AGGREGATES = {"count", "sum", "avg", "min", "max"}


# --------------------------------------------------------------------- #
# statements
# --------------------------------------------------------------------- #

class Statement:
    pass


@dataclass
class CreateTable(Statement):
    table: str
    columns: list[tuple[str, str]]  # (name, 'INTEGER'|'TEXT')
    if_not_exists: bool = False


@dataclass
class DropTable(Statement):
    table: str
    if_exists: bool = False


@dataclass
class Insert(Statement):
    table: str
    columns: Optional[list[str]]           # None -> positional VALUES
    rows: list[list[Expr]]                 # each a VALUES tuple


@dataclass
class SelectItem:
    expr: Expr
    alias: Optional[str] = None


@dataclass
class TableRef:
    name: str
    alias: Optional[str] = None

    @property
    def effective(self) -> str:
        return self.alias or self.name


@dataclass
class Join:
    table: TableRef
    on: Expr


@dataclass
class Select(Statement):
    columns: list[SelectItem]              # [SelectItem(Column('*'))] for *
    from_table: TableRef
    joins: list[Join] = field(default_factory=list)
    where: Optional[Expr] = None
    group_by: list[Expr] = field(default_factory=list)
    having: Optional[Expr] = None
    order_by: list[tuple[Expr, str]] = field(default_factory=list)  # (expr,'asc'|'desc')
    limit: Optional[int] = None
    offset: int = 0
    distinct: bool = False


@dataclass
class Update(Statement):
    table: str
    assignments: list[tuple[str, Expr]]
    where: Optional[Expr] = None


@dataclass
class Delete(Statement):
    table: str
    where: Optional[Expr] = None


@dataclass
class Begin(Statement):
    pass


@dataclass
class Commit(Statement):
    pass


@dataclass
class Rollback(Statement):
    pass


# --------------------------------------------------------------------- #
# rendering (for output column labels)
# --------------------------------------------------------------------- #

def sql_repr(node: Expr) -> str:
    if isinstance(node, Literal):
        if node.value is None:
            return "NULL"
        if isinstance(node.value, str):
            return f"'{node.value}'"
        return str(node.value)
    if isinstance(node, Column):
        return f"{node.table}.{node.name}" if node.table else node.name
    if isinstance(node, BinOp):
        return f"{sql_repr(node.left)} {node.op} {sql_repr(node.right)}"
    if isinstance(node, And):
        return f"({sql_repr(node.left)} AND {sql_repr(node.right)})"
    if isinstance(node, Or):
        return f"({sql_repr(node.left)} OR {sql_repr(node.right)})"
    if isinstance(node, Not):
        return f"NOT {sql_repr(node.operand)}"
    if isinstance(node, IsNull):
        return f"{sql_repr(node.operand)} IS {'NOT ' if node.negated else ''}NULL"
    if isinstance(node, Like):
        return f"{sql_repr(node.operand)} {'NOT ' if node.negated else ''}LIKE {sql_repr(node.pattern)}"
    if isinstance(node, In):
        inner = ", ".join(sql_repr(o) for o in node.options)
        return f"{sql_repr(node.operand)} {'NOT ' if node.negated else ''}IN ({inner})"
    if isinstance(node, Between):
        return (f"{sql_repr(node.operand)} {'NOT ' if node.negated else ''}BETWEEN "
                f"{sql_repr(node.low)} AND {sql_repr(node.high)}")
    if isinstance(node, FuncCall):
        return f"{node.name.upper()}({', '.join(sql_repr(a) for a in node.args)})"
    return repr(node)
