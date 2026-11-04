from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
import re
import unicodedata

from .strings import scan_string_literals
from .tokens import scan_protected_tokens


_NAME_RE = re.compile(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*")
_LANG_NAME_HINTS = ("lang", "language", "locale", "localized", "localise", "localize", "i18n", "l10n")


@dataclass(frozen=True)
class ParsedCall:
    function: str
    args: tuple[str, ...]
    close_pos: int
    tail: str
    start_pos: int = 0
    end_pos: int = 0


def _matching_paren(source: str, open_pos: int) -> int | None:
    if open_pos >= len(source) or source[open_pos] != "(":
        return None
    literals = {x.start: x for x in scan_string_literals(source)}
    depth = 0
    i = open_pos
    while i < len(source):
        lit = literals.get(i)
        if lit is not None:
            i = lit.end
            continue
        c = source[i]
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return None


def _split_args(source: str) -> tuple[str, ...]:
    literals = {x.start: x for x in scan_string_literals(source)}
    out: list[str] = []
    start = 0
    depth = [0, 0, 0]
    i = 0
    while i < len(source):
        lit = literals.get(i)
        if lit is not None:
            i = lit.end
            continue
        c = source[i]
        if c == "(":
            depth[0] += 1
        elif c == ")":
            depth[0] = max(0, depth[0] - 1)
        elif c == "[":
            depth[1] += 1
        elif c == "]":
            depth[1] = max(0, depth[1] - 1)
        elif c == "{":
            depth[2] += 1
        elif c == "}":
            depth[2] = max(0, depth[2] - 1)
        elif c == "," and depth == [0, 0, 0]:
            out.append(source[start:i].strip())
            start = i + 1
        i += 1
    out.append(source[start:].strip())
    return tuple(x for x in out if x != "")


def parse_top_level_call(expression: str | None) -> ParsedCall | None:
    """Parse ``foo(a, b)`` without evaluating it.

    Ren'Py Screen Language often appends properties after the expression, e.g.
    ``custom_lang("设置", "Settings") style "label"``. The returned ``tail``
    preserves that text, but only arguments inside the first call are parsed.
    """
    if not isinstance(expression, str):
        return None
    s = expression.strip()
    m = _NAME_RE.match(s)
    if not m:
        return None
    pos = m.end()
    while pos < len(s) and s[pos].isspace():
        pos += 1
    if pos >= len(s) or s[pos] != "(":
        return None
    close = _matching_paren(s, pos)
    if close is None:
        return None
    return ParsedCall(m.group(0), _split_args(s[pos + 1:close]), close, s[close + 1:].strip(), 0, close + 1)


def find_calls(expression: str | None) -> list[ParsedCall]:
    """Find function calls anywhere in an expression without evaluating it."""
    if not isinstance(expression, str):
        return []
    literals = scan_string_literals(expression)
    string_ranges = [(x.start, x.end) for x in literals]

    def inside_string(pos: int) -> bool:
        return any(a <= pos < b for a, b in string_ranges)

    out: list[ParsedCall] = []
    for m in _NAME_RE.finditer(expression):
        if inside_string(m.start()):
            continue
        # Avoid starting in the middle of an attribute/name match.
        if m.start() > 0 and (expression[m.start()-1].isalnum() or expression[m.start()-1] == "_"):
            continue
        pos = m.end()
        while pos < len(expression) and expression[pos].isspace():
            pos += 1
        if pos >= len(expression) or expression[pos] != "(":
            continue
        close = _matching_paren(expression, pos)
        if close is None:
            continue
        out.append(ParsedCall(
            m.group(0), _split_args(expression[pos + 1:close]), close,
            expression[close + 1:].strip(), m.start(), close + 1
        ))
    return out


def static_string_expression(expression: str) -> str | None:
    """Return a static string value for adjacent Python/Ren'Py literals.

    No names, calls, f-strings or operators are evaluated.
    """
    expr = expression.strip()
    literals = scan_string_literals(expr)
    if not literals:
        return None
    values: list[str] = []
    pos = 0
    for lit in literals:
        if expr[pos:lit.start].strip():
            return None
        if lit.dynamic or lit.value is None:
            return None
        values.append(lit.value)
        pos = lit.end
    if expr[pos:].strip():
        return None
    return "".join(values)


def _script_for_char(ch: str) -> str | None:
    cp = ord(ch)
    # Han extensions that occur in modern game text are covered by these ranges.
    if 0x3400 <= cp <= 0x4DBF or 0x4E00 <= cp <= 0x9FFF or 0xF900 <= cp <= 0xFAFF:
        return "Han"
    if 0x3040 <= cp <= 0x30FF or 0x31F0 <= cp <= 0x31FF:
        return "Kana"
    if 0xAC00 <= cp <= 0xD7AF or 0x1100 <= cp <= 0x11FF:
        return "Hangul"
    if 0x0400 <= cp <= 0x052F:
        return "Cyrillic"
    if 0x0E00 <= cp <= 0x0E7F:
        return "Thai"
    if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0x08A0 <= cp <= 0x08FF:
        return "Arabic"
    if (0x0041 <= cp <= 0x005A) or (0x0061 <= cp <= 0x007A) or (0x00C0 <= cp <= 0x024F):
        return "Latin"
    if unicodedata.category(ch).startswith("L"):
        return "OtherLetter"
    return None


def script_counts(text: str | None) -> Counter:
    out = Counter()
    for ch in text or "":
        s = _script_for_char(ch)
        if s:
            out[s] += 1
    return out


def script_hint(text: str | None) -> str:
    counts = script_counts(text)
    if not counts:
        return "none"
    # Han + Kana is a strong Japanese-script signal. Han alone deliberately
    # stays "Han" because script alone cannot distinguish zh/ja reliably.
    if counts["Kana"]:
        return "Japanese"
    if counts["Hangul"] and counts["Hangul"] >= counts["Han"]:
        return "Korean"
    return counts.most_common(1)[0][0]


def has_human_language_payload(text: str | None) -> bool:
    """True when a static string contains language-bearing characters.

    Protected Ren'Py/format tokens are removed first. This is *not* used to
    decide whether something is user-visible; AST/source context already did
    that. It only keeps placeholder-only strings out of an AI translation queue.
    """
    if not text:
        return False
    tokens = scan_protected_tokens(text)
    parts: list[str] = []
    pos = 0
    for tok in tokens:
        parts.append(text[pos:tok.start])
        pos = tok.end
    parts.append(text[pos:])
    payload = "".join(parts)
    return any(unicodedata.category(ch).startswith("L") for ch in payload)


def _strip_protected(text: str | None) -> str:
    if not text:
        return ""
    tokens = scan_protected_tokens(text)
    parts: list[str] = []
    pos = 0
    for tok in tokens:
        parts.append(text[pos:tok.start])
        pos = tok.end
    parts.append(text[pos:])
    return "".join(parts)


_LOCALIZABLE_PUNCTUATION = set("。！？…，、；：（）【】《》「」『』〈〉〔〕〖〗〝〞﹏～—–·：")


def needs_format_or_punctuation_localization(text: str | None, kind: str | None = None) -> bool:
    """Return True for user-visible text with no words but target-sensitive typography.

    These rows should not consume a normal semantic translation request, but they also
    must not be silently copied to every target language. Examples include ``[name]。``
    and date/number formats. Generic UI glyphs such as ``×`` or ``>`` stay invariant.
    """
    if not text:
        return False
    raw = str(text)
    if any(ch in _LOCALIZABLE_PUNCTUATION for ch in raw):
        return True
    protected = scan_protected_tokens(raw)
    protected_kinds = {x.kind for x in protected}
    if protected_kinds & {"strftime", "printf", "format_field"}:
        # Formats can have language-specific ordering/separators even with no words.
        return True
    # Dialogue/narration made only of punctuation is a typography decision. Keep it in
    # a lightweight localization queue rather than treating it as an invariant asset.
    if kind in {"dialogue", "dialogue_extend", "narration", "centered"}:
        payload = _strip_protected(raw)
        if payload.strip() and not any(unicodedata.category(ch).startswith("L") for ch in payload):
            return True
    return False


def build_translation_queue(source_rows: list[dict]) -> tuple[list[dict], list[dict], list[dict]]:
    """Split source rows into semantic translation, localization-only and invariant/dynamic.

    The old Stage 3 binary queue discarded punctuation/format-only text. Stage 3.1 keeps
    those rows separate so a target-language patch can normalize punctuation and formats
    without paying for/depending on a semantic translation model call.
    """
    queue: list[dict] = []
    localization: list[dict] = []
    skipped: list[dict] = []
    for row in source_rows:
        if row.get("dynamic"):
            r = dict(row); r["queue_reason"] = "dynamic_expression"; skipped.append(r); continue
        if not row.get("text"):
            r = dict(row); r["queue_reason"] = "no_static_text"; skipped.append(r); continue
        if has_human_language_payload(row.get("text")):
            r = dict(row); r["script_hint"] = script_hint(r.get("text")); r["queue_reason"] = "semantic_translation"; queue.append(r); continue
        if needs_format_or_punctuation_localization(row.get("text"), row.get("kind")):
            r = dict(row); r["script_hint"] = script_hint(r.get("text")); r["queue_reason"] = "format_or_punctuation_localization"; localization.append(r); continue
        r = dict(row); r["queue_reason"] = "invariant_or_placeholder_only"; skipped.append(r)
    return queue, localization, skipped


def _is_keyword_arg(arg: str) -> bool:
    # Conservative check: ``name=value`` only at the beginning. Operators inside
    # nested/static strings are irrelevant because the multilingual detector only
    # accepts static positional string arguments as variants.
    return bool(re.match(r"^[A-Za-z_]\w*\s*=", arg.strip()))


def analyze_multilingual(dynamic_rows: list[dict], explicit_functions: dict[str, list[str]] | None = None):
    """Analyze dynamic user-visible expressions for multilingual helper calls.

    This does not assume every function with two strings is a language switch.
    Automatic recognition is intentionally limited to strong evidence:
      * explicitly configured function, or
      * language-like function name + repeated static string variants, or
      * repeated calls with stable, distinct writing scripts in argument slots.

    Returns ``(function_stats, calls, variants, unresolved)``.
    """
    explicit_functions = explicit_functions or {}
    parsed_rows = []
    totals = Counter()
    eligible = Counter()
    distinct_script_pairs = Counter()

    for idx, row in enumerate(dynamic_rows):
        calls = find_calls(row.get("expression"))
        for call in calls:
            totals[call.function] += 1
            positional = []
            for arg in call.args:
                if _is_keyword_arg(arg):
                    break
                positional.append(arg)
            static = [static_string_expression(a) for a in positional]
            # A multilingual alternative must start with at least two static variants.
            if len(static) < 2 or static[0] is None or static[1] is None:
                parsed_rows.append((idx, row, call, static, False))
                continue
            eligible[call.function] += 1
            h0, h1 = script_hint(static[0]), script_hint(static[1])
            if h0 != "none" and h1 != "none" and h0 != h1:
                distinct_script_pairs[call.function] += 1
            parsed_rows.append((idx, row, call, static, True))

    decisions = {}
    stats = []
    functions = sorted(set(totals) | set(explicit_functions))
    for fn in functions:
        total = totals[fn]
        good = eligible[fn]
        distinct = distinct_script_pairs[fn]
        ratio = good / total if total else 0.0
        distinct_ratio = distinct / good if good else 0.0
        basename = fn.rsplit(".", 1)[-1].lower()
        name_hint = any(h in basename for h in _LANG_NAME_HINTS)
        explicit = fn in explicit_functions
        confidence = "none"
        reason = "insufficient evidence"
        if explicit and good:
            confidence = "explicit"
            reason = "configured multilingual function"
        elif name_hint and good >= 2 and ratio >= 0.6:
            confidence = "high"
            reason = "language-like function name with repeated static variants"
        elif good >= 5 and ratio >= 0.8 and distinct_ratio >= 0.7:
            confidence = "medium"
            reason = "repeated static alternatives with stable distinct writing scripts"
        decisions[fn] = confidence
        stats.append({
            "function": fn,
            "calls": total,
            "eligible_static_variant_calls": good,
            "eligible_ratio": round(ratio, 4),
            "distinct_script_pair_calls": distinct,
            "distinct_script_ratio": round(distinct_ratio, 4),
            "name_hint": name_hint,
            "confidence": confidence,
            "reason": reason,
        })

    calls_out: list[dict] = []
    variants_out: list[dict] = []
    recognized_indices = set()
    for idx, row, call, static, is_eligible in parsed_rows:
        confidence = decisions.get(call.function, "none")
        # Medium is reported as a hypothesis but is not automatically removed from
        # unresolved. High/explicit is strong enough for a structured extraction.
        recognized = is_eligible and confidence in {"high", "explicit"}
        if not recognized:
            continue
        recognized_indices.add(idx)
        call_id = f"ml_{len(calls_out)+1:05d}"
        static_variants = []
        for arg_index, value in enumerate(static):
            if value is None:
                break
            static_variants.append((arg_index, value))
        calls_out.append({
            "call_id": call_id,
            "function": call.function,
            "confidence": confidence,
            "variant_count": len(static_variants),
            "kind": row.get("kind"),
            "file": row.get("file"),
            "line": row.get("line"),
            "layer": row.get("layer"),
            "expression": row.get("expression"),
            "call_expression": (row.get("expression") or "")[call.start_pos:call.end_pos],
            "tail": call.tail,
        })
        for arg_index, value in static_variants:
            variants_out.append({
                "call_id": call_id,
                "function": call.function,
                "confidence": confidence,
                "variant_index": arg_index,
                "text": value,
                "script_hint": script_hint(value),
                "kind": row.get("kind"),
                "file": row.get("file"),
                "line": row.get("line"),
                "layer": row.get("layer"),
            })

    unresolved = [dict(row) for idx, row in enumerate(dynamic_rows) if idx not in recognized_indices]
    return stats, calls_out, variants_out, unresolved


def build_translation_pairs(rows: list[dict]):
    """Pair TranslateString old/new rows and collapse exact duplicate TM entries.

    Compiled games can expose the same TranslateString through both AST and the
    in-memory decompiled source. For translation memory this is one pair, not two.
    Exact repeats are retained as ``occurrence_count`` instead of duplicated rows.
    """
    pending: dict[tuple[str, str], list[dict]] = defaultdict(list)
    paired: list[dict] = []
    for row in rows:
        kind = row.get("kind")
        if kind not in {"translation_old", "translation_new"}:
            continue
        key = (str(row.get("file") or ""), str(row.get("language") or ""))
        if kind == "translation_old":
            pending[key].append(row)
            continue
        if not pending[key]:
            continue
        old = pending[key].pop(0)
        old_text, new_text = old.get("text") or "", row.get("text") or ""
        language_raw = row.get("language")
        paired.append({
            "file": row.get("file"),
            "language": language_raw,
            "language_bucket": "default(None)" if str(language_raw) == "None" else (language_raw or "unknown"),
            "old": old_text,
            "new": new_text,
            "old_line": old.get("line"),
            "new_line": row.get("line"),
            "identical": old_text == new_text,
            "old_script_hint": script_hint(old_text),
            "new_script_hint": script_hint(new_text),
        })

    out: list[dict] = []
    index: dict[tuple, dict] = {}
    for p in paired:
        key = (p["file"], str(p["language"]), p["old"], p["new"])
        current = index.get(key)
        if current is None:
            current = dict(p)
            current["occurrence_count"] = 1
            current["pair_id"] = f"tl_{len(out)+1:05d}"
            index[key] = current
            out.append(current)
        else:
            current["occurrence_count"] += 1
    return out


def infer_variant_roles(variants: list[dict], dominant_source_script: str | None):
    """Annotate multilingual variants with a conservative source-like role guess.

    The guess is based on writing script only and is therefore advisory. For example,
    Han alone is not labelled Chinese. No language code is invented.
    """
    rows = [dict(x) for x in variants]
    if not dominant_source_script or dominant_source_script == "none":
        for row in rows:
            row["role_guess"] = "unknown"
        return rows, {}

    by_fn_index: dict[tuple[str, int], Counter] = defaultdict(Counter)
    for row in rows:
        by_fn_index[(row["function"], int(row["variant_index"]))][row.get("script_hint") or "none"] += 1

    functions = sorted({x["function"] for x in rows})
    source_index_by_function = {}
    for fn in functions:
        candidates = []
        indexes = sorted({int(x["variant_index"]) for x in rows if x["function"] == fn})
        for idx in indexes:
            counts = by_fn_index[(fn, idx)]
            total = sum(counts.values())
            hit = counts[dominant_source_script]
            ratio = hit / total if total else 0.0
            candidates.append((ratio, hit, total, idx))
        candidates.sort(reverse=True)
        if candidates:
            ratio, hit, total, idx = candidates[0]
            second_ratio = candidates[1][0] if len(candidates) > 1 else 0.0
            if hit >= 2 and ratio >= 0.7 and ratio > second_ratio:
                source_index_by_function[fn] = idx

    for row in rows:
        guessed = source_index_by_function.get(row["function"])
        if guessed is None:
            row["role_guess"] = "unknown"
        elif int(row["variant_index"]) == guessed:
            row["role_guess"] = "source_like"
        else:
            row["role_guess"] = "alternate_like"
    return rows, source_index_by_function


def classify_dynamic_rows(rows: list[dict]) -> list[dict]:
    """Add a non-evaluating triage classification for unresolved display expressions."""
    out = []
    simple_ref = re.compile(r'^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)?(?:\[[^\]]+\])?\s*:?$')
    call_re = re.compile(r'^([A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*)\s*\(')
    for row in rows:
        r = dict(row)
        expr = str(r.get("expression") or "").strip()
        if re.search(r'\b_\s*\(', expr):
            cls = "contains_marked_static"
            action = "static_children_extracted_check_runtime_result"
        elif simple_ref.match(expr):
            cls = "reference_or_subscript"
            action = "trace_assignment_or_runtime_observe"
        elif call_re.match(expr):
            cls = "runtime_call"
            action = "trace_function_or_runtime_observe"
        elif any(op in expr for op in (' + ', ' % ', ' if ', ' else ')) or expr.startswith('('):
            cls = "composite_expression"
            action = "trace_inputs_or_runtime_observe"
        else:
            cls = "screen_expression"
            action = "parse_screen_expression_or_runtime_observe"
        r["dynamic_class"] = cls
        r["recommended_next_step"] = action
        out.append(r)
    return out


def interpolation_expression_rows(source_rows: list[dict]) -> list[dict]:
    """Inspect expressions inside displayed strings without evaluating game code."""
    from .tokens import scan_protected_tokens
    out = []
    for row in source_rows:
        if row.get("dynamic") in (True, "True", "true", 1, "1"):
            continue
        for token in scan_protected_tokens(str(row.get("text") or "")):
            if token.kind != "renpy_interpolation":
                continue
            expression = token.text[1:-1]
            if not find_calls(expression):
                continue
            child = dict(row)
            child.update(expression=expression, text="", dynamic=True,
                         column=int(row.get("column") or 0) + token.start)
            out.append(child)
    return out
