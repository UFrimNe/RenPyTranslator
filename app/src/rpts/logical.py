from __future__ import annotations
from dataclasses import dataclass
from .strings import scan_string_literals

@dataclass(frozen=True)
class LogicalStatement:
    text: str
    line: int
    indent: int
    start: int
    end: int

def _indent_width(s: str) -> int:
    # Ren'Py normally uses spaces but accept tabs for analysis.
    total = 0
    for c in s:
        if c == " ":
            total += 1
        elif c == "\t":
            total += 4
        else:
            break
    return total

def iter_logical_statements(source: str, *, diagnostics=None) -> list[LogicalStatement]:
    """
    Split Ren'Py source into logical statements without splitting strings,
    bracketed expressions, or backslash continuations.
    """
    literals = scan_string_literals(source, renpy=True, diagnostics=diagnostics)
    lit_by_start = {x.start: x for x in literals}

    out = []
    n = len(source)
    i = 0
    start = 0
    start_line = 1
    line = 1
    paren = bracket = brace = 0
    in_comment = False

    def emit(end):
        nonlocal start, start_line
        raw = source[start:end]
        if raw.strip():
            first_line = raw.splitlines()[0] if raw.splitlines() else raw
            out.append(LogicalStatement(
                text=raw.rstrip("\r\n"),
                line=start_line,
                indent=_indent_width(first_line),
                start=start,
                end=end,
            ))

    while i < n:
        lit = lit_by_start.get(i)
        if lit is not None:
            segment = source[i:lit.end]
            line += segment.count("\n")
            i = lit.end
            continue

        c = source[i]

        if in_comment:
            if c == "\n":
                in_comment = False
            else:
                i += 1
                continue

        if c == "#":
            in_comment = True
            i += 1
            continue

        if c == "(":
            paren += 1
        elif c == ")":
            paren = max(0, paren - 1)
        elif c == "[":
            bracket += 1
        elif c == "]":
            bracket = max(0, bracket - 1)
        elif c == "{":
            brace += 1
        elif c == "}":
            brace = max(0, brace - 1)
        elif c == "\n":
            # continuation if an odd number of backslashes immediately precede EOL
            b = i - 1
            slash_count = 0
            while b >= start and source[b] == "\\":
                slash_count += 1
                b -= 1
            continued = slash_count % 2 == 1
            if paren == bracket == brace == 0 and not continued:
                emit(i + 1)
                start = i + 1
                start_line = line + 1
            line += 1
        i += 1

    if start < n:
        emit(n)
    if diagnostics is not None and (paren or bracket or brace):
        diagnostics.append({"code": "unclosed_group", "line": start_line, "column": 0,
                            "message": "В конце файла остались незакрытые скобки."})
    return out
