from __future__ import annotations
from dataclasses import dataclass
import ast
import re

_PREFIX_CHARS = set("rRuUbBfF")

@dataclass(frozen=True)
class StringLiteral:
    start: int
    end: int
    line: int
    column: int
    raw: str
    value: str | None
    prefix: str
    quote: str
    triple: bool
    dynamic: bool

def _decode_literal(raw: str) -> tuple[str | None, bool]:
    # Python's parser is a useful decoder for ordinary/unicode/raw literals.
    # Ren'Py source can contain Python-2 `u` prefixes; Python 3 accepts them.
    try:
        v = ast.literal_eval(raw)
        if isinstance(v, str):
            return v, False
        if isinstance(v, bytes):
            try:
                return v.decode("utf-8"), False
            except Exception:
                return None, True
    except Exception:
        pass

    # f-strings are deliberately dynamic. Do not execute/evaluate them.
    if raw.lstrip("rRuUbB").lower().startswith("f"):
        return None, True
    return None, True

def renpy_unescape(value: str) -> str:
    """Decode script escapes, without interpreting Python expressions."""
    pieces = []
    i = 0
    escapes = {"{": "{{", "[": "[[", "%": "%%", "n": "\n", "u": ""}
    while i < len(value):
        if value[i] != "\\" or i + 1 == len(value):
            pieces.append(value[i])
            i += 1
            continue
        char = value[i + 1]
        if char == "u":
            digits = re.match(r"[0-9a-fA-F]{1,4}", value[i + 2:])
            if digits:
                pieces.append(chr(int(digits[0], 16)))
                i += 2 + len(digits[0])
                continue
        pieces.append(escapes.get(char, char))
        i += 2
    return "".join(pieces)


def _decode_renpy_literal(raw: str, prefix: str, quote: str) -> tuple[str | None, bool]:
    if prefix not in {"", "r"}:
        return None, True
    body = raw[len(prefix) + len(quote):-len(quote)]
    # Monologues must be split before escapes are decoded. A written \n is not
    # a source paragraph separator; raw monologues are never split.
    if prefix == "r" or len(quote) == 3:
        return body, False
    return renpy_unescape(re.sub(r"[ \n]+", " ", body)), False


def scan_string_literals(source: str, *, renpy=False, diagnostics=None) -> list[StringLiteral]:
    out: list[StringLiteral] = []
    i = 0
    line = 1
    col = 0
    n = len(source)

    while i < n:
        ch = source[i]

        if ch == "\n":
            i += 1
            line += 1
            col = 0
            continue

        # Comments outside strings.
        if ch == "#":
            while i < n and source[i] != "\n":
                i += 1
                col += 1
            continue

        start = i
        start_line = line
        start_col = col
        prefix = ""

        # String prefixes: r, u, b, f and common two-letter combinations.
        j = i
        while j < n and source[j] in _PREFIX_CHARS and (j - i) < 3:
            prefix += source[j]
            j += 1

        if j >= n or source[j] not in (("'", '"', "`") if renpy else ("'", '"')):
            i += 1
            col += 1
            continue

        # Do not treat identifier suffix as prefix, e.g. foo"bar".
        if start > 0 and (source[start-1].isalnum() or source[start-1] == "_") and prefix:
            i += 1
            col += 1
            continue

        quote_char = source[j]
        triple = source.startswith(quote_char * 3, j)
        q = quote_char * (3 if triple else 1)
        k = j + len(q)
        escaped = False

        while k < n:
            if not escaped and source.startswith(q, k):
                k += len(q)
                raw = source[start:k]
                value, dynamic = (_decode_renpy_literal(raw, prefix, q) if renpy else _decode_literal(raw))
                out.append(StringLiteral(
                    start=start, end=k, line=start_line, column=start_col,
                    raw=raw, value=value, prefix=prefix, quote=q,
                    triple=triple, dynamic=dynamic,
                ))
                segment = source[i:k]
                newlines = segment.count("\n")
                if newlines:
                    line += newlines
                    col = len(segment.rsplit("\n", 1)[-1])
                else:
                    col += len(segment)
                i = k
                break

            c = source[k]
            if c == "\n" and not triple and not escaped and not renpy:
                # Invalid/incomplete single-line literal: leave it to parser as unknown.
                i += 1
                col += 1
                break

            if c == "\\" and not escaped:
                escaped = True
                k += 1
                continue

            escaped = False
            k += 1
        else:
            if renpy:
                if diagnostics is not None:
                    diagnostics.append({"code": "unterminated_string", "line": start_line,
                                        "column": start_col, "message": "Строка не закрыта до конца файла."})
                out.append(StringLiteral(start, n, start_line, start_col, source[start:],
                                         None, prefix, q, triple, True))
                break
            i += 1
            col += 1

    return out


class LexicalIndex:
    """One lexical pass shared by recognizers for a logical statement.

    Masking keeps offsets and newlines intact. Call names in dialogue, comments
    and docstrings cannot be mistaken for executable calls.
    """
    def __init__(self, source: str, *, renpy=True):
        self.source = source
        self.literals = scan_string_literals(source, renpy=renpy)
        self.by_start = {lit.start: lit for lit in self.literals}
        code = list(source)
        commentless = list(source)
        self.parens = {}
        stack = []
        i = 0
        while i < len(source):
            lit = self.by_start.get(i)
            if lit:
                code[i:lit.end] = ["\n" if c == "\n" else " " for c in source[i:lit.end]]
                i = lit.end
                continue
            if source[i] == "#":
                end = source.find("\n", i)
                end = len(source) if end < 0 else end
                code[i:end] = commentless[i:end] = [" "] * (end - i)
                i = end
                continue
            if source[i] == "(":
                stack.append(i)
            elif source[i] == ")" and stack:
                self.parens[stack.pop()] = i
            i += 1
        self.code = "".join(code)
        self.commentless = "".join(commentless)

def literal_at_start(text: str):
    lits = scan_string_literals(text)
    if not lits:
        return None
    first = lits[0]
    prefix_ws = len(text) - len(text.lstrip())
    return first if first.start == prefix_ws else None
