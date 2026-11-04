from __future__ import annotations

import ast
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .logical import iter_logical_statements
from .multilingual import find_calls, static_string_expression

_SCREEN_DEF_RE = re.compile(r'^\s*screen\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*:\s*$')
_SCREEN_CALL_RE = re.compile(r'^\s*(?:use|show\s+screen)\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*:?.*$')
_LABEL_DEF_RE = re.compile(r'^\s*label\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*:\s*$')
_LABEL_CALL_RE = re.compile(r'^\s*call\s+([A-Za-z_]\w*)\s*(?:\((.*)\))?\s*(?:from\s+\S+)?\s*$')
_ASSIGN_RE = re.compile(r'^\s*(?:(?:define|default)\s+|\$\s*)?([A-Za-z_]\w*)\s*=\s*(.+?)\s*$')
_FOR_RE = re.compile(r'^\s*for\s+([A-Za-z_]\w*)\s+in\s+(.+?)\s*:\s*$')
_DEF_RE = re.compile(r'^\s*def\s+([A-Za-z_]\w*)\s*\((.*?)\)\s*:\s*$')
_RETURN_RE = re.compile(r'^\s*return\s+(.+?)\s*$')
_PARAM_RE = re.compile(r'^\s*([A-Za-z_]\w*)\s*(?:=\s*(.*))?$')
_SIMPLE_REF_RE = re.compile(r'^[A-Za-z_]\w*(?:(?:\.[A-Za-z_]\w*)|(?:\[[^\]]+\]))*$')
_SCREEN_PROP_WORDS = {
    'action','alternate','at','background','bar_invert','bar_resizing','bold','bottom_margin','bottom_padding',
    'button','changed','child','color','default','delay','draggable','droppable','events','first_indent','focus',
    'font','foreground','hover','hover_background','hover_color','hover_sound','id','idle','idle_background',
    'idle_color','insensitive','insensitive_background','insensitive_color','italic','kerning','keysym','language',
    'layout','left_margin','left_padding','length','modal','mouse','offset','outlines','padding','pos','prefix','range',
    'right_margin','right_padding','selected','selected_background','selected_color','sensitive','size','spacing','style',
    'style_group','style_prefix','subpixel','text_align','textalign','tooltip','top_margin','top_padding','underline',
    'value','variant','xalign','xanchor','xfill','xmaximum','xminimum','xoffset','xpos','xsize','yalign','yanchor','yfill',
    'ymaximum','yminimum','yoffset','ypos','ysize','slow_cps','slow_cps_multiplier','substitute','hyperlink_functions',
}


# Dynamic Screen Language expressions are not all missing translatable text.
# Some are the rendering layer for text already captured by Ren'Py AST nodes
# (say/menu/history), some are runtime metadata, and only the remainder need
# content observation. These rules are intentionally conservative and tied to
# well-known Ren'Py screen roles rather than arbitrary variable names.
_STANDARD_RENDER_SCREENS = {"say", "choice", "input", "history", "nvl", "notify", "confirm"}
_AST_COVERED_SIMPLE = {
    "say": {"who", "what"},
    "input": {"prompt"},
    "notify": {"message"},
    "confirm": {"message", "confirm_question", "confirm_detail"},
}
_METADATA_CALLS = {"FileTime", "FileSaveName", "FilePageName", "FileSlotName"}
_METADATA_REFS = {"game_time", "elapsed", "playtime", "timestamp", "date", "time"}
_COMMON_KEY_LABELS = {
    "ctrl", "control", "esc", "escape", "tab", "enter", "return", "space", "shift", "alt",
    "pageup", "pagedown", "home", "end", "insert", "delete", "backspace", "up", "down", "left", "right",
    "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9", "f10", "f11", "f12",
}


@dataclass
class StaticValue:
    value: Any
    source: str
    line: int
    confidence: str = 'high'


@dataclass
class ScreenDef:
    name: str
    line: int
    indent: int
    end_line: int
    params: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    loops: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class FunctionSummary:
    name: str
    line: int
    params: list[str]
    passthrough_param: str | None = None
    static_returns: list[Any] = field(default_factory=list)


@dataclass
class LabelDef:
    name: str
    line: int
    indent: int
    end_line: int
    params: list[str] = field(default_factory=list)
    defaults: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)


def _safe_eval_node(node: ast.AST, names: dict[str, StaticValue] | None = None, multilingual_helpers: set[str] | None = None):
    names = names or {}
    multilingual_helpers = multilingual_helpers or set()
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (str, int, float, bool, type(None))):
            return node.value
        raise ValueError
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        vals = [_safe_eval_node(x, names, multilingual_helpers) for x in node.elts]
        return vals if isinstance(node, ast.List) else tuple(vals)
    if isinstance(node, ast.Dict):
        return {_safe_eval_node(k, names, multilingual_helpers): _safe_eval_node(v, names, multilingual_helpers) for k, v in zip(node.keys, node.values)}
    if isinstance(node, ast.Name) and node.id in names:
        return names[node.id].value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        a, b = _safe_eval_node(node.left, names, multilingual_helpers), _safe_eval_node(node.right, names, multilingual_helpers)
        if isinstance(a, (str, list, tuple)) and isinstance(b, type(a)):
            return a + b
        raise ValueError
    if isinstance(node, ast.IfExp):
        a = _safe_eval_node(node.body, names, multilingual_helpers)
        b = _safe_eval_node(node.orelse, names, multilingual_helpers)
        if a == b:
            return a
        return [a, b]
    if isinstance(node, ast.Call):
        fn = None
        if isinstance(node.func, ast.Name):
            fn = node.func.id
        elif isinstance(node.func, ast.Attribute):
            fn = node.func.attr
        if fn in {'_', '__', '___', '_p'} and node.args:
            return _safe_eval_node(node.args[0], names, multilingual_helpers)
        if fn in multilingual_helpers and len(node.args) >= 2:
            values = [_safe_eval_node(a, names, multilingual_helpers) for a in node.args]
            if all(isinstance(v, str) for v in values):
                return values
    raise ValueError


def safe_eval_expr(expr: str, names: dict[str, StaticValue] | None = None, multilingual_helpers: set[str] | None = None):
    try:
        node = ast.parse(expr.strip(), mode='eval').body
        return _safe_eval_node(node, names, multilingual_helpers)
    except Exception:
        return None


def _split_args(source: str) -> list[str]:
    # Python's parser is useful when the expression is valid Python. Fall back to a
    # conservative comma splitter for Ren'Py-ish call syntax.
    try:
        node = ast.parse('f(' + source + ')', mode='eval').body
        out = []
        for a in node.args:
            out.append(ast.get_source_segment('f(' + source + ')', a) or '')
        for kw in node.keywords:
            seg = ast.get_source_segment('f(' + source + ')', kw.value) or ''
            out.append((kw.arg + '=' if kw.arg else '**') + seg)
        return out
    except Exception:
        parts, start, depth, quote, esc = [], 0, 0, None, False
        for i, ch in enumerate(source):
            if quote:
                if esc: esc = False
                elif ch == '\\': esc = True
                elif ch == quote: quote = None
                continue
            if ch in "'\"": quote = ch; continue
            if ch in '([{': depth += 1
            elif ch in ')]}': depth = max(0, depth - 1)
            elif ch == ',' and depth == 0:
                parts.append(source[start:i].strip()); start = i + 1
        parts.append(source[start:].strip())
        return [x for x in parts if x]


def _parse_params(raw: str | None):
    params, defaults = [], {}
    if not raw:
        return params, defaults
    for part in _split_args(raw):
        if part.startswith('*'):
            continue
        if '=' in part:
            name, expr = part.split('=', 1)
            name = name.strip()
            if re.match(r'^[A-Za-z_]\w*$', name):
                params.append(name)
                val = safe_eval_expr(expr)
                if val is not None:
                    defaults[name] = val
        else:
            name = part.strip()
            if re.match(r'^[A-Za-z_]\w*$', name):
                params.append(name)
    return params, defaults


def _parse_call_args(raw: str | None, params: list[str], names: dict[str, StaticValue], multilingual_helpers: set[str] | None = None):
    result: dict[str, Any] = {}
    if not raw:
        return result
    pos = 0
    for part in _split_args(raw):
        # keyword only when lhs is a simple name.
        m = re.match(r'^([A-Za-z_]\w*)\s*=\s*(.*)$', part, re.S)
        if m:
            val = safe_eval_expr(m.group(2), names, multilingual_helpers)
            if val is not None:
                result[m.group(1)] = val
            continue
        if pos < len(params):
            val = safe_eval_expr(part, names, multilingual_helpers)
            if val is not None:
                result[params[pos]] = val
        pos += 1
    return result


def _flatten_strings(value: Any) -> list[str]:
    out: list[str] = []
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values(): out.extend(_flatten_strings(v))
    elif isinstance(value, (list, tuple, set)):
        for v in value: out.extend(_flatten_strings(v))
    return out


def _leading_expression(expr: str) -> str:
    s = (expr or '').strip()
    if s.endswith(':'):
        s = s[:-1].rstrip()
    # Python sinks already contain a complete expression. Only SL properties
    # need trimming; never drop an operator/suffix from a valid Python value.
    try:
        ast.parse(s, mode='eval')
        return s
    except (SyntaxError, ValueError):
        pass
    # Calls are self-delimiting; preserve their full call but discard SL properties.
    calls = find_calls(s)
    if calls and calls[0].start_pos == 0:
        return s[:calls[0].end_pos]
    # For refs like `i.caption action ...` or `who id ...`, first token is the display expression.
    m = re.match(r'([A-Za-z_]\w*(?:(?:\.[A-Za-z_]\w*)|(?:\[[^\]]+\]))*)', s)
    if m:
        return m.group(1)
    # Parenthesized composite: leave intact; resolver will likely mark unresolved.
    return s


class FileFlow:
    def __init__(self, source: str, multilingual_helpers: set[str] | None = None):
        self.source = source
        self.multilingual_helpers = set(multilingual_helpers or ())
        self.lines = source.splitlines()
        self.statements = iter_logical_statements(source)
        self.assignments: dict[str, StaticValue] = {}
        self.project_assignments: dict[str, StaticValue] = {}
        self.screens: dict[str, ScreenDef] = {}
        self.functions: dict[str, FunctionSummary] = {}
        self.labels: dict[str, LabelDef] = {}
        self.aliases: dict[str, str] = {}
        self._scan()

    def _scan(self):
        # Pass 1: top-level/simple static assignments. We deliberately accept only
        # simple names and safe literal expressions; no game code is executed.
        for st in self.statements:
            raw = st.text.strip()
            m = _ASSIGN_RE.match(raw)
            if m:
                name, expr = m.group(1), m.group(2)
                # Avoid comparisons/keyword properties accidentally matching.
                if raw.startswith(('if ', 'elif ', 'while ', 'for ')):
                    continue
                val = safe_eval_expr(expr, self.assignments, self.multilingual_helpers)
                if val is not None:
                    self.assignments[name] = StaticValue(val, raw, st.line)

        # Pass 2: screen definitions and block extents.
        sts = self.statements
        for i, st in enumerate(sts):
            m = _SCREEN_DEF_RE.match(st.text)
            if not m:
                continue
            params, defaults = _parse_params(m.group(2))
            end_line = len(self.lines) + 1
            for later in sts[i+1:]:
                if later.indent <= st.indent:
                    end_line = later.line - 1
                    break
            self.screens[m.group(1)] = ScreenDef(m.group(1), st.line, st.indent, end_line, params, defaults)

        # Label definitions are useful for small helper labels that display a
        # parameter through Text()/show expression. Calls are collected project-wide
        # later, just like screen callsites.
        for i, st in enumerate(sts):
            m = _LABEL_DEF_RE.match(st.text)
            if not m:
                continue
            params, defaults = _parse_params(m.group(2))
            end_line = len(self.lines) + 1
            for later in sts[i+1:]:
                if later.indent <= st.indent:
                    end_line = later.line - 1
                    break
            self.labels[m.group(1)] = LabelDef(m.group(1), st.line, st.indent, end_line, params, defaults)

        # Safe aliases. A common Ren'Py pattern is:
        #   $ localized = renpy.translation.translate_string(line)
        # Treat translate_string as an extraction-time identity, not as code to run.
        for st in sts:
            raw = st.text.strip()
            m = _ASSIGN_RE.match(raw)
            if not m:
                continue
            name, expr = m.group(1), m.group(2).strip()
            if _SIMPLE_REF_RE.match(expr):
                self.aliases[name] = expr
                continue
            try:
                node = ast.parse(expr, mode='eval').body
                if isinstance(node, ast.Call) and node.args:
                    fn = None
                    if isinstance(node.func, ast.Name): fn = node.func.id
                    elif isinstance(node.func, ast.Attribute): fn = node.func.attr
                    if fn in {'translate_string'}:
                        seg = ast.get_source_segment(expr, node.args[0]) or ''
                        if _SIMPLE_REF_RE.match(seg.strip()):
                            self.aliases[name] = seg.strip()
            except Exception:
                pass

        # Screen callsites anywhere in file.
        for st in sts:
            m = _SCREEN_CALL_RE.match(st.text)
            if not m or m.group(1) not in self.screens:
                continue
            sd = self.screens[m.group(1)]
            vals = _parse_call_args(m.group(2), sd.params, self.assignments, self.multilingual_helpers)
            sd.calls.append({'line': st.line, 'values': vals, 'source': st.text.strip()})

        # Loop bindings inside screens.
        for sd in self.screens.values():
            for st in sts:
                if not (sd.line < st.line <= sd.end_line):
                    continue
                m = _FOR_RE.match(st.text)
                if m:
                    sd.loops.append({'name': m.group(1), 'expr': m.group(2), 'line': st.line, 'indent': st.indent})

        # Very conservative Python function summaries: direct `return param` passthrough
        # and static return literals. Works without executing Python blocks.
        for i, st in enumerate(sts):
            m = _DEF_RE.match(st.text)
            if not m:
                continue
            params, _ = _parse_params(m.group(2))
            fs = FunctionSummary(m.group(1), st.line, params)
            for later in sts[i+1:]:
                if later.indent <= st.indent:
                    break
                rm = _RETURN_RE.match(later.text)
                if not rm:
                    continue
                rexpr = rm.group(1).strip()
                if rexpr in params:
                    fs.passthrough_param = rexpr
                val = safe_eval_expr(rexpr, self.assignments, self.multilingual_helpers)
                if val is not None and val not in fs.static_returns:
                    fs.static_returns.append(val)
            self.functions[fs.name] = fs

    def all_assignments(self) -> dict[str, StaticValue]:
        merged = dict(self.project_assignments)
        merged.update(self.assignments)
        return merged

    def screen_at(self, line: int) -> ScreenDef | None:
        matches = [x for x in self.screens.values() if x.line <= line <= x.end_line]
        if matches:
            return max(matches, key=lambda x: x.line)
        # Fallback for decompiler output that the logical statement splitter may
        # not model perfectly. Scan physical lines backwards for the nearest
        # enclosing `screen name(...):` and verify indentation.
        if 1 <= line <= len(self.lines):
            current = self.lines[line-1]
            current_indent = len(current) - len(current.lstrip(' \t'))
            for n in range(line, 0, -1):
                raw = self.lines[n-1]
                m = re.match(r'^\s*screen\s+([A-Za-z_]\w*)\s*(?:\((.*?)\))?\s*:', raw)
                if not m:
                    continue
                header_indent = len(raw) - len(raw.lstrip(' \t'))
                if n == line or current_indent > header_indent:
                    params, defaults = _parse_params(m.group(2))
                    return ScreenDef(m.group(1), n, header_indent, line, params, defaults)
        return None

    def label_at(self, line: int) -> LabelDef | None:
        matches = [x for x in self.labels.values() if x.line <= line <= x.end_line]
        return max(matches, key=lambda x: x.line) if matches else None

    def context_snippet(self, line: int, radius: int = 2) -> str:
        if not self.lines or line <= 0:
            return ""
        start = max(1, line - radius)
        end = min(len(self.lines), line + radius)
        return "\\n".join(f"{n}: {self.lines[n-1]}" for n in range(start, end + 1))

    def dynamic_disposition(self, normalized: str, line: int):
        """Classify an unresolved display expression without pretending it is text.

        This answers a different question from ``resolve``: whether runtime observation
        is actually useful for translation. A say-screen ``what`` is dynamic, but the
        dialogue itself has already been captured from Ren'Py AST and must not become a
        second translation unit.
        """
        expr = (normalized or "").strip()
        sd = self.screen_at(line)
        screen_name = sd.name if sd else ""
        lower_screen = screen_name.lower()

        calls = find_calls(expr)
        if calls and calls[0].start_pos == 0:
            fn = calls[0].function.rsplit('.', 1)[-1]
            if fn == "FileTime":
                return "runtime_localization", "Ren'Py save timestamp; static format children are extracted separately", screen_name
            if fn in {"FileSaveName", "FilePageName", "FileSlotName"}:
                return "runtime_metadata", "Ren'Py save/UI metadata should not be semantically translated", screen_name
            if "debug" in fn.lower() or fn.lower().startswith("test_") or fn.lower().endswith("_test"):
                return "developer_only", "debug/test display helper", screen_name

        # Core say rendering is already represented by Say/TranslateSay AST nodes.
        if lower_screen == "say":
            if expr in {"who", "what"}:
                return "already_covered", "say screen renders AST dialogue/name already extracted", screen_name
            try:
                node = ast.parse(expr, mode='eval')
                names = {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}
                if "what" in names and names <= {"what"}:
                    return "already_covered", "say screen decorates AST dialogue already extracted", screen_name
            except Exception:
                pass

        # Choice captions come from Menu AST. The default/custom choice screen commonly
        # iterates `items` and renders `<loop>.caption`.
        if lower_screen == "choice" and re.search(r'(?:^|[.\[])caption(?:\]|$)', expr):
            return "already_covered", "choice caption is already extracted from Menu AST", screen_name

        # History re-renders dialogue that was already extracted at its source.
        if lower_screen == "history" and re.search(r'\.(?:who|what)$', expr):
            return "already_covered", "history screen re-renders previously extracted dialogue", screen_name

        # Structural fallbacks for common Ren'Py renderer patterns. These are
        # deliberately based on nearby Screen Language structure, not only on a
        # variable name, so custom `text what` outside a say/history renderer is
        # not silently discarded. This also recovers context when an older or
        # customized screen header was not recognized by the decompiler scanner.
        context = self.context_snippet(line, radius=3)
        ctx = context.lower()
        if expr in {'who', 'what'} and (
            'id \"who\"' in ctx or 'id \"what\"' in ctx or
            'style \"say_' in ctx or 'style \"bubble_' in ctx
        ):
            return 'already_covered', 'renderer displays Say/TranslateSay AST content already extracted', screen_name
        if 'what' in expr and ('say_dialogue' in ctx or 'dialogue_continue_icon_inline' in ctx):
            return 'already_covered', 'say decoration re-renders AST dialogue already extracted', screen_name
        if re.search(r'(?:^|[.\[])caption(?:\]|$)', expr) and (
            'for i in items' in ctx or 'action i.action' in ctx or 'choice_selected_action' in ctx
        ):
            return 'already_covered', 'choice caption is already extracted from Menu AST', screen_name
        if re.fullmatch(r'[hd]\.(?:who|what)', expr) or (expr == 'what' and ('history_' in ctx or 'h.who' in ctx)):
            return 'already_covered', 'history/NVL renderer re-renders dialogue already extracted from AST', screen_name
        if expr == 'test_label' and ('test_target' in ctx or 'start(test_target)' in ctx):
            return 'developer_only', 'developer/test navigation label', screen_name
        if expr.endswith('[\"no\"]') and ('gallery_number' in ctx or 'gallery' in lower_screen):
            return 'runtime_metadata', 'gallery/card ordinal identifier', screen_name

        # Common runtime clocks/counters are metadata, not language-bearing content.
        simple_tail = re.split(r'[.\[]', expr, 1)[0].lower() if expr else ""
        subscript_keys = re.findall(r'[\"\']([^\"\']+)[\"\']', expr)
        if simple_tail in _METADATA_REFS or any(k.lower() in _METADATA_REFS for k in subscript_keys):
            return "runtime_metadata", "runtime clock/counter metadata", screen_name

        return "runtime_content", "runtime value may contain user-visible language", screen_name

    def _resolve_ref(self, ref: str, line: int):
        # Direct assignment / parameter / safe alias.
        if re.match(r'^[A-Za-z_]\w*$', ref):
            if ref in self.aliases and self.aliases[ref] != ref:
                vals, kind, source = self._resolve_ref(self.aliases[ref], line)
                if vals:
                    return vals, 'alias_' + (kind or 'reference'), f'{ref} -> {source}'
            sd = self.screen_at(line)
            if sd and ref in sd.params:
                vals = []
                if ref in sd.defaults:
                    vals.append(sd.defaults[ref])
                for call in sd.calls:
                    if ref in call['values']:
                        vals.append(call['values'][ref])
                strings = []
                for v in vals: strings.extend(_flatten_strings(v))
                if strings:
                    return sorted(dict.fromkeys(strings)), 'screen_parameter', f'screen {sd.name}.{ref}'
            ld = self.label_at(line)
            if ld and ref in ld.params:
                vals = []
                if ref in ld.defaults:
                    vals.append(ld.defaults[ref])
                for call in ld.calls:
                    if ref in call['values']:
                        vals.append(call['values'][ref])
                strings = []
                for v in vals:
                    strings.extend(_flatten_strings(v))
                if strings:
                    return sorted(dict.fromkeys(strings)), 'label_parameter', f'label {ld.name}.{ref}'
            names = self.all_assignments()
            if ref in names:
                kind = 'static_assignment' if ref in self.assignments else 'project_static_assignment'
                return _flatten_strings(names[ref].value), kind, names[ref].source
            return [], None, None

        # base[static_key/index]
        try:
            node = ast.parse(ref, mode='eval').body
        except Exception:
            node = None
        names = self.all_assignments()
        if isinstance(node, ast.Subscript) and isinstance(node.value, ast.Name) and node.value.id in names:
            base = names[node.value.id].value
            try:
                key = _safe_eval_node(node.slice, names, self.multilingual_helpers)
                value = base[key]
                return _flatten_strings(value), 'static_subscript', f'{node.value.id}[{key!r}]'
            except Exception:
                pass

        # loopvar.field / loopvar["field"] from a static collection.
        sd = self.screen_at(line)
        if sd:
            for loop in reversed(sd.loops):
                if loop['line'] >= line:
                    continue
                name = loop['name']
                if not ref.startswith(name + '.') and not ref.startswith(name + '['):
                    continue
                coll = safe_eval_expr(loop['expr'], self.all_assignments(), self.multilingual_helpers)
                if coll is None and re.match(r'^[A-Za-z_]\w*$', loop['expr']) and loop['expr'] in self.all_assignments():
                    coll = self.all_assignments()[loop['expr']].value
                if not isinstance(coll, (list, tuple)):
                    continue
                values = []
                for item in coll:
                    try:
                        if ref.startswith(name + '.'):
                            key = ref[len(name)+1:]
                            if isinstance(item, dict) and key in item:
                                values.extend(_flatten_strings(item[key]))
                        else:
                            n = ast.parse(ref, mode='eval').body
                            key = _safe_eval_node(n.slice, self.all_assignments(), self.multilingual_helpers)
                            if isinstance(item, (dict, list, tuple)):
                                values.extend(_flatten_strings(item[key]))
                    except Exception:
                        pass
                if values:
                    return sorted(dict.fromkeys(values)), 'static_loop_collection', loop['expr']
        return [], None, None

    def resolve(self, expression: str, line: int):
        expr = _leading_expression(expression)
        if _SIMPLE_REF_RE.match(expr):
            vals, kind, source = self._resolve_ref(expr, line)
            if vals:
                return vals, kind, source, expr

        static = safe_eval_expr(expr, self.all_assignments(), self.multilingual_helpers)
        if isinstance(static, str):
            return [static], 'static_expression', expr, expr

        calls = find_calls(expr)
        if calls and calls[0].start_pos == 0 and calls[0].end_pos == len(expr):
            call = calls[0]
            fn = call.function.rsplit('.', 1)[-1]
            fs = self.functions.get(fn)
            if fs:
                args = list(call.args)
                if fs.passthrough_param and fs.passthrough_param in fs.params:
                    idx = fs.params.index(fs.passthrough_param)
                    if idx < len(args):
                        a = args[idx]
                        sval = safe_eval_expr(a, self.all_assignments(), self.multilingual_helpers)
                        if isinstance(sval, str):
                            return [sval], 'function_passthrough_static', f'{fn} -> {fs.passthrough_param}', expr
                        vals, kind, source = self._resolve_ref(a.strip(), line)
                        if vals:
                            return vals, 'function_passthrough_' + (kind or 'reference'), f'{fn} -> {source}', expr
                returns = []
                for v in fs.static_returns: returns.extend(_flatten_strings(v))
                if returns:
                    return sorted(dict.fromkeys(returns)), 'function_static_return', fn, expr
        return [], None, None, expr


def analyze_dataflow(dynamic_rows: list[dict], sources: dict[str, str], multilingual_helpers: set[str] | None = None):
    helpers = set(multilingual_helpers or ())
    flows = {file: FileFlow(text, multilingual_helpers=helpers) for file, text in sources.items() if text}

    # Project-wide static defines/defaults. Ren'Py store values and screen calls
    # routinely cross file boundaries. Only unique, declarative assignments are
    # shared; runtime `$` assignments are deliberately kept file-local.
    declarations: dict[str, list[StaticValue]] = {}
    for flow in flows.values():
        for name, value in flow.assignments.items():
            raw = value.source.lstrip()
            if raw.startswith(("define ", "default ")):
                declarations.setdefault(name, []).append(value)
    project_assignments = {name: vals[0] for name, vals in declarations.items() if len(vals) == 1}
    for flow in flows.values():
        flow.project_assignments = project_assignments

    # Rebuild screen callsites project-wide, so a screen declared in screens.rpy
    # can be resolved from `use/show screen` in another file. Duplicate screen
    # names are conservatively fed the same static call alternatives.
    screens_by_name: dict[str, list[tuple[str, FileFlow, ScreenDef]]] = {}
    for filename, flow in flows.items():
        for sd in flow.screens.values():
            sd.calls = []
            screens_by_name.setdefault(sd.name, []).append((filename, flow, sd))
    for caller_file, caller in flows.items():
        for st in caller.statements:
            m = _SCREEN_CALL_RE.match(st.text)
            if not m:
                continue
            targets = screens_by_name.get(m.group(1), [])
            for target_file, target_flow, sd in targets:
                vals = _parse_call_args(m.group(2), sd.params, caller.all_assignments(), helpers)
                sd.calls.append({
                    'line': st.line, 'values': vals, 'source': st.text.strip(),
                    'caller_file': caller_file, 'target_file': target_file,
                })

    labels_by_name: dict[str, list[tuple[str, FileFlow, LabelDef]]] = {}
    for filename, flow in flows.items():
        for ld in flow.labels.values():
            ld.calls = []
            labels_by_name.setdefault(ld.name, []).append((filename, flow, ld))
    for caller_file, caller in flows.items():
        for st in caller.statements:
            m = _LABEL_CALL_RE.match(st.text)
            if not m:
                continue
            for target_file, target_flow, ld in labels_by_name.get(m.group(1), []):
                vals = _parse_call_args(m.group(2), ld.params, caller.all_assignments(), helpers)
                ld.calls.append({'line': st.line, 'values': vals, 'source': st.text.strip(),
                                 'caller_file': caller_file, 'target_file': target_file})

    resolved, unresolved, source_rows = [], [], []
    for row in dynamic_rows:
        r = dict(row)
        flow = flows.get(str(r.get('file') or ''))
        if flow is None:
            r['dataflow_status'] = 'no_source_available'
            r['normalized_expression'] = _leading_expression(str(r.get('expression') or ''))
            r['screen_name'] = ''
            r['source_context'] = ''
            r['dynamic_disposition'] = 'runtime_content'
            r['disposition_reason'] = 'decompiled/source text unavailable for static tracing'
            unresolved.append(r)
            continue
        try:
            line = int(r.get('line') or 0)
        except Exception:
            line = 0
        values, kind, provenance, normalized = flow.resolve(str(r.get('expression') or ''), line)
        r['normalized_expression'] = normalized
        sd = flow.screen_at(line)
        r['screen_name'] = sd.name if sd else ''
        r['source_context'] = flow.context_snippet(line)
        if values:
            r['dataflow_status'] = 'resolved_static'
            r['resolution_kind'] = kind or ''
            r['provenance'] = provenance or ''
            r['resolved_count'] = len(values)
            r['resolved_values'] = ' | '.join(v.replace('\n', '\\n') for v in values)
            r['dynamic_disposition'] = 'resolved_static'
            r['disposition_reason'] = 'static data-flow proof'
            resolved.append(r)
            for v in values:
                source_rows.append({
                    'file': r.get('file'), 'line': r.get('line'), 'kind': r.get('kind'),
                    'expression': r.get('expression'), 'normalized_expression': normalized,
                    'resolution_kind': kind or '', 'provenance': provenance or '', 'text': v,
                    'screen_name': r.get('screen_name') or '', 'source_context': r.get('source_context') or '',
                    'layer': 'source', 'dynamic': False, 'speaker': '', 'column': r.get('column') or '',
                    'language': r.get('language') or '', 'source': r.get('source') or '',
                })
        else:
            disposition, reason, screen_name = flow.dynamic_disposition(normalized, line)
            r['dataflow_status'] = 'runtime_needed' if disposition == 'runtime_content' else disposition
            r['resolution_kind'] = ''
            r['provenance'] = ''
            r['resolved_count'] = 0
            r['resolved_values'] = ''
            r['screen_name'] = screen_name or r.get('screen_name') or ''
            r['dynamic_disposition'] = disposition
            r['disposition_reason'] = reason
            unresolved.append(r)
    return resolved, unresolved, source_rows


def is_probable_input_key_label(row: dict) -> bool:
    """Conservative detector for key-name labels found via screen parameters."""
    text = str(row.get('text') or '').strip()
    if not text:
        return False
    norm = str(row.get('normalized_expression') or '').lower()
    provenance = str(row.get('provenance') or '').lower()
    if 'key' not in norm and '.key' not in provenance:
        return False
    compact = re.sub(r'[^a-z0-9]+', '', text.lower())
    return compact in _COMMON_KEY_LABELS or (len(compact) == 1 and compact.isalnum())


def runtime_probe_source() -> str:
    """Return an opt-in Ren'Py script that logs displayed strings unchanged.

    The probe is never installed by the audit command. It chains an existing
    ``config.replace_text`` hook and records a best-effort Ren'Py filename/line
    alongside the displayed text. The location API is optional at runtime so
    older engines gracefully fall back to an empty location.
    """
    return r'''# RPTS Stage 4.1 runtime probe. OPTIONAL; generated by the audit tool.
# Copy this file into game/ only for a test run, then remove it afterwards.
# It does not translate or replace text. It logs unique displayed strings with best-effort context.

init 999 python:
    import os
    import io

    try:
        _rpts_text_type = unicode
    except NameError:
        _rpts_text_type = str

    _rpts_seen_text = set()
    _rpts_previous_replace_text = config.replace_text

    def _rpts_probe_location():
        try:
            fn = getattr(renpy, "get_filename_line", None)
            if fn is not None:
                value = fn()
                if value and len(value) >= 2:
                    return _rpts_text_type(value[0]), int(value[1])
        except Exception:
            pass
        return "", 0

    def _rpts_probe_replace_text(value):
        result = value
        if _rpts_previous_replace_text is not None:
            result = _rpts_previous_replace_text(value)
        try:
            if result is None:
                return result
            text = _rpts_text_type(result)
            filename, lineno = _rpts_probe_location()
            key = (filename, lineno, text)
            if text and key not in _rpts_seen_text:
                _rpts_seen_text.add(key)
                safe_file = filename.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
                safe_text = text.replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")
                path = os.path.join(config.savedir, "rpts_runtime_strings.tsv")
                with io.open(path, "a", encoding="utf-8") as fh:
                    fh.write(safe_file + "\t" + _rpts_text_type(lineno) + "\t" + safe_text + "\n")
        except Exception:
            pass
        return result

    config.replace_text = _rpts_probe_replace_text
'''
