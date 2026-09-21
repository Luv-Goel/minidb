"""Recursive-descent parser: tokens -> AST.

Grammar (informal):

    statement := create | drop | insert | select | update | delete
               | begin | commit | rollback
    select    := SELECT [DISTINCT] select_list FROM table [JOIN table ON expr]*
                 [WHERE expr] [GROUP BY expr_list] [HAVING expr]
                 [ORDER BY expr [ASC|DESC] (, ...)*] [LIMIT n [OFFSET n]]
    expr      := or_expr
    or_expr   := and_expr (OR and_expr)*
    and_expr  := not_expr (AND not_expr)*
    not_expr  := NOT not_expr | predicate
    predicate := comparison ((NOT)? (LIKE|IN|BETWEEN|IS NULL) ...)?
    comparison:= additive ((=|!=|<|<=|>|>=) additive)?
    additive  := term ((+|-) term)*
    term      := factor ((*|/|%) factor)*
    factor    := literal | column | func_call | '(' expr ')' | '-' factor
"""

from __future__ import annotations

from . import ast
from .errors import ParseError
from .lexer import Token, tokenize


class Parser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.i = 0

    # ---------------- token helpers ---------------- #

    def peek(self, offset: int = 0) -> Token:
        return self.tokens[min(self.i + offset, len(self.tokens) - 1)]

    def advance(self) -> Token:
        tok = self.tokens[self.i]
        if tok.kind != "eof":
            self.i += 1
        return tok

    def at_keyword(self, *words: str) -> bool:
        tok = self.peek()
        return tok.kind == "keyword" and tok.text.lower() in words

    def at_op(self, *ops: str) -> bool:
        tok = self.peek()
        return tok.kind == "op" and tok.text in ops

    def at_punct(self, *p: str) -> bool:
        tok = self.peek()
        return tok.kind == "punct" and tok.text in p

    def expect_keyword(self, word: str) -> Token:
        if not self.at_keyword(word):
            raise ParseError(f"expected {word.upper()}, got "
                             f"{self.peek().text or 'end of input'!r}",
                             self.peek().pos)
        return self.advance()

    def expect_punct(self, p: str) -> Token:
        if not self.at_punct(p):
            raise ParseError(f"expected {p!r}", self.peek().pos)
        return self.advance()

    def expect_ident(self) -> str:
        tok = self.peek()
        if tok.kind not in ("ident", "keyword"):
            raise ParseError(f"expected identifier, got {tok.text!r}", tok.pos)
        self.advance()
        return tok.text

    # ---------------- entry point ---------------- #

    def parse_statement(self) -> ast.Statement:
        tok = self.peek()
        if tok.kind != "keyword":
            raise ParseError(f"expected a statement, got {tok.text!r}", tok.pos)
        kw = tok.text.lower()
        if kw == "create":
            stmt = self._parse_create()
        elif kw == "drop":
            stmt = self._parse_drop()
        elif kw == "insert":
            stmt = self._parse_insert()
        elif kw == "select":
            stmt = self._parse_select()
        elif kw == "update":
            stmt = self._parse_update()
        elif kw == "delete":
            stmt = self._parse_delete()
        elif kw == "begin":
            self.advance()
            stmt = ast.Begin()
        elif kw == "commit":
            self.advance()
            stmt = ast.Commit()
        elif kw == "rollback":
            self.advance()
            stmt = ast.Rollback()
        else:
            raise ParseError(f"unsupported statement {tok.text!r}", tok.pos)
        # optional trailing semicolon(s)
        while self.at_punct(";"):
            self.advance()
        if self.peek().kind != "eof":
            raise ParseError(f"unexpected trailing input {self.peek().text!r}",
                             self.peek().pos)
        return stmt

    # ---------------- DDL / DML ---------------- #

    def _parse_create(self) -> ast.CreateTable:
        self.expect_keyword("create")
        self.expect_keyword("table")
        if_not_exists = False
        if self.at_keyword("if"):
            self.advance()
            self.expect_keyword("not")
            self.expect_keyword("exists")
            if_not_exists = True
        name = self.expect_ident()
        self.expect_punct("(")
        columns = []
        while True:
            col_name = self.expect_ident()
            type_tok = self.advance()
            if (type_tok.kind != "keyword"
                    or type_tok.text.lower() not in ("integer", "text")):
                raise ParseError(
                    f"expected column type INTEGER or TEXT, got "
                    f"{type_tok.text!r}", type_tok.pos)
            columns.append((col_name, type_tok.text.upper()))
            if self.at_punct(","):
                self.advance()
                continue
            break
        self.expect_punct(")")
        return ast.CreateTable(name, columns, if_not_exists)

    def _parse_drop(self) -> ast.DropTable:
        self.expect_keyword("drop")
        self.expect_keyword("table")
        if_exists = False
        if self.at_keyword("if"):
            self.advance()
            self.expect_keyword("exists")
            if_exists = True
        return ast.DropTable(self.expect_ident(), if_exists)

    def _parse_insert(self) -> ast.Insert:
        self.expect_keyword("insert")
        self.expect_keyword("into")
        table = self.expect_ident()
        columns = None
        if self.at_punct("("):
            self.advance()
            columns = [self.expect_ident()]
            while self.at_punct(","):
                self.advance()
                columns.append(self.expect_ident())
            self.expect_punct(")")
        self.expect_keyword("values")
        rows = []
        while True:
            self.expect_punct("(")
            row = [self._parse_expr()]
            while self.at_punct(","):
                self.advance()
                row.append(self._parse_expr())
            self.expect_punct(")")
            rows.append(row)
            if self.at_punct(","):
                self.advance()
                continue
            break
        return ast.Insert(table, columns, rows)

    def _parse_select(self) -> ast.Select:
        self.expect_keyword("select")
        distinct = False
        if self.at_keyword("distinct"):
            self.advance()
            distinct = True
        items = []
        while True:
            if self.at_op("*"):
                self.advance()
                expr: ast.Expr = ast.Column("*")
            else:
                expr = self._parse_expr()
            alias = None
            if self.at_keyword("as"):
                self.advance()
                alias = self.expect_ident()
            elif self.peek().kind == "ident":
                alias = self.advance().text
            items.append(ast.SelectItem(expr, alias))
            if self.at_punct(","):
                self.advance()
                continue
            break
        self.expect_keyword("from")
        table = self._parse_table_ref()

        joins = []
        while True:
            if self.at_keyword("inner"):
                self.advance()
                self.expect_keyword("join")
            elif self.at_keyword("join"):
                self.advance()
            else:
                break
            join_table = self._parse_table_ref()
            self.expect_keyword("on")
            joins.append(ast.Join(join_table, self._parse_expr()))

        where = None
        if self.at_keyword("where"):
            self.advance()
            where = self._parse_expr()

        group_by: list[ast.Expr] = []
        if self.at_keyword("group"):
            self.advance()
            self.expect_keyword("by")
            group_by.append(self._parse_expr())
            while self.at_punct(","):
                self.advance()
                group_by.append(self._parse_expr())

        having = None
        if self.at_keyword("having"):
            self.advance()
            having = self._parse_expr()

        order_by: list[tuple[ast.Expr, str]] = []
        if self.at_keyword("order"):
            self.advance()
            self.expect_keyword("by")
            while True:
                expr = self._parse_expr()
                direction = "asc"
                if self.at_keyword("asc"):
                    self.advance()
                elif self.at_keyword("desc"):
                    self.advance()
                    direction = "desc"
                order_by.append((expr, direction))
                if self.at_punct(","):
                    self.advance()
                    continue
                break

        limit = None
        offset = 0
        if self.at_keyword("limit"):
            self.advance()
            tok = self.advance()
            if tok.kind != "number":
                raise ParseError("LIMIT expects an integer", tok.pos)
            limit = int(tok.text)
        if self.at_keyword("offset"):
            self.advance()
            tok = self.advance()
            if tok.kind != "number":
                raise ParseError("OFFSET expects an integer", tok.pos)
            offset = int(tok.text)

        return ast.Select(items, table, joins, where, group_by, having,
                          order_by, limit, offset, distinct)

    def _parse_table_ref(self) -> ast.TableRef:
        name = self.expect_ident()
        alias = None
        if self.at_keyword("as"):
            self.advance()
            alias = self.expect_ident()
        elif self.peek().kind == "ident":
            alias = self.advance().text
        return ast.TableRef(name, alias)

    def _parse_update(self) -> ast.Update:
        self.expect_keyword("update")
        table = self.expect_ident()
        self.expect_keyword("set")
        assignments = []
        while True:
            col = self.expect_ident()
            if not self.at_op("="):
                raise ParseError("expected '=' in SET clause", self.peek().pos)
            self.advance()
            assignments.append((col, self._parse_expr()))
            if self.at_punct(","):
                self.advance()
                continue
            break
        where = None
        if self.at_keyword("where"):
            self.advance()
            where = self._parse_expr()
        return ast.Update(table, assignments, where)

    def _parse_delete(self) -> ast.Delete:
        self.expect_keyword("delete")
        self.expect_keyword("from")
        table = self.expect_ident()
        where = None
        if self.at_keyword("where"):
            self.advance()
            where = self._parse_expr()
        return ast.Delete(table, where)

    # ---------------- expressions (precedence climbing) ---------------- #

    def _parse_expr(self) -> ast.Expr:
        return self._parse_or()

    def _parse_or(self) -> ast.Expr:
        left = self._parse_and()
        while self.at_keyword("or"):
            self.advance()
            left = ast.Or(left, self._parse_and())
        return left

    def _parse_and(self) -> ast.Expr:
        left = self._parse_not()
        while self.at_keyword("and"):
            self.advance()
            left = ast.And(left, self._parse_not())
        return left

    def _parse_not(self) -> ast.Expr:
        if self.at_keyword("not"):
            self.advance()
            return ast.Not(self._parse_not())
        return self._parse_predicate()

    def _parse_predicate(self) -> ast.Expr:
        left = self._parse_comparison()
        while True:
            negated = False
            if self.at_keyword("not"):
                self.advance()
                negated = True
            if self.at_keyword("like"):
                self.advance()
                left = ast.Like(left, self._parse_comparison(), negated)
            elif self.at_keyword("between"):
                self.advance()
                low = self._parse_comparison()
                self.expect_keyword("and")
                high = self._parse_comparison()
                left = ast.Between(left, low, high, negated)
            elif self.at_keyword("in"):
                self.advance()
                self.expect_punct("(")
                options = [self._parse_expr()]
                while self.at_punct(","):
                    self.advance()
                    options.append(self._parse_expr())
                self.expect_punct(")")
                left = ast.In(left, options, negated)
            elif self.at_keyword("is"):
                self.advance()
                not2 = False
                if self.at_keyword("not"):
                    self.advance()
                    not2 = True
                self.expect_keyword("null")
                left = ast.IsNull(left, not2)
            elif negated:
                raise ParseError("expected LIKE, IN, BETWEEN or IS after NOT",
                                 self.peek().pos)
            else:
                break
        return left

    def _parse_comparison(self) -> ast.Expr:
        left = self._parse_additive()
        if self.at_op("=", "!=", "<>", "<", "<=", ">", ">="):
            op = self.advance().text
            if op == "<>":
                op = "!="
            return ast.BinOp(op, left, self._parse_additive())
        return left

    def _parse_additive(self) -> ast.Expr:
        left = self._parse_term()
        while self.at_op("+", "-", "||"):
            op = self.advance().text
            left = ast.BinOp(op, left, self._parse_term())
        return left

    def _parse_term(self) -> ast.Expr:
        left = self._parse_factor()
        while self.at_op("*", "/", "%"):
            op = self.advance().text
            left = ast.BinOp(op, left, self._parse_factor())
        return left

    def _parse_factor(self) -> ast.Expr:
        tok = self.peek()
        if self.at_op("-"):
            self.advance()
            return ast.BinOp("-", ast.Literal(0), self._parse_factor())
        if self.at_op("+"):
            self.advance()
            return self._parse_factor()
        if tok.kind == "number":
            self.advance()
            return ast.Literal(float(tok.text) if "." in tok.text else int(tok.text))
        if tok.kind == "string":
            self.advance()
            return ast.Literal(tok.text)
        if self.at_keyword("null"):
            self.advance()
            return ast.Literal(None)
        if self.at_punct("("):
            self.advance()
            expr = self._parse_expr()
            self.expect_punct(")")
            return expr
        if tok.kind in ("ident", "keyword"):
            name = self.advance().text
            if self.at_punct("("):           # function call
                self.advance()
                args: list[ast.Expr] = []
                if not self.at_punct(")"):
                    if self.at_op("*"):
                        self.advance()
                        args.append(ast.Column("*"))
                    else:
                        args.append(self._parse_expr())
                    while self.at_punct(","):
                        self.advance()
                        args.append(self._parse_expr())
                self.expect_punct(")")
                return ast.FuncCall(name, args)
            if self.at_punct("."):           # qualified column t.col
                self.advance()
                col = self.advance()
                if col.kind not in ("ident", "keyword") and not (
                        col.kind == "op" and col.text == "*"):
                    raise ParseError("expected column name after '.'", col.pos)
                return ast.Column(col.text, table=name)
            return ast.Column(name)
        raise ParseError(f"unexpected token {tok.text!r}", tok.pos)


def parse(sql: str) -> ast.Statement:
    return Parser(tokenize(sql)).parse_statement()
