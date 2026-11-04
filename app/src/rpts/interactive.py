"""Static ordered-message extraction. Never evaluate or execute game Python."""
from __future__ import annotations
import ast
import hashlib
import json
import re
import textwrap
from pathlib import Path

VERSION = "11.16"


def code_text(code):
    value = getattr(code, "source", None)
    state = getattr(code, "_setstate", ())
    return value if isinstance(value, str) else state[1] if len(state) > 1 and isinstance(state[1], str) else ""


def bundle_from_ast(nodes):
    result = {"code": [], "calls": [], "declarations": []}
    seen = set()
    def visit(node, label=""):
        if isinstance(node, (str, bytes, int, float, bool, type(None))) or id(node) in seen:
            return
        seen.add(id(node))
        data = getattr(node, "__dict__", {})
        kind = type(node).__name__
        if kind == "Label":
            label = str(data.get("_name") or data.get("name") or "")
        if kind == "Python":
            result["code"].append(code_text(data.get("code")))
        if kind == "Define":
            result["declarations"].append(str(data.get("varname") or "") + " = " + code_text(data.get("code")))
        if kind == "Call" and not data.get("expression"):
            args = getattr(data.get("arguments"), "arguments", [])
            kwargs = {key: str(value) for key, value in args if key}
            if "messages" in kwargs:
                result["calls"].append(dict(entrypoint=str(data.get("label") or ""), label=label,
                                             line=int(data.get("linenumber") or 0), kwargs=kwargs))
        children = node.values() if isinstance(node, dict) else node if isinstance(node, (list, tuple)) else [v for k, v in data.items() if k not in {"next", "bytecode"}]
        for child in children:
            visit(child, label)
    visit(nodes)
    return result


def bundle_from_source(source):
    from .logical import iter_logical_statements
    result = {"code": [], "calls": [], "declarations": []}
    lines = source.splitlines()
    for i, line in enumerate(lines):
        if re.match(r"\s*(?:init(?:\s+-?\d+)?\s+)?python(?:\s+early)?\s*:\s*$", line):
            indent = len(line) - len(line.lstrip())
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or len(lines[end]) - len(lines[end].lstrip()) > indent):
                end += 1
            result["code"].append(textwrap.dedent("\n".join(lines[i + 1:end])))
    label = ""
    for stmt in iter_logical_statements(source):
        text = stmt.text.strip()
        match = re.match(r"label\s+(\w+)", text)
        if match:
            label = match[1]
        if re.match(r"(?:define|default)\s+\w+\s*=", text):
            result["declarations"].append(text)
        match = re.match(r"call\s+(\w+)\s*(\(.*\))\s*(?:from\s+\w+)?$", text, re.S)
        if match:
            try:
                call = ast.parse(match[1] + match[2], mode="eval").body
                kwargs = {kw.arg: ast.unparse(kw.value) for kw in call.keywords if kw.arg}
                if "messages" in kwargs:
                    result["calls"].append(dict(entrypoint=match[1], label=label, line=stmt.line, kwargs=kwargs))
            except SyntaxError:
                pass
    return result


def message_signature(kind, game_time, messages):
    rows = [[str(m.get("runtime_speaker", m.get("speaker", ""))), str(m.get("text", ""))] for m in messages]
    return hashlib.sha256(json.dumps([kind, game_time, rows], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


def extract_messages(bundles):
    from .characters import character_declaration
    functions, contacts, names = {}, {}, {}
    for bundle in bundles.values():
        for declaration in bundle["declarations"]:
            identity = character_declaration(declaration)
            if identity.get("display_name"):
                names.setdefault(identity["display_name"], set()).add(identity["speaker"])
        for code in bundle["code"]:
            try:
                tree = ast.parse(textwrap.dedent(code))
            except (SyntaxError, ValueError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef) and len(node.body) == 1 and isinstance(node.body[0], ast.Return):
                    functions[node.name] = node
                if isinstance(node, ast.Dict):
                    for key, value in zip(node.keys, node.values):
                        if isinstance(key, ast.Constant) and isinstance(key.value, str) and isinstance(value, ast.Dict):
                            for k, v in zip(value.keys, value.values):
                                if isinstance(k, ast.Constant) and k.value == "name" and isinstance(v, ast.Constant) and isinstance(v.value, str):
                                    contacts[key.value] = v.value
    aliases = {key: next(iter(names[name])) for key, name in contacts.items() if len(names.get(name, ())) == 1}
    def value(node, env=None, depth=0):
        env = env or {}
        if depth > 6:
            raise ValueError("helper nesting limit")
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name) and node.id in env:
            return env[node.id]
        if isinstance(node, ast.Dict):
            fields = {k.value: v for k, v in zip(node.keys, node.values) if isinstance(k, ast.Constant)}
            if "text" in fields and "speaker" in fields:
                return {k: value(fields[k], env, depth + 1) for k in ("text", "speaker")}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            fn = functions[node.func.id]
            args = fn.args.args
            if len(node.args) > len(args) or any(isinstance(a, ast.Starred) for a in node.args):
                raise ValueError("unsupported positional arguments")
            bound = {arg.arg: value(default, env, depth + 1) for arg, default in zip(args[-len(fn.args.defaults):], fn.args.defaults)} if fn.args.defaults else {}
            bound.update({arg.arg: value(actual, env, depth + 1) for arg, actual in zip(args, node.args)})
            for kw in node.keywords:
                if kw.arg is None or kw.arg not in {a.arg for a in args} or kw.arg in {a.arg for a in args[:len(node.args)]}:
                    raise ValueError("unsupported keyword arguments")
                bound[kw.arg] = value(kw.value, env, depth + 1)
            return value(fn.body[0].value, bound, depth + 1)
        raise ValueError("non-literal message or unsupported helper")
    result = dict(version=VERSION, aliases=aliases, scenes=[], messages=[], unresolved=[])
    for file, bundle in bundles.items():
        for call in bundle["calls"]:
            try:
                kwargs = call["kwargs"]
                nodes = ast.parse(kwargs["messages"], mode="eval").body
                if not isinstance(nodes, (ast.List, ast.Tuple)):
                    raise ValueError("dynamic messages list")
                messages = [value(node) for node in nodes.elts]
                if not all(isinstance(m, dict) and isinstance(m.get("text"), str) and isinstance(m.get("speaker"), str) for m in messages):
                    raise ValueError("message text/speaker is not a literal string")
                game_time = value(ast.parse(kwargs.get("game_time", "''"), mode="eval").body)
                caller = value(ast.parse(kwargs.get("caller", "''"), mode="eval").body)
                entry = call["entrypoint"]
                kind = {"phone_call": "call", "phone_chat": "chat", "phone_chat_snapshot": "snapshot"}.get(entry, entry)
                signature = message_signature(kind, game_time, messages)
                scene_id = hashlib.sha256(json.dumps([file, call["line"], call["label"], signature], ensure_ascii=False).encode("utf-8")).hexdigest()[:20]
                debug = bool(re.search(r"(?:^|_)(?:test|debug|demo)(?:_|$)", call["label"]))
                scene = dict(signature=signature, scene_id=scene_id, kind=kind, game_time=game_time, file=file, line=call["line"],
                             label=call["label"], debug=debug, count=len(messages),
                             adapter="emberbound_phone" if entry in {"phone_call", "phone_chat", "phone_chat_snapshot"} and "phone_chat_msg" in functions else "string_lookup")
                result["scenes"].append(scene)
                participants = sorted({aliases.get(caller if m["speaker"] == "caller" else "yunxi" if m["speaker"] == "player" else m["speaker"], "") for m in messages} - {""})
                for index, message in enumerate(messages):
                    raw = message["speaker"]
                    speaker = aliases.get(caller if raw == "caller" else "yunxi" if raw == "player" else raw, "")
                    result["messages"].append(dict(scene, index=index, text=message["text"], runtime_speaker=raw,
                        speaker=speaker, participants=participants, viewpoint=aliases.get("yunxi", ""),
                        narration=raw == "note", segment_id="interactive:" + scene_id + ":" + str(index).zfill(4)))
            except (ValueError, SyntaxError, TypeError, KeyError, RecursionError) as exc:
                result["unresolved"].append(dict(file=file, line=call["line"], label=call["label"], error=str(exc)))
    return result


def message_segments(inventory):
    from .project_db import _normalize_segment
    rows = {}
    for message in inventory["messages"]:
        if message["debug"]:
            continue
        row = _normalize_segment(dict(file=message["file"], line=message["line"], text=message["text"],
            kind="narration" if message["narration"] else "dialogue", speaker=message["speaker"],
            label="interactive:" + message["scene_id"]), origin="interactive", channel="semantic")
        row["segment_id"] = message["segment_id"]
        row["context_json"] = json.dumps({"interactive": message}, ensure_ascii=False)
        rows[row["segment_id"]] = row
    return list(rows.values())


def scan_game(game_dir):
    from .game_audit import discover_files, discover_archive_scripts
    from .rpyc_loader import load_rpyc_bytes
    game_dir = Path(game_dir)
    if not game_dir.is_dir():
        return None
    paths = discover_files(game_dir, prefer_compiled=False, exclude_tl=True)
    archives, errors, _ = discover_archive_scripts(game_dir, paths, prefer_compiled=False, exclude_tl=True)
    bundles, warnings = {}, [{"file": str(p), "error": str(e)} for p, e in errors]
    for path, reader in [(str(p.relative_to(game_dir)), p.read_bytes) for p in paths] + [(str(a.path.relative_to(game_dir)) + "::" + e.name, lambda a=a, e=e: a.read(e.name)) for a, e in archives]:
        try:
            data = reader()
            bundles[path] = bundle_from_ast(load_rpyc_bytes(data).ast) if Path(path).suffix in {".rpyc", ".rpymc"} else bundle_from_source(data.decode("utf-8-sig"))
        except Exception as exc:
            warnings.append(dict(file=path, error=type(exc).__name__ + ": " + str(exc)))
    inventory = extract_messages(bundles)
    inventory["scan_warnings"] = warnings
    return inventory
