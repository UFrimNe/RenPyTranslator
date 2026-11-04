from __future__ import annotations

from dataclasses import replace
from io import StringIO
from pathlib import Path
import importlib
import sys

from .ast_adapter import extract_ast
from .interactive import bundle_from_ast
from .parser import RenPySourceParser
from .rpyc_loader import load_rpyc, load_rpyc_bytes


class BackendUnavailable(RuntimeError):
    pass


def _unrpyc_root(explicit: str | Path | None = None) -> Path | None:
    candidates = []
    if explicit:
        candidates.append(Path(explicit))
    here = Path(__file__).resolve()
    project = here.parents[2]
    candidates += [
        project / "vendor" / "unrpyc",
        project / "vendor" / "unrpyc-master",
        project / "vendor" / "unrpyc-main",
    ]
    for p in candidates:
        if (p / "unrpyc.py").is_file() and (p / "decompiler").is_dir():
            return p
    return None


def unrpyc_available(explicit: str | Path | None = None) -> bool:
    return _unrpyc_root(explicit) is not None


def _import_unrpyc(root: Path):
    root_s = str(root)
    if root_s not in sys.path:
        sys.path.insert(0, root_s)
    try:
        return importlib.import_module("unrpyc"), importlib.import_module("decompiler")
    except Exception as e:
        raise BackendUnavailable(f"Unable to import unrpyc from {root}: {e}") from e


def load_with_unrpyc(path: str | Path, *, try_harder: bool = False, unrpyc_path=None):
    root = _unrpyc_root(unrpyc_path)
    if root is None:
        raise BackendUnavailable("unrpyc backend is not installed")
    unrpyc, decompiler = _import_unrpyc(root)
    context = unrpyc.Context()
    ast = unrpyc.get_ast(Path(path), try_harder, context)
    return ast, context, decompiler


def _ast_to_source(ast, context, decompiler) -> str:
    out = StringIO()
    options = decompiler.Options(
        log=context.log_contents,
        translator=None,
        init_offset=False,
        sl_custom_names=None,
    )
    decompiler.pprint(out, ast, options)
    return out.getvalue()


def extract_compiled(path: str | Path, *, backend="auto", try_harder=False, unrpyc_path=None, defer_source=False):
    """Return (candidates, metadata) from .rpyc/.rpymc without writing game files."""
    path = Path(path)
    backend = backend.lower()
    if backend not in {"auto", "native", "unrpyc"}:
        raise ValueError("backend must be auto, native, or unrpyc")

    if backend in {"auto", "unrpyc"} and unrpyc_available(unrpyc_path):
        ast, context, decompiler = load_with_unrpyc(
            path, try_harder=try_harder, unrpyc_path=unrpyc_path
        )
        # Native AST extraction catches direct Say/Menu/TL structures. The
        # in-memory decompile + source parser adds Screen Language, Character
        # declarations and other syntax that fake AST classes cannot execute.
        direct = extract_ast(ast, say_code_fn=getattr(decompiler, "say_get_code", None))
        version = getattr(importlib.import_module("unrpyc"), "__version__", "unknown")
        try:
            text = _ast_to_source(ast, context, decompiler)
            parsed = [] if defer_source else RenPySourceParser(infer_say=False, infer_menu=False).parse(text)
            merged = _dedup(direct + parsed)
            return merged, {
                "backend": "unrpyc", "_interactive_bundle": bundle_from_ast(ast),
                "unrpyc_version": version,
                "unrpyc_log": list(context.log_contents),
                "memory_source_chars": len(text),
                "_memory_source": text,
                "try_harder": bool(try_harder),
            }
        except Exception as e:
            # Some games contain AST classes/versions that the currently installed
            # unrpyc can load safely but cannot pretty-print. Do not fail the whole
            # file: the duck-typed AST adapter can still recover Say/Menu/TL strings
            # and nested blocks directly without executing game code.
            return _dedup(direct), {
                "backend": "unrpyc_ast_fallback", "_interactive_bundle": bundle_from_ast(ast),
                "unrpyc_version": version,
                "unrpyc_log": list(context.log_contents),
                "decompile_warning": f"{type(e).__name__}: {e}",
                "memory_source_chars": 0,
                "try_harder": bool(try_harder),
                "limitations": [
                    "unrpyc loaded the AST but could not pretty-print this file.",
                    "Direct AST extraction was used; complex Screen Language may be incomplete for this file.",
                ],
            }

    if backend == "unrpyc":
        raise BackendUnavailable("unrpyc backend requested but not installed")

    loaded = load_rpyc(path)
    candidates = extract_ast(loaded.ast)
    return candidates, {
        "backend": "native", "_interactive_bundle": bundle_from_ast(loaded.ast),
        "format": loaded.format,
        "compressed_size": loaded.compressed_size,
        "pickle_size": loaded.pickle_size,
        "limitations": [
            "Native mode is intentionally conservative.",
            "Complex Screen Language and custom statements may require the unrpyc backend.",
        ],
    }



def extract_compiled_bytes(data: bytes, *, suffix=".rpyc", backend="auto", try_harder=False, unrpyc_path=None, defer_source=False):
    """Like extract_compiled(), but for an RPYC payload already read from an RPA."""
    backend = backend.lower()
    if backend in {"auto", "unrpyc"} and unrpyc_available(unrpyc_path):
        import tempfile
        with tempfile.TemporaryDirectory(prefix="rpts_rpyc_") as td:
            p = Path(td) / ("archive_entry" + suffix)
            p.write_bytes(data)
            return extract_compiled(p, backend="unrpyc", try_harder=try_harder, unrpyc_path=unrpyc_path, defer_source=defer_source)
    if backend == "unrpyc":
        raise BackendUnavailable("unrpyc backend requested but not installed")
    loaded = load_rpyc_bytes(data)
    candidates = extract_ast(loaded.ast)
    return candidates, {
        "backend": "native", "_interactive_bundle": bundle_from_ast(loaded.ast),
        "format": loaded.format,
        "compressed_size": loaded.compressed_size,
        "pickle_size": loaded.pickle_size,
        "from_archive": True,
        "limitations": [
            "Native mode is intentionally conservative.",
            "Complex Screen Language and custom statements may require the unrpyc backend.",
        ],
    }


def _dedup(items):
    out = []
    seen = set()
    for c in items:
        key = (c.kind, c.text, c.line, c.column, c.speaker, c.dynamic, c.expression, c.language, c.translation_id)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
