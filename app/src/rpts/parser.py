from __future__ import annotations
import ast
import re
from dataclasses import dataclass, replace
from .model import Candidate
from .logical import iter_logical_statements
from .strings import scan_string_literals, LexicalIndex, renpy_unescape

COMMAND_PREFIXES = {
    "play", "queue", "stop", "voice", "scene", "show", "hide", "with",
    "jump", "call", "return", "pause", "window", "camera", "layer",
    "image", "transform", "style", "init", "define", "default", "python",
    "screen", "label", "use", "add", "key", "timer", "on", "drag", "bar",
    "vbar", "viewport", "vpgrid", "fixed", "frame", "hbox", "vbox", "grid",
    "null", "side", "imagemap", "hotspot", "hotbar", "mousearea", "input",
    "if", "elif", "else", "while", "for", "pass", "break", "continue",
}

SCREEN_TEXT_COMMANDS = {"text", "textbutton", "label", "tooltip"}
MARKERS = {"_", "__", "___", "_p"}

PYTHON_SINKS = {
    "renpy.notify": (0, "notification"),
    "renpy.input": (0, "input_prompt"),
    "ui.text": (0, "ui"),
    "Text": (0, "ui"),
    "renpy.say": (1, "dialogue"),
}

_CALL_RE = re.compile(r"(?<![\w.])((?:[^\W\d]|_)\w*(?:\.(?:[^\W\d]|_)\w*)*)\s*\(")
_CHARACTER_CONSTRUCTORS = {"Character", "DynamicCharacter", "store.Character", "store.DynamicCharacter", "renpy.character.Character"}
_SINK_KEYWORDS = {"renpy.notify": "message", "renpy.input": "prompt", "ui.text": "text", "Text": "text", "renpy.say": "what"}


def _qualified_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _qualified_name(node.value)
        return base + "." + node.attr if base else None
    return None


def _static_python_value(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bytes):
            try:
                return node.value.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return node.value if isinstance(node.value, (str, bool, type(None))) else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_python_value(node.left), _static_python_value(node.right)
        return left + right if isinstance(left, str) and isinstance(right, str) else None
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in MARKERS and node.args:
        return _static_python_value(node.args[0])
    return None


def _static_python_string(expr):
    try:
        value = _static_python_value(ast.parse(expr.strip(), mode="eval").body)
        return value if isinstance(value, str) else None
    except (SyntaxError, ValueError, RecursionError):
        return None


def _argument_node(node, index, keyword=None):
    if keyword:
        for kw in node.keywords:
            if kw.arg == keyword:
                return kw.value
    # A starred argument makes subsequent positional indexes unknowable.
    if len(node.args) > index and not any(isinstance(a, ast.Starred) for a in node.args[:index + 1]):
        return node.args[index]
    return None


def _character_symbol(node, symbols):
    if not isinstance(node, ast.Call) or _qualified_name(node.func) not in _CHARACTER_CONSTRUCTORS:
        return None
    kind_node = next((kw.value for kw in node.keywords if kw.arg == "kind"), None)
    parent = symbols.get(_qualified_name(kind_node), {})
    dynamic_node = next((kw.value for kw in node.keywords if kw.arg == "dynamic"), None)
    dynamic = (bool(_static_python_value(dynamic_node)) if isinstance(dynamic_node, ast.Constant)
               else dynamic_node is not None or bool(parent.get("dynamic")))
    dynamic = dynamic or _qualified_name(node.func).endswith("DynamicCharacter")
    name_node = _argument_node(node, 0, "name")
    value = _static_python_value(name_node) if name_node is not None else parent.get("value")
    return {"value": value if isinstance(value, str) else None, "dynamic": dynamic,
            "expression": ast.unparse(name_node) if name_node is not None else parent.get("expression"),
            "explicit_none": isinstance(name_node, ast.Constant) and name_node.value is None}


def discover_character_symbols(statements, initial=None, indexes=None):
    """Resolve declarative Character bindings and aliases without running game code."""
    statements = list(statements)
    symbols = dict(initial or {})
    bindings = {}
    indexes = indexes if indexes is not None else [LexicalIndex(st.text) for st in statements]
    for st, index in zip(statements, indexes):
        text = index.commentless.strip()
        match = re.match(r"(?:(?:define|default)\s+|\$\s*)?([^\W\d]\w*(?:\.[^\W\d]\w*)*)\s*=\s*(.+)", text, re.S)
        if not match:
            continue
        try:
            bindings[match[1]] = ast.parse(match[2], mode="eval").body
        except (SyntaxError, ValueError, RecursionError):
            continue
    # A file-local assignment overrides an imported store binding. An unknown
    # callable/string must not remain classified as a declared Character.
    for name in bindings:
        symbols.pop(name, None)
    for _ in range(len(bindings) + 1):
        changed = False
        for name, node in bindings.items():
            value = _character_symbol(node, symbols)
            if value is None:
                value = symbols.get(_qualified_name(node))
            if value is not None and symbols.get(name) != value:
                symbols[name] = dict(value)
                changed = True
        if not changed:
            break
    return symbols


@dataclass(frozen=True)
class _PythonCall:
    name: str
    start: int
    end: int
    source: str
    node: ast.Call | None

    def argument(self, index, keyword=None):
        if self.node is None:
            return None
        node = _argument_node(self.node, index, keyword)
        return ast.get_source_segment(self.source, node) if node is not None else None


def _python_calls(index):
    calls = []
    for match in _CALL_RE.finditer(index.code):
        close = index.parens.get(match.end() - 1)
        if close is None:
            continue
        source = index.source[match.start():close + 1]
        try:
            node = ast.parse(source, mode="eval").body
            if not isinstance(node, ast.Call):
                node = None
        except (SyntaxError, ValueError, RecursionError):
            node = None
        calls.append(_PythonCall(match[1], match.start(), close + 1, source, node))
    return calls


def _position(st, pos):
    prefix = st.text[:pos]
    return st.line + prefix.count("\n"), len(prefix.rsplit("\n", 1)[-1])


def _screen_expression(expr):
    """Strip SL properties, preserving the complete simple Python expression."""
    expr = LexicalIndex(expr).commentless.strip().rstrip(":").rstrip()
    try:
        ast.parse(expr, mode="eval")
        return expr
    except (SyntaxError, ValueError):
        pass
    index = LexicalIndex(expr)
    if 0 in index.by_start:
        return expr[:index.by_start[0].end]
    if expr.startswith("(") and 0 in index.parens:
        return expr[:index.parens[0] + 1]
    calls = _python_calls(index)
    if calls and calls[0].start == 0:
        return expr[:calls[0].end]
    return expr


def _static_string_expr(expr: str, *, renpy=False):
    expr = expr.strip()
    lits = scan_string_literals(expr, renpy=renpy)
    if not lits:
        return None
    # Accept adjacent string literals ("a" "b") as Python/Ren'Py concatenation.
    pos = 0
    values = []
    for lit in lits:
        if expr[pos:lit.start].strip():
            return None
        if lit.value is None or lit.dynamic:
            return None
        values.append(lit.value)
        pos = lit.end
    if expr[pos:].strip():
        return None
    return "".join(values)


def _has_top_level_assignment(text: str, index=None) -> bool:
    text = (index if index is not None else LexicalIndex(text)).code
    depth = [0, 0, 0]
    i = 0
    while i < len(text):
        c = text[i]
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
        elif c == "=" and depth == [0, 0, 0]:
            prev = text[i-1] if i else ""
            nxt = text[i+1] if i+1 < len(text) else ""
            if prev not in "<>!=:" and nxt != "=":
                return True
        elif c == "#":
            break
        i += 1
    return False

def _top_level_literals(text: str, *, renpy=True, index=None):
    index = index if index is not None else LexicalIndex(text, renpy=renpy)
    out = []
    depth = [0,0,0]
    i = 0
    by_start = index.by_start
    while i < len(text):
        lit = by_start.get(i)
        if lit:
            if depth == [0,0,0]:
                out.append(lit)
            i = lit.end
            continue
        c = index.code[i]
        if c == "(":
            depth[0] += 1
        elif c == ")":
            depth[0] = max(0,depth[0]-1)
        elif c == "[":
            depth[1] += 1
        elif c == "]":
            depth[1] = max(0,depth[1]-1)
        elif c == "{":
            depth[2] += 1
        elif c == "}":
            depth[2] = max(0,depth[2]-1)
        elif c == "#":
            break
        i += 1
    return out

def _spoken_values(lit, delimiter):
    if not lit.triple or lit.prefix == "r":
        return [(0, lit.value)] if lit.value is not None else []
    body = re.sub(r" *\n *", "\n", lit.value or "")
    parts = body.split(delimiter) if delimiter else [body]
    values, pos = [], 0
    for part in parts:
        leading = len(part) - len(part.lstrip())
        offset = body.count("\n", 0, pos) + part.count("\n", 0, leading)
        pos += len(part) + len(delimiter)
        part = part.strip()
        if part:
            pattern = r"[ \n]+" if delimiter else r" +"
            values.append((offset, renpy_unescape(re.sub(pattern, " ", part))))
    return values


class RenPySourceParser:
    def __init__(self, python_sinks=None, *, infer_say=True, infer_menu=True):
        self.python_sinks = dict(PYTHON_SINKS)
        if python_sinks:
            self.python_sinks.update(python_sinks)
        # For original .rpy/.rpym source, say/menu syntax must be inferred from text.
        # For compiled .rpyc/.rpymc we already have authoritative Say/Menu AST nodes,
        # so re-inferring them from unrpyc pretty-printed source would duplicate every
        # line and can misread Screen Language properties as character dialogue.
        self.infer_say = bool(infer_say)
        self.infer_menu = bool(infer_menu)
        self.diagnostics = []
        self.character_symbols = {}

    def parse(self, source: str, *, character_symbols=None) -> list[Candidate]:
        source = source.replace("\r\n", "\n").replace("\r", "\n")
        self.diagnostics = []
        statements = iter_logical_statements(source, diagnostics=self.diagnostics)
        indexes = [LexicalIndex(st.text) for st in statements]
        self.character_symbols = discover_character_symbols(statements, character_symbols, indexes)
        out: list[Candidate] = []
        menu_stack: list[dict] = []
        tl_strings_stack: list[tuple[int, str | None]] = []
        translate_stack: list[tuple[int, str]] = []
        no_say_stack: list[tuple[int, str]] = []
        monologue_delimiter = "\n\n"
        label_stack = []
        global_label = None
        context_stack = []

        def current_language():
            if tl_strings_stack:
                return tl_strings_stack[-1][1]
            if translate_stack:
                return translate_stack[-1][1]
            return None

        def emit(values, language=None):
            if values is None:
                return
            if isinstance(values, Candidate):
                values = [values]
            lang = current_language() if language is None else language
            for c in values:
                if lang is not None and c.language is None:
                    c = replace(c, language=lang)
                if c.label is None and label_stack:
                    c = replace(c, label=label_stack[-1][1])
                c = replace(c, context_scope=tuple(x[1] for x in context_stack))
                out.append(c)

        for st, index in zip(statements, indexes):
            statement_start = len(out)
            stripped = index.commentless.strip()
            if not stripped:
                continue

            # Close indentation-scoped contexts before interpreting this line.
            menu_stack[:] = [x for x in menu_stack if st.indent > x["indent"]]
            tl_strings_stack[:] = [x for x in tl_strings_stack if st.indent > x[0]]
            translate_stack[:] = [x for x in translate_stack if st.indent > x[0]]
            no_say_stack[:] = [x for x in no_say_stack if st.indent > x[0]]
            label_stack[:] = [x for x in label_stack if st.indent > x[0]]
            context_stack[:] = [x for x in context_stack if st.indent > x[0]]
            # Alternative source branches are not a chronological played route.
            if not no_say_stack and (re.match(r'^(?:if\b|elif\b|else\b|while\b|for\b|menu\b).*:\s*$', stripped,re.S)
                                    or menu_stack and re.match(r'^["\u201c\'].*:\s*$',stripped,re.S)):
                context_stack.append((st.indent,str(st.line)))

            if not no_say_stack:
                lm = re.match(r"label\s+(\.?[^\W\d]\w*(?:\.[^\W\d]\w*)*)(?:\s*\(.*\))?(?:\s+hide)?\s*:\s*$", stripped, re.S)
                if lm:
                    name = lm[1]
                    if name.startswith("."):
                        name = (global_label or "") + name
                    else:
                        global_label = name
                    label_stack.append((st.indent, name))
                    continue

            mm = re.match(r"rpy\s+monologue\s+(double|single|none)\s*$", stripped)
            if mm:
                monologue_delimiter = {"double": "\n\n", "single": "\n", "none": ""}[mm.group(1)]
                continue

            # Existing string-translation block.
            m = re.match(r"translate\s+([A-Za-z0-9_-]+|None)\s+strings\s*:\s*$", stripped)
            if m:
                # Keep the literal token "None" distinguishable from original source.
                tl_strings_stack.append((st.indent, m.group(1)))
                continue

            # Existing dialogue translation block: translate EN id_123:
            m = re.match(r"translate\s+([A-Za-z0-9_-]+|None)\s+(?!strings\b)(.+?):\s*$", stripped, flags=re.S)
            if m:
                translate_stack.append((st.indent, m.group(1)))
                tail = m.group(2).strip()
                if tail.startswith("python") or tail.startswith("early python"):
                    no_say_stack.append((st.indent, "python"))
                continue

            if self.infer_menu and re.match(r"menu(?:\s+[^:]*)?\s*:\s*$", stripped):
                menu_stack.append({"indent": st.indent, "item_indent": None})
                continue

            # Blocks where a bare `property "value"` is data, not Ren'Py say syntax.
            # We still scan explicit text displayables, translation markers and runtime sinks.
            if re.match(r"(?:init(?:\s+[+-]?\d+)?\s+)?python\b.*:\s*$", stripped):
                no_say_stack.append((st.indent, "python"))
                continue
            if re.match(r"screen\b.*:\s*$", stripped):
                no_say_stack.append((st.indent, "screen"))
                continue
            if re.match(r"style\b.*:\s*$", stripped):
                no_say_stack.append((st.indent, "style"))
                continue
            if re.match(r"transform\b.*:\s*$", stripped):
                no_say_stack.append((st.indent, "transform"))
                continue
            if re.match(r"layeredimage\b.*:\s*$", stripped):
                no_say_stack.append((st.indent, "layeredimage"))
                continue
            if re.match(r"image\b.*:\s*$", stripped):
                no_say_stack.append((st.indent, "image_atl"))
                continue

            in_screen = any(kind == "screen" for _, kind in no_say_stack)
            in_no_say = bool(no_say_stack)

            # Existing translate strings.
            if tl_strings_stack:
                m = re.match(r"(old|new)\s+(.+?)\s*$", stripped, flags=re.S)
                if m:
                    value = _static_string_expr(m.group(2), renpy=True)
                    if value is not None:
                        emit(Candidate(
                            kind="translation_old" if m.group(1) == "old" else "translation_new",
                            text=value, line=st.line, column=st.indent,
                            language=tl_strings_stack[-1][1], source=stripped
                        ), language=tl_strings_stack[-1][1])
                    continue

            # Menu choices/captions only exist at the menu's direct child indentation.
            # Text deeper inside a choice block is normal narration/dialogue.
            if menu_stack and menu_stack[-1]["item_indent"] is None and st.indent > menu_stack[-1]["indent"]:
                menu_stack[-1]["item_indent"] = st.indent
            if menu_stack and not in_no_say and st.indent == menu_stack[-1]["item_indent"]:
                first = scan_string_literals(stripped, renpy=True)
                if first and first[0].start == 0 and first[0].value is not None:
                    rest = stripped[first[0].end:].strip()
                    if rest.endswith(":"):
                        emit(Candidate(kind="menu", text=first[0].value, line=st.line,
                                       column=st.indent, source=stripped))
                        emit(self._marked_calls(st, _python_calls(index)))
                        continue
                    if not rest:
                        emit(Candidate(kind="menu_caption", text=first[0].value, line=st.line,
                                       column=st.indent, source=stripped))
                        continue

            # Screen language text-bearing displayables.
            sm = re.match(r"(text|textbutton|label|tooltip)\b(.*)$", stripped, flags=re.S)
            if sm:
                cmd, expr = sm.group(1), sm.group(2).strip()
                # Outside a screen, `label name(...):` is a Ren'Py script label.
                if cmd == "label" and not in_screen and re.match(r"[A-Za-z_]\w*(?:\s*\([^)]*\))?\s*:\s*$", expr, flags=re.S):
                    continue

                display_expr = _screen_expression(expr)
                direct = _static_python_string(display_expr)
                produced = []
                if direct is not None:
                    produced.append(Candidate(kind="ui", text=direct, line=st.line,
                                              column=st.indent, source=stripped))
                else:
                    produced.append(Candidate(kind="ui", text=None, line=st.line,
                                              column=st.indent, dynamic=True,
                                              expression=display_expr, source=stripped))
                emit(produced)
                emitted_texts = {c.text for c in produced if c.text}
                emit(self._marked_calls(st, _python_calls(index), exclude_texts=emitted_texts))
                continue

            # Character declaration/name.
            calls = _python_calls(index)
            emit(self._character_names(st, calls))

            # Python/runtime sinks and translation markers remain useful in Python/screen blocks.
            emit(self._python_sink_calls(st, calls))
            emit(self._marked_calls(st, calls, exclude_texts={x.text for x in out[statement_start:] if x.text}))

            # Never infer Ren'Py say syntax from Python, Screen Language, style or ATL properties.
            if self.infer_say and not in_no_say:
                emit(self._say(st, monologue_delimiter, index))

        # Exact duplicate records from overlapping recognizers are useless.
        dedup = []
        seen = set()
        for c in out:
            key = (c.kind, c.text, c.line, c.speaker, c.dynamic, c.expression, c.language, c.label)
            if key not in seen:
                seen.add(key)
                dedup.append(c)
        return dedup

    def _character_names(self, st, calls):
        out = []
        for call in calls:
            symbol = _character_symbol(call.node, self.character_symbols)
            if symbol is None or symbol.get("explicit_none"):
                continue
            line, column = _position(st, call.start)
            value, expr = symbol["value"], symbol["expression"]
            if symbol["dynamic"]:
                expr = value if value is not None else expr
                value = None
            if value is not None or expr is not None:
                out.append(Candidate(kind="character_name", text=value, dynamic=value is None,
                                     expression=expr if value is None else None, line=line,
                                     column=column, source=st.text.strip()))
        return out

    def _marked_calls(self, st, calls=None, exclude_texts=None):
        exclude_texts = exclude_texts or set()
        out = []
        for call in calls if calls is not None else _python_calls(LexicalIndex(st.text)):
            if call.name not in MARKERS:
                continue
            expr = call.argument(0)
            val = _static_python_string(expr) if expr is not None else None
            if val is not None and val not in exclude_texts:
                line, column = _position(st, call.start)
                out.append(Candidate(
                    kind="marked_string", text=val, line=line,
                    column=column, source=st.text.strip()
                ))
        return out

    def _python_sink_calls(self, st, calls=None):
        out = []
        for call in calls if calls is not None else _python_calls(LexicalIndex(st.text)):
            line, column = _position(st, call.start)
            if call.name == "renpy.display_menu":
                items = _argument_node(call.node, 0, "items") if call.node else None
                if not isinstance(items, (ast.List, ast.Tuple)):
                    self.diagnostics.append({"code": "dynamic_python_menu", "line": line, "column": column,
                                             "message": "Пункты Python-меню формируются динамически; требуется наблюдение в игре."})
                    continue
                for entry in items.elts:
                    if not isinstance(entry, (ast.Tuple, ast.List)) or len(entry.elts) != 2:
                        continue
                    expr = ast.get_source_segment(call.source, entry.elts[0])
                    val = _static_python_string(expr) if expr else None
                    kind = "menu_caption" if isinstance(entry.elts[1], ast.Constant) and entry.elts[1].value is None else "menu"
                    out.append(Candidate(kind=kind, text=val, line=line, column=column, dynamic=val is None,
                                         expression=expr if val is None else None, source=st.text.strip()))
                continue
            speaker = None
            if call.name in self.python_sinks:
                arg_index, kind = self.python_sinks[call.name]
                keyword = _SINK_KEYWORDS.get(call.name)
                if call.name == "renpy.say":
                    who = call.argument(0, "who")
                    if who is not None:
                        speaker = _static_python_string(who)
                        if speaker is None and who.strip() != "None":
                            speaker = who.strip()
                    if who is not None and who.strip() == "None":
                        kind = "narration"
            elif call.name in self.character_symbols or call.name in {"narrator", "centered", "extend"}:
                arg_index, keyword = 0, "what"
                speaker = call.name
                kind = "dialogue"
                if call.name in {"narrator", "centered", "extend"}:
                    kind = {"narrator": "narration", "centered": "centered", "extend": "dialogue_extend"}[call.name]
                    speaker = None
            else:
                continue
            expr = call.argument(arg_index, keyword)
            if expr is None:
                self.diagnostics.append({"code": "unresolved_python_argument", "line": line, "column": column,
                                         "message": "Не удалось определить текстовый аргумент вызова " + call.name + "."})
                continue
            val = _static_python_string(expr)
            out.append(Candidate(kind=kind, text=val, line=line, column=column, speaker=speaker,
                                 dynamic=val is None, expression=expr if val is None else None, source=st.text.strip()))
        return out

    def _say(self, st, monologue_delimiter="\n\n", index=None):
        index = index if index is not None else LexicalIndex(st.text)
        stripped = index.commentless.strip()
        if not stripped or stripped.startswith(("#", "$")):
            return []

        first_word_m = re.match(r"([^\W\d]\w*)", stripped)
        first_word = first_word_m.group(1) if first_word_m else None

        offset = len(st.text) - len(st.text.lstrip())
        top = [replace(lit, start=lit.start - offset, end=lit.end - offset,
                       column=lit.column - offset if lit.line == 1 else lit.column)
               for lit in _top_level_literals(st.text, index=index)]
        if first_word == "centered":
            if top and top[-1].value is not None:
                lit = top[-1]
                return [Candidate(kind="centered", text=v, line=st.line + lit.line - 1 + offset,
                                  column=st.indent + lit.column, source=stripped)
                        for offset, v in _spoken_values(lit, monologue_delimiter)]
            return []
        if first_word in COMMAND_PREFIXES:
            return []

        if not top:
            return []

        # Exclude assignment/expression-only statements.
        if _has_top_level_assignment(st.text, index) and not stripped.startswith("expression "):
            return []

        # A line beginning with a literal can be narration or literal-speaker form.
        if top[0].start == 0:
            if len(top) >= 2:
                speaker = top[0].value
                dialogue = top[1].value
                if speaker is not None and dialogue is not None:
                    values = _spoken_values(top[1], monologue_delimiter)
                    return [Candidate(
                        kind="dialogue", text=v, speaker=speaker,
                        line=st.line + top[1].line - 1 + offset, column=st.indent + top[1].column,
                        source=stripped
                    ) for offset, v in values]
            if top[0].value is not None:
                values = _spoken_values(top[0], monologue_delimiter)
                return [Candidate(
                    kind="narration", text=v, line=st.line + top[0].line - 1 + offset,
                    column=st.indent, source=stripped
                ) for offset, v in values]
            return []

        if stripped.startswith("extend "):
            lit = top[0]
            return [Candidate(kind="dialogue_extend", text=v, line=st.line + lit.line - 1 + offset,
                              column=st.indent+lit.column, source=stripped)
                    for offset, v in _spoken_values(lit, monologue_delimiter)]

        # Character(...) "Dialogue" or ordinary speaker/attributes.
        dialogue_lit = top[-1]
        if dialogue_lit.value is None:
            return []
        prefix = stripped[:dialogue_lit.start].strip()
        if not prefix:
            return []

        # Reject obvious function/command-style invocations that aren't say.
        if prefix.endswith(("=", ",", ":")):
            return []
        if first_word in {"old", "new"}:
            return []

        speaker = None
        calls = _python_calls(LexicalIndex(prefix))
        if calls and calls[0].start == 0 and calls[0].name in _CHARACTER_CONSTRUCTORS:
            symbol = _character_symbol(calls[0].node, self.character_symbols)
            if symbol:
                speaker = symbol["expression"] if symbol["dynamic"] else symbol["value"]
        elif first_word:
            dm = re.match(r"([^\W\d]\w*(?:\.[^\W\d]\w*)*)", prefix)
            speaker = dm.group(1) if dm else first_word
        else:
            speaker = prefix

        values = _spoken_values(dialogue_lit, monologue_delimiter)
        return [Candidate(
            kind="dialogue", text=v, speaker=speaker,
            line=st.line + dialogue_lit.line - 1 + offset, column=st.indent + dialogue_lit.column,
            source=stripped
        ) for offset, v in values]
