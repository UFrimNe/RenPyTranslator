from __future__ import annotations

import hashlib
import re


def encode_say_string(s: str) -> str:
    s = s.replace('\\', '\\\\')
    s = s.replace('\n', '\\n')
    s = s.replace('"', '\\"')
    s = re.sub(r'(?<= ) ', r'\\ ', s)
    return '"' + s + '"'


def fallback_say_code(node) -> str:
    """Best-effort equivalent of Ren'Py/Unrpyc Say.get_code.

    Used only when the exact unrpyc implementation is unavailable. Modern compiled
    scripts normally expose TranslateSay/Translate identifiers directly, so this is
    a compatibility fallback rather than the preferred source of truth.
    """
    rv = []
    who = getattr(node, 'who', None)
    if who:
        rv.append(str(who))
    attrs = getattr(node, 'attributes', None)
    if attrs:
        rv.extend(str(x) for x in attrs)
    tattrs = getattr(node, 'temporary_attributes', None)
    if tattrs:
        rv.append('@')
        rv.extend(str(x) for x in tattrs)
    rv.append(encode_say_string(str(getattr(node, 'what', '') or '')))
    if getattr(node, 'interact', True) is False:
        rv.append('nointeract')
    explicit = getattr(node, 'explicit_identifier', False)
    identifier = getattr(node, 'identifier', None)
    if explicit and identifier:
        rv.extend(['id', str(identifier)])
    elif identifier:
        rv.extend(['id', str(identifier)])
    # Arguments are intentionally omitted here when unrpyc is unavailable. Their
    # exact canonical reconstruction differs across Ren'Py generations.
    with_ = getattr(node, 'with_', None)
    if with_:
        rv.extend(['with', str(with_)])
    return ' '.join(rv)


class IdentifierGenerator:
    """Generate Ren'Py-style identifiers for bare Say nodes.

    Exact identifiers carried by Translate/TranslateSay nodes always take priority.
    This generator mirrors the modern say-only digest path and records that it is a
    generated fallback so callers never confuse it with an identifier read from AST.
    """
    def __init__(self):
        self.identifiers: set[str] = set()
        self.preexisting: set[str] = set()

    def reserve(self, identifier):
        if isinstance(identifier, str) and identifier:
            self.preexisting.add(identifier)

    def _exists(self, identifier: str) -> bool:
        return identifier in self.identifiers or identifier in self.preexisting

    def unique_identifier(self, label: str | None, digest: str) -> str:
        base = digest if label is None else label.replace('.', '_') + '_' + digest
        identifier = base
        i = 0
        while self._exists(identifier):
            i += 1
            identifier = f'{base}_{i}'
        return identifier

    def generate(self, node, label: str | None, alternate_label: str | None = None, say_code_fn=None):
        code = (say_code_fn or fallback_say_code)(node)
        digest = hashlib.md5((code + '\r\n').encode('utf-8')).hexdigest()[:8]
        generated = self.unique_identifier(label, digest)
        id_identifier = getattr(node, 'identifier', None)

        alternate = None
        if alternate_label is not None:
            alternate = self.unique_identifier(alternate_label, digest)
            identifier = id_identifier or generated
        elif id_identifier is not None:
            alternate = generated
            identifier = id_identifier
        else:
            identifier = generated

        self.identifiers.add(identifier)
        if alternate:
            self.identifiers.add(alternate)
        return identifier, alternate, code
