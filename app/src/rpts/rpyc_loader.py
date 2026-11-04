from __future__ import annotations

"""Minimal, restricted RPYC reader used by the test suite.

This module intentionally does *not* use pickle.loads() with arbitrary imports.
Ren'Py .rpyc data is pickle-based, so a normal unpickler could execute globals
referenced by an untrusted game. The restricted unpickler below only allows a
small safe builtin/collections set and replaces renpy.* and narrowly approved Ren'Py store classes with inert
placeholder classes.

It is not a decompiler. Its purpose is to recover enough object state for the
native AST extractor (Say/Menu/TranslateString and child blocks). For complete
screen/custom-statement recovery, use the optional unrpyc backend.
"""

from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
import builtins
import collections
import codecs
import pickle
import struct
import zlib


class RpycError(Exception):
    pass


class BadRpycHeader(RpycError):
    pass


class UnsafePickleGlobal(RpycError):
    pass


class FakeRenPyObject:
    """Inert stand-in for arbitrary Ren'Py classes referenced by a pickle."""
    def __new__(cls, *args, **kwargs):
        obj = object.__new__(cls)
        if args:
            obj._new_args = args
        if kwargs:
            obj._new_kwargs = kwargs
        return obj

    def __init__(self, *args, **kwargs):
        pass

    def __setstate__(self, state):
        slotstate = None
        if (
            isinstance(state, tuple)
            and len(state) == 2
            and (state[0] is None or isinstance(state[0], dict))
            and (state[1] is None or isinstance(state[1], dict))
        ):
            state, slotstate = state

        if isinstance(state, dict):
            self.__dict__.update(state)
        elif state is not None:
            self._setstate = state

        if isinstance(slotstate, dict):
            self.__dict__.update(slotstate)


class FakePyExpr(str):
    def __new__(cls, value="", filename=None, linenumber=0, *args, **kwargs):
        obj = str.__new__(cls, value)
        obj.filename = filename
        obj.linenumber = linenumber
        if args:
            obj._new_args = args
        return obj

    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)


class FakeRevertableList(list):
    def __new__(cls, *args, **kwargs):
        return list.__new__(cls)
    def __init__(self, *args, **kwargs):
        if args:
            try:
                list.__init__(self, *args)
            except Exception:
                list.__init__(self)
    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)


class FakeRevertableDict(dict):
    def __new__(cls, *args, **kwargs):
        return dict.__new__(cls)
    def __init__(self, *args, **kwargs):
        if args:
            try:
                dict.__init__(self, *args)
            except Exception:
                dict.__init__(self)
    def __setstate__(self, state):
        if isinstance(state, dict):
            self.__dict__.update(state)


class FakeRevertableSet(set):
    def __new__(cls, *args, **kwargs):
        return set.__new__(cls)
    def __init__(self, *args, **kwargs):
        if args:
            try:
                set.__init__(self, *args)
            except Exception:
                set.__init__(self)
    def __setstate__(self, state):
        if isinstance(state, tuple) and state and isinstance(state[0], dict):
            self.update(state[0].keys())
        elif isinstance(state, (set, frozenset, list, tuple, dict)):
            self.update(state.keys() if isinstance(state, dict) else state)


class FakeGroupedLine(tuple):
    def __new__(cls, *args):
        return tuple.__new__(cls, args)


@dataclass(frozen=True)
class LoadedRpyc:
    ast: object
    format: str
    compressed_size: int
    pickle_size: int


_CLASS_CACHE: dict[tuple[str, str], type] = {}


def _fake_class(module: str, name: str) -> type:
    key = (module, name)
    if key in _CLASS_CACHE:
        return _CLASS_CACHE[key]

    if name == "PyExpr" and module in {"renpy.ast", "renpy.astsupport", "renpy.python"}:
        base = FakePyExpr
    elif name in {"RevertableList", "RevertableObjectList"}:
        base = FakeRevertableList
    elif name in {"RevertableDict", "RevertableObjectDict"}:
        base = FakeRevertableDict
    elif name in {"RevertableSet", "RevertableObjectSet"}:
        base = FakeRevertableSet
    elif name == "GroupedLine" and module == "renpy.lexer":
        base = FakeGroupedLine
    else:
        base = FakeRenPyObject

    cls = type(name, (base,), {"__module__": module})
    _CLASS_CACHE[key] = cls
    return cls


# Explicit allow-list of inert constructors that appear in Ren'Py AST pickles.
# Keep this list narrow: unlike unrpyc's safe_modules={"collections"}, the
# native reader never imports an arbitrary object from collections/builtins.
_SAFE_GLOBALS = {
    ("builtins", "object"): object,
    ("builtins", "list"): list,
    ("builtins", "dict"): dict,
    ("builtins", "tuple"): tuple,
    ("builtins", "set"): set,
    ("builtins", "frozenset"): frozenset,
    ("builtins", "slice"): slice,
    ("builtins", "complex"): complex,
    ("builtins", "str"): str,
    ("builtins", "bytes"): bytes,
    ("builtins", "bytearray"): bytearray,
    ("builtins", "int"): int,
    ("builtins", "float"): float,
    ("builtins", "bool"): bool,

    # Python 2 names still occur in Ren'Py 6/7 protocol-2 pickles.
    ("__builtin__", "object"): object,
    ("__builtin__", "list"): list,
    ("__builtin__", "dict"): dict,
    ("__builtin__", "tuple"): tuple,
    ("__builtin__", "set"): set,
    ("__builtin__", "frozenset"): frozenset,
    ("__builtin__", "str"): str,
    ("__builtin__", "bytes"): bytes,
    ("__builtin__", "bytearray"): bytearray,
    ("__builtin__", "int"): int,
    ("__builtin__", "float"): float,
    ("__builtin__", "bool"): bool,

    ("collections", "OrderedDict"): collections.OrderedDict,
    ("collections", "defaultdict"): collections.defaultdict,
    ("collections", "deque"): collections.deque,
    ("collections", "Counter"): collections.Counter,
    ("_codecs", "encode"): codecs.encode,
}


class RestrictedRenPyUnpickler(pickle.Unpickler):
    def find_class(self, module, name):
        allowed = _SAFE_GLOBALS.get((module, name))
        if allowed is not None:
            return allowed
        if module == "copyreg" and name == "_reconstructor":
            import copyreg
            return copyreg._reconstructor
        if module == "copy_reg" and name == "_reconstructor":
            import copyreg
            return copyreg._reconstructor
        if module.startswith("renpy"):
            return _fake_class(module, name)
        # Ren'Py defines layeredimage helper classes from common/00layeredimage_ren.py
        # in the script store namespace. RPYC pickles can therefore reference globals
        # such as store.layeredimage.RawLayeredImage / RawAttribute. Never import the
        # real store module here: map every global from this one exact Ren'Py-owned
        # namespace to an inert fake class, just like renpy.* above. Other store.*
        # modules remain blocked so an untrusted game cannot smuggle arbitrary callables.
        if module == "store.layeredimage":
            return _fake_class(module, name)
        raise UnsafePickleGlobal(f"Blocked pickle global: {module}.{name}")


def _extract_pickle_blob(raw: bytes) -> tuple[bytes, str]:
    if raw.startswith(b"RENPY RPC2"):
        position = 10
        chunks = {}
        # Each entry is slot/start/length uint32, terminated by slot 0.
        while True:
            if position + 12 > len(raw):
                raise BadRpycHeader("Truncated RPYC2 slot table")
            slot, start, length = struct.unpack("III", raw[position:position + 12])
            position += 12
            if slot == 0:
                break
            if start + length > len(raw):
                raise BadRpycHeader(f"RPYC2 slot {slot} points outside the file")
            chunks[slot] = raw[start:start + length]
        if 1 not in chunks:
            raise BadRpycHeader("RPYC2 has no slot 1")
        return chunks[1], "rpyc2"
    return raw, "rpyc1"


def _restricted_loads(data: bytes):
    errors = []
    # Ren'Py 8 is normally ASCII-clean protocol-2/3+ pickle; Ren'Py 6/7 data
    # can contain Python-2 strings, for which latin1 is the lossless bridge.
    for encoding in ("ASCII", "latin1"):
        try:
            return RestrictedRenPyUnpickler(BytesIO(data), fix_imports=False, encoding=encoding, errors="strict").load()
        except UnicodeDecodeError as e:
            errors.append(e)
            continue
    if errors:
        raise errors[-1]
    raise RpycError("Unable to unpickle RPYC payload")


MAX_RPYC_PICKLE_BYTES = 128 * 1024 * 1024


def _limited_zlib_decompress(data: bytes, limit: int) -> bytes:
    dec = zlib.decompressobj()
    out = dec.decompress(data, limit + 1)
    if len(out) > limit or dec.unconsumed_tail or not dec.eof:
        raise BadRpycHeader(f"RPYC payload exceeds safety limit ({limit} bytes)")
    return out


def load_rpyc_bytes(raw: bytes) -> LoadedRpyc:
    packed, fmt = _extract_pickle_blob(raw)
    try:
        payload = _limited_zlib_decompress(packed, MAX_RPYC_PICKLE_BYTES)
    except zlib.error as e:
        raise BadRpycHeader(f"Expected zlib-compressed RPYC payload: {e}") from e

    loaded = _restricted_loads(payload)
    if isinstance(loaded, tuple) and len(loaded) >= 2:
        ast = loaded[1]
    else:
        ast = loaded
    return LoadedRpyc(ast=ast, format=fmt, compressed_size=len(packed), pickle_size=len(payload))


def load_rpyc(path: str | Path) -> LoadedRpyc:
    return load_rpyc_bytes(Path(path).read_bytes())
