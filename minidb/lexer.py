"""SQL tokenizer.

Produces a flat list of typed tokens; keywords are matched
case-insensitively but identifiers keep their original case.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import ParseError

KEYWORDS = {
    "select", "from", "where", "insert", "into", "values", "update", "set",
    "delete", "create", "table", "drop", "begin", "commit", "rollback",
    "and", "or", "not", "null", "as", "join", "inner", "on", "group", "by",
    "having", "order", "asc", "desc", "limit", "offset", "is", "like",
    "integer", "text", "in", "between", "distinct", "if", "exists",
}


@dataclass
class Token:
    kind: str   # 'keyword' | 'ident' | 'number' | 'string' | 'op' | 'punct' | 'eof'
    text: str
    pos: int


OPS = ["<=", ">=", "!=", "<>", "||", "=", "<", ">", "+", "-", "*", "/", "%"]
PUNCT = {"(", ")", ",", ";", "."}


def tokenize(sql: str) -> list[Token]:
    tokens: list[Token] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in " \t\r\n":
            i += 1
            continue
        if c == "-" and sql[i:i + 2] == "--":          # line comment
            j = sql.find("\n", i)
            i = n if j == -1 else j + 1
            continue
        if c.isdigit():
            j = i
            while j < n and (sql[j].isdigit() or sql[j] == "."):
                j += 1
            tokens.append(Token("number", sql[i:j], i))
            i = j
            continue
        if c.isalpha() or c == "_":
            j = i
            while j < n and (sql[j].isalnum() or sql[j] == "_"):
                j += 1
            word = sql[i:j]
            kind = "keyword" if word.lower() in KEYWORDS else "ident"
            tokens.append(Token(kind, word, i))
            i = j
            continue
        if c == "'":
            j = i + 1
            chars = []
            while True:
                if j >= n:
                    raise ParseError("unterminated string literal", i)
                if sql[j] == "'":
                    if j + 1 < n and sql[j + 1] == "'":  # '' escape
                        chars.append("'")
                        j += 2
                        continue
                    break
                chars.append(sql[j])
                j += 1
            tokens.append(Token("string", "".join(chars), i))
            i = j + 1
            continue
        for op in OPS:
            if sql.startswith(op, i):
                tokens.append(Token("op", op, i))
                i += len(op)
                break
        else:
            if c in PUNCT:
                tokens.append(Token("punct", c, i))
                i += 1
            else:
                raise ParseError(f"unexpected character {c!r}", i)
    tokens.append(Token("eof", "", n))
    return tokens
