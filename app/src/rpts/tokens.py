from __future__ import annotations
from dataclasses import dataclass
from collections import Counter
import re

@dataclass(frozen=True)
class ProtectedToken:
    kind: str
    text: str
    start: int
    end: int

PRINTF_RE = re.compile(
    r"%(?:\([^)]+\))?[#0+'I-]*\d*(?:\.\d+)?[hlL]?[diouxXeEfFgGcrsa%]"
)
STRFTIME_RE = re.compile(r"%(?:[-_0^#]+)?(?:[EO])?[aAbBcdHIjmMpSUwWxXyYZfzGVu]")
ESCAPE_RE = re.compile(r"\\(?:[abfnrtv\\'\"0]|x[0-9A-Fa-f]{2}|u[0-9A-Fa-f]{4}|U[0-9A-Fa-f]{8})")

def scan_protected_tokens(text: str) -> list[ProtectedToken]:
    out: list[ProtectedToken] = []
    occupied = [False] * len(text)

    def add(kind, s, e):
        if s < 0 or e <= s:
            return
        if any(occupied[s:e]):
            return
        out.append(ProtectedToken(kind, text[s:e], s, e))
        for i in range(s, min(e, len(occupied))):
            occupied[i] = True

    # Ren'Py interpolation. [[ is an escaped literal [ and is protected as escape.
    i = 0
    while i < len(text):
        if text.startswith("[[", i):
            add("renpy_escape", i, i + 2)
            i += 2
            continue
        if text[i] == "[":
            j = i + 1
            depth = 1
            quote = None
            escaped = False
            while j < len(text):
                c = text[j]
                if quote:
                    if c == "\\" and not escaped:
                        escaped = True
                    elif c == quote and not escaped:
                        quote = None
                    else:
                        escaped = False
                else:
                    if c in ("'", '"'):
                        quote = c
                    elif c == "[":
                        depth += 1
                    elif c == "]":
                        depth -= 1
                        if depth == 0:
                            add("renpy_interpolation", i, j + 1)
                            i = j + 1
                            break
                j += 1
            else:
                i += 1
                continue
            continue
        i += 1

    # Curly tokens. We protect both text tags and format fields. The exact
    # semantic subtype is heuristic; preserving the bytes is the safety goal.
    i = 0
    while i < len(text):
        if text.startswith("{{", i) or text.startswith("}}", i):
            add("format_escape", i, i + 2)
            i += 2
            continue
        if text[i] == "{":
            j = text.find("}", i + 1)
            if j != -1:
                body = text[i+1:j]
                if body == "":
                    kind = "format_field"
                elif body.startswith(("/", "#")):
                    kind = "renpy_tag"
                elif re.match(r"^[A-Za-z_][\w.-]*(?:[=!:].*)?$", body, flags=re.UNICODE):
                    # May be a custom Ren'Py tag or a format field. Keep a
                    # distinct ambiguous type so validator never rewrites it.
                    if body.split("=", 1)[0].split(":",1)[0] in {
                        "b","i","u","s","color","font","size","a","image","alpha",
                        "outlinecolor","plain","rt","rb","art","space","vspace",
                        "cps","k","noalt"
                    } or "=" in body:
                        kind = "renpy_tag"
                    else:
                        kind = "format_field"
                else:
                    kind = "curly_token"
                add(kind, i, j + 1)
                i = j + 1
                continue
        i += 1

    # `%d` is ambiguous: printf decimal vs strftime day. If a string contains
    # a cluster of date/time directives, prefer strftime semantics for all
    # overlapping directives in that string.
    strftime_matches = list(STRFTIME_RE.finditer(text))
    date_mode = len(strftime_matches) >= 2 and any(m.group(0) in {
        "%Y", "%y", "%m", "%B", "%b", "%H", "%I", "%M", "%S"
    } for m in strftime_matches)
    if date_mode:
        for m in strftime_matches:
            add("strftime", m.start(), m.end())

    for m in PRINTF_RE.finditer(text):
        add("printf", m.start(), m.end())

    if not date_mode:
        # Non-overlapping directives like %Y are still clearly strftime.
        for m in strftime_matches:
            if m.group(0) not in {"%d", "%i", "%u"}:
                add("strftime", m.start(), m.end())

    for m in ESCAPE_RE.finditer(text):
        add("escape", m.start(), m.end())

    return sorted(out, key=lambda x: (x.start, x.end))


def normalize_masked_markers(masked: str, tokens: list[ProtectedToken]) -> tuple[str, list[str]]:
    """Force every expected RPTS marker to occur exactly once.

    Local LLMs occasionally duplicate a marker even when the surrounding JSON is
    valid. Each source occurrence has its own unique marker, so removing duplicate
    copies of the *same* marker is deterministic and cannot collapse two genuine
    source placeholders. Unknown RPTS markers are also discarded. Missing markers
    remain a hard error because their insertion position cannot be inferred safely.
    """
    out = str(masked or "")
    expected = {f"⟪RPT{idx:04d}⟫" for idx in range(len(tokens))}
    changes: list[str] = []

    # Remove markers invented by the model.
    def unknown_repl(m):
        marker = m.group(0)
        if marker in expected:
            return marker
        changes.append(f"removed unknown marker {marker}")
        return ""
    out = re.sub(r"⟪RPT\d{4}⟫", unknown_repl, out)

    # Keep only the first occurrence of every source marker.
    for idx in range(len(tokens)):
        marker = f"⟪RPT{idx:04d}⟫"
        count = out.count(marker)
        if count == 0:
            raise ValueError(f"Missing protected marker {marker}")
        if count <= 1:
            continue
        first = True
        def repl(_m):
            nonlocal first
            if first:
                first = False
                return marker
            return ""
        out = re.sub(re.escape(marker), repl, out)
        changes.append(f"deduplicated {marker} x{count}")
    return out, changes


def strip_extra_interpolations(source: str, translated: str) -> tuple[str, list[str]]:
    """Remove Ren'Py interpolation tokens invented or duplicated by a model.

    This is intentionally limited to square-bracket Ren'Py interpolation. New
    interpolation expressions are executable game syntax and therefore must never
    be introduced by translation. Exact source multiplicities are authoritative.
    Missing source tokens are *not* synthesized here and will still fail validation.
    """
    expected = Counter(
        t.text for t in scan_protected_tokens(source) if t.kind == "renpy_interpolation"
    )
    if not expected and not any(t.kind == "renpy_interpolation" for t in scan_protected_tokens(translated)):
        return translated, []

    seen = Counter()
    remove: list[ProtectedToken] = []
    changes: list[str] = []
    for tok in scan_protected_tokens(translated):
        if tok.kind != "renpy_interpolation":
            continue
        if seen[tok.text] < expected[tok.text]:
            seen[tok.text] += 1
            continue
        remove.append(tok)
        changes.append(f"removed extra interpolation {tok.text}")

    if not remove:
        return translated, []
    out = translated
    for tok in sorted(remove, key=lambda t: t.start, reverse=True):
        out = out[:tok.start] + out[tok.end:]
    # Cosmetic cleanup after removing a hallucinated token in parentheses/spaces.
    out = re.sub(r"\(\s*\)", "", out)
    out = re.sub(r"[ \t]{2,}", " ", out)
    return out, changes


def strip_protected_for_context(text: str) -> str:
    """Remove game syntax from context/reference text shown only as semantic help.

    The current source line still carries protected RPTS markers. Context and EN
    references do not need executable Ren'Py syntax, and exposing raw placeholders
    there encourages small LLMs to copy or translate them into the answer.
    """
    toks = scan_protected_tokens(str(text or ""))
    if not toks:
        return str(text or "")
    out = str(text or "")
    for tok in sorted(toks, key=lambda t: t.start, reverse=True):
        # Preserve a single space so words on each side do not merge.
        replacement = " "
        if tok.kind == "renpy_interpolation" and re.fullmatch(r"\[[A-Za-z_]\w*(?:![a-z]+)?\]", tok.text):
            replacement = "‹" + tok.text[1:-1] + "›"
        out = out[:tok.start] + replacement + out[tok.end:]
    out = re.sub(r"[ \t]{2,}", " ", out).strip()
    return out

def align_reference_markers(reference: str, tokens: list[ProtectedToken], scope: str | None = None) -> str:
    """Give the EN anchor the same placeholder spelling as its own source item."""
    positions = {}
    for i, token in enumerate(tokens):
        positions.setdefault(token.text, []).append(i)
    used = Counter()
    edits = []
    for token in scan_protected_tokens(reference):
        candidates = positions.get(token.text, [])
        occurrence = used[token.text]; used[token.text] += 1
        replacement = " "
        if occurrence < len(candidates):
            i = candidates[occurrence]
            replacement = f"⟪RPTS{scope}_{i:04d}⟫" if scope is not None else f"⟪RPT{i:04d}⟫"
        edits.append((token.start, token.end, replacement))
    for start, end, replacement in reversed(edits):
        reference = reference[:start] + replacement + reference[end:]
    return reference


def protect(text: str) -> tuple[str, list[ProtectedToken]]:
    tokens = scan_protected_tokens(text)
    if not tokens:
        return text, []
    pieces = []
    pos = 0
    for idx, tok in enumerate(tokens):
        pieces.append(text[pos:tok.start])
        pieces.append(f"⟪RPT{idx:04d}⟫")
        pos = tok.end
    pieces.append(text[pos:])
    return "".join(pieces), tokens

def restore(masked: str, tokens: list[ProtectedToken]) -> str:
    out = masked
    for idx, tok in enumerate(tokens):
        marker = f"⟪RPT{idx:04d}⟫"
        if marker not in out:
            raise ValueError(f"Missing protected marker {marker}")
        out = out.replace(marker, tok.text)
    if re.search(r"⟪RPT\d{4}⟫", out):
        raise ValueError("Unknown protected marker remained")
    return out

def token_signature(text: str):
    return [(t.kind, t.text) for t in scan_protected_tokens(text)]

def validate_preservation(source: str, translated: str) -> tuple[bool, str]:
    a = token_signature(source)
    b = token_signature(translated)
    if a != b:
        return False, f"protected token mismatch: {a!r} != {b!r}"
    return True, ""
