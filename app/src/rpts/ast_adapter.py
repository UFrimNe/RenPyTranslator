from __future__ import annotations
import ast as py_ast
from .model import Candidate
from .translation_ids import IdentifierGenerator, fallback_say_code


def _cls(node):
    return node.__class__.__name__


def _static_expr_string(expr):
    if not isinstance(expr, str):
        return None
    s = expr.strip()
    try:
        v = py_ast.literal_eval(s)
        return v if isinstance(v, str) else None
    except Exception:
        return None


def _translation_strings(node):
    fn = getattr(node, "get_translation_strings", None)
    if not callable(fn):
        return []
    try:
        value = fn()
    except Exception:
        return []
    out = []
    for item in value or []:
        if isinstance(item, tuple) and len(item) >= 2:
            line, text = item[0], item[1]
        else:
            line, text = getattr(node, "linenumber", 0), item
        if isinstance(text, str):
            out.append((int(line or 0), text))
    return out


def _walk_objects(nodes):
    """Yield AST-ish objects recursively for identifier reservation only."""
    if nodes is None:
        return
    if not isinstance(nodes, (list, tuple)):
        nodes = [nodes]
    seen = set()
    stack = list(reversed(nodes))
    while stack:
        node = stack.pop()
        if node is None or id(node) in seen:
            continue
        seen.add(id(node))
        yield node
        for attr in ("block", "children"):
            child = getattr(node, attr, None)
            if isinstance(child, (list, tuple)):
                stack.extend(reversed(child))
        items = getattr(node, "items", None)
        if isinstance(items, (list, tuple)):
            for item in reversed(items):
                if isinstance(item, (list, tuple)) and len(item) >= 3 and isinstance(item[2], (list, tuple)):
                    stack.extend(reversed(item[2]))
        entries = getattr(node, "entries", None)
        if isinstance(entries, (list, tuple)):
            for entry in reversed(entries):
                if isinstance(entry, (list, tuple)):
                    for part in reversed(entry):
                        if isinstance(part, (list, tuple)):
                            stack.extend(reversed(part))


def extract_ast(nodes, *, say_code_fn=None, generate_translation_ids=True) -> list[Candidate]:
    """Duck-typed Ren'Py AST extraction contract.

    Stage 5 additionally preserves exact Ren'Py translation identifiers from
    Translate/TranslateSay nodes. Bare Say nodes receive a generated compatibility
    identifier when possible; those are explicitly marked as generated so they are
    never confused with an id read from compiled AST.
    """
    if nodes is None:
        return []
    if not isinstance(nodes, (list, tuple)):
        nodes = [nodes]

    idgen = IdentifierGenerator()
    for node in _walk_objects(nodes):
        name = _cls(node)
        identifier = getattr(node, 'identifier', None)
        if name in {'Translate', 'TranslateSay'} and identifier:
            idgen.reserve(identifier)
        elif name == 'Say' and identifier:
            # User-provided Say ids must also be reserved, matching Ren'Py's
            # preexisting identifier collision protection.
            idgen.reserve(identifier)

    out = []
    seen_nodes = set()
    state = {'label': None, 'alternate': None}

    def visit(node, inherited_language=None, inherited_tlid=None, inherited_tlid_alt=None, context_scope=()):
        if node is None or id(node) in seen_nodes:
            return
        seen_nodes.add(id(node))
        name = _cls(node)
        line = int(getattr(node, "linenumber", 0) or 0)
        filename = str(getattr(node, "filename", "") or "")
        node_language = getattr(node, "language", None)
        language = node_language if node_language is not None else inherited_language

        if name == 'Label':
            label_name = getattr(node, 'name', None)
            if isinstance(label_name, str) and not getattr(node, 'hide', False):
                if label_name.startswith('_'):
                    state['alternate'] = label_name
                else:
                    state['label'] = label_name
                    state['alternate'] = None

        # A Translate wrapper carries the exact translation id for its whole block.
        exact_tlid = inherited_tlid
        exact_alt = inherited_tlid_alt
        if name == 'Translate':
            node_id = getattr(node, 'identifier', None)
            if isinstance(node_id, str) and node_id:
                exact_tlid = node_id
                exact_alt = getattr(node, 'alternate', None)

        if name in {"Say", "TranslateSay"} or (
            hasattr(node, "what") and hasattr(node, "who") and name.lower().endswith("say")
        ):
            what = getattr(node, "what", None)
            who = getattr(node, "who", None)
            if isinstance(what, str):
                speaker = _static_expr_string(who)
                if speaker is None and isinstance(who, str):
                    speaker = who
                kind = "narration" if who in (None, "") else "dialogue"
                if who == "centered":
                    kind = "centered"; speaker = None
                elif who == "extend":
                    kind = "dialogue_extend"; speaker = None

                canonical_say_code = None
                try:
                    canonical_say_code = (say_code_fn or fallback_say_code)(node)
                except Exception:
                    canonical_say_code = None

                translation_id = exact_tlid
                translation_alt = exact_alt
                translation_source = 'ast_translate' if exact_tlid else None
                if name == 'TranslateSay':
                    node_id = getattr(node, 'identifier', None)
                    if isinstance(node_id, str) and node_id:
                        translation_id = node_id
                        translation_alt = getattr(node, 'alternate', None)
                        translation_source = 'ast_translate_say'
                elif translation_id is None and generate_translation_ids:
                    try:
                        translation_id, translation_alt, _ = idgen.generate(
                            node, state['label'], state['alternate'], say_code_fn=say_code_fn
                        )
                        translation_source = 'generated_canonical' if say_code_fn else 'generated_fallback'
                    except Exception:
                        translation_id = translation_alt = None
                        translation_source = None

                out.append(Candidate(
                    kind=kind, text=what, line=line, speaker=speaker,
                    language=language, source=filename, label=state['label'],
                    translation_id=translation_id,
                    translation_id_source=translation_source,
                    translation_id_alternate=translation_alt,
                    say_code=canonical_say_code,
                    context_scope=context_scope,
                ))

        elif name == "TranslateString" or (hasattr(node, "old") and hasattr(node, "new") and hasattr(node, "language")):
            old = getattr(node, "old", None)
            new = getattr(node, "new", None)
            language = getattr(node, "language", None)
            if isinstance(old, str):
                out.append(Candidate(kind="translation_old", text=old, line=line, language=language, source=filename, label=state['label']))
            if isinstance(new, str):
                out.append(Candidate(kind="translation_new", text=new, line=line, language=language, source=filename, label=state['label']))

        for sl_line, text in _translation_strings(node):
            out.append(Candidate(
                kind="ast_additional_string", text=text, line=sl_line,
                language=language, source=filename, label=state['label']
            ))

        # Common child containers across Ren'Py AST generations. A Translate block
        # inherits its exact id; normal blocks simply inherit language/context.
        child_tlid = exact_tlid if name == 'Translate' else inherited_tlid
        child_alt = exact_alt if name == 'Translate' else inherited_tlid_alt
        for attr in ("block", "children"):
            child = getattr(node, attr, None)
            if isinstance(child, (list, tuple)):
                for x in child:
                    scope=context_scope+(f'{name}:{line}',) if name in {'While','For'} else context_scope
                    visit(x, language, child_tlid, child_alt, scope)

        items = getattr(node, "items", None)
        if isinstance(items, (list, tuple)):
            for item_index,item in enumerate(items):
                if isinstance(item, (list, tuple)) and len(item) >= 1:
                    label = item[0]
                    if isinstance(label, str) and label:
                        out.append(Candidate(kind="menu", text=label, line=line, source=filename, language=language, label=state['label']))
                    if len(item) >= 3:
                        block = item[2]
                        if isinstance(block, (list, tuple)):
                            for x in block:
                                visit(x, language, inherited_tlid, inherited_tlid_alt, context_scope+(f'menu:{line}:{item_index}',))

        entries = getattr(node, "entries", None)
        if isinstance(entries, (list, tuple)):
            for entry_index,entry in enumerate(entries):
                if isinstance(entry, (list, tuple)):
                    for part in entry:
                        if isinstance(part, (list, tuple)):
                            for x in part:
                                if hasattr(x, "__class__"):
                                    visit(x, language, inherited_tlid, inherited_tlid_alt, context_scope+(f'{name}:{line}:{entry_index}',))

    for node in nodes:
        visit(node)

    dedup = []
    seen = set()
    for c in out:
        key = (c.kind, c.text, c.line, c.speaker, c.language, c.source, c.label, c.translation_id)
        if key not in seen:
            seen.add(key); dedup.append(c)
    return dedup
