from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .translation_ids import encode_say_string
from .ui_project import UI_KINDS, build_ui_map, fetch_ui_rows, load_overrides

TRANSLATED_STATUSES = {"translated", "translated_tm", "approved"}
DIALOGUE_KINDS = {"dialogue", "narration", "centered", "dialogue_extend"}
MANIFEST_NAME = "rpts_translation_manifest.json"
BOOTSTRAP_NAME = "rpts_language.rpy"
GENERATED_NAME = "rpts_generated.rpy"
RUNTIME_UI_KINDS = set(UI_KINDS)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _metadata(db: sqlite3.Connection) -> dict[str, Any]:
    out = {}
    try:
        for key, value in db.execute("SELECT key,value FROM metadata"):
            try:
                out[str(key)] = json.loads(value)
            except Exception:
                out[str(key)] = value
    except sqlite3.OperationalError:
        pass
    return out


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {str(r[1]) for r in db.execute(f"PRAGMA table_info({table})")}


def _translated_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _columns(db, "segments")
    want = [
        "segment_id", "source_file", "source_line", "kind", "channel", "origin", "source_text",
        "speaker", "label", "translation_id", "translation_id_source", "translation_id_alternate",
        "say_code", "status", "translation", "provider", "model", "context_json",
    ]
    present = [x for x in want if x in cols]
    cur = db.execute(
        f"SELECT {','.join(present)} FROM segments WHERE translation<>'' ORDER BY source_file,source_line,segment_id"
    )
    out = []
    for raw in cur.fetchall():
        row = dict(zip(present, raw))
        if row.get("channel") == "interactive_legacy":
            continue
        if str(row.get("status") or "") not in TRANSLATED_STATUSES:
            continue
        if str(row.get("provider") or "").lower() == "mock":
            continue
        for k in want:
            row.setdefault(k, "")
        out.append(row)
    return out


def _render_say(row: dict[str, Any]) -> str | None:
    source = str(row.get("source_text") or "")
    translated = str(row.get("translation") or "")
    say_code = str(row.get("say_code") or "")
    if say_code:
        src = encode_say_string(source)
        dst = encode_say_string(translated)
        if src in say_code:
            return say_code.replace(src, dst, 1)
    # Backward-compatible fallback for old Stage 6 databases. Only use forms
    # whose syntax is unambiguous without the original Say AST.
    kind = str(row.get("kind") or "")
    speaker = str(row.get("speaker") or "")
    if kind == "narration":
        return encode_say_string(translated)
    if kind == "centered":
        return "centered " + encode_say_string(translated)
    if kind == "dialogue_extend":
        return "extend " + encode_say_string(translated)
    if kind == "dialogue" and speaker and speaker.isidentifier():
        return f"{speaker} {encode_say_string(translated)}"
    return None




def _py_unicode_literal(value: str) -> str:
    # ASCII-only u"..." literal that is valid on both Python 2 (Ren'Py 6/7)
    # and Python 3 (Ren'Py 8). json.dumps gives us reliable escaping.
    return "u" + json.dumps(str(value), ensure_ascii=True)


def _runtime_ui_map(rows: list[dict[str, Any]], overrides: dict[str, str] | None = None) -> tuple[dict[str, str], list[dict[str, Any]]]:
    """Build exact-match replacements for all known UI text.

    Stage 7.6 restricted this fallback to dataflow/multilingual origins. That
    missed games whose settings labels are stored as static UI data and later
    displayed through variables. Stage 7.7 deliberately includes every UI
    category and statically recognized interactive messages. Ordinary story
    dialogue is excluded by fetch_ui_rows. Conflicting translations are never
    guessed.
    """
    mapping, conflicts, _ = build_ui_map(rows, overrides or {})
    return mapping, conflicts


def _runtime_ui_bootstrap(mapping: dict[str, str], language: str = "russian") -> str:
    if not mapping:
        return ""
    lines = [
        "",
        "# RPTS dynamic UI fallback.",
        "# Only exact whole-text matches are replaced; original game callback is preserved.",
        "init 999 python:",
        "    _rpts_ui_map = {",
    ]
    for old, new in sorted(mapping.items()):
        lines.append(f"        {_py_unicode_literal(old)}: {_py_unicode_literal(new)},")
    lines.extend([
        "    }",
        "",
        "    _rpts_previous_replace_text = config.replace_text",
        "    if getattr(_rpts_previous_replace_text, '_rpts_dynamic_ui', False):",
        "        _rpts_previous_replace_text = getattr(_rpts_previous_replace_text, '_rpts_previous', None)",
        "",
        "    def _rpts_replace_text(s):",
        f"        if _preferences.language != {language!r}:",
        "            return _rpts_previous_replace_text(s) if _rpts_previous_replace_text is not None else s",
        "        mapped = _rpts_ui_map.get(s, s)",
        "        if _rpts_previous_replace_text is not None:",
        "            mapped = _rpts_previous_replace_text(mapped)",
        "        return _rpts_ui_map.get(mapped, mapped)",
        "",
        "    _rpts_replace_text._rpts_dynamic_ui = True",
        "    _rpts_replace_text._rpts_previous = _rpts_previous_replace_text",
        "    config.replace_text = _rpts_replace_text",
    ])
    return "\n".join(lines) + "\n"

def _multilingual_maps(db, translated_rows, overrides):
    """Only wrap proven helpers; never execute or rewrite parsed game expressions."""
    translations = {}
    for row in translated_rows:
        translations.setdefault(row["source_text"], set()).add(row["translation"])
    for old, new in overrides.items(): translations[old] = {new}
    calls = {}
    for call_id, function, text, role in db.execute("SELECT call_id,function,text,role_guess FROM multilingual_variants"):
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", function): continue
        if any(part.startswith("__") for part in function.split(".")): continue
        call = calls.setdefault((function, call_id), [])
        call.append((text, role))
    candidates = {}
    for (function, _), variants in calls.items():
        source = [text for text, role in variants if role == "source_like"]
        if len(source) != 1 or len(translations.get(source[0], set())) != 1: continue
        target = next(iter(translations[source[0]]))
        for text, _ in variants:
            candidates.setdefault((function, text), set()).add(target)
    result = {}
    for (function, text), targets in candidates.items():
        if len(targets) == 1: result.setdefault(function, {})[text] = next(iter(targets))
    return result


def _multilingual_bootstrap(helper_maps, language):
    if not helper_maps: return ""
    lines = ["", "# Translate helper results before interpolation into a larger string.",
             "init 999 python:", "    _rpts_helper_maps = {"]
    for function, mapping in sorted(helper_maps.items()):
        lines.append(f"        {function!r}: {{")
        for old, new in sorted(mapping.items()):
            lines.append(f"            {_py_unicode_literal(old)}: {_py_unicode_literal(new)},")
        lines.append("        },")
    lines.extend(["    }", "    try:", "        _rpts_string_types = (basestring,)",
        "    except NameError:", "        _rpts_string_types = (str,)",
        "    def _rpts_wrap_helper(original, mapping):",
        "        original = getattr(original, '_rpts_helper_original', original)",
        "        def translated(*args, **kwargs):",
        "            result = original(*args, **kwargs)",
        f"            if _preferences.language == {language!r} and isinstance(result, _rpts_string_types):",
        "                return mapping.get(result, result)",
        "            return result",
        "        translated._rpts_helper_original = original",
        "        return translated",
        "    for _rpts_path, _rpts_mapping in _rpts_helper_maps.items():",
        "        _rpts_parts = _rpts_path.split('.')",
        "        _rpts_owner = renpy.store",
        "        for _rpts_part in _rpts_parts[:-1]:",
        "            _rpts_owner = getattr(_rpts_owner, _rpts_part, None)",
        "        _rpts_original = getattr(_rpts_owner, _rpts_parts[-1], None)",
        "        if callable(_rpts_original):",
        "            setattr(_rpts_owner, _rpts_parts[-1], _rpts_wrap_helper(_rpts_original, _rpts_mapping))"])
    return "\n".join(lines) + "\n"


def _safe_language_name(value: str) -> str:
    value = (value or "russian").strip().lower().replace("-", "_").replace(" ", "_")
    if not value or not (value[0].isalpha() or value[0] == "_") or not all(c.isalnum() or c == "_" for c in value):
        raise ValueError(f"Invalid Ren'Py language name: {value!r}")
    return value


def _string_pair_block(language: str, pairs: list[tuple[str, str]]) -> str:
    lines = [f"translate {language} strings:", ""]
    for old, new in pairs:
        lines.append(f"    old {encode_say_string(old)}")
        lines.append(f"    new {encode_say_string(new)}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def build_patch(db_path: str | Path, output_dir: str | Path, *, renpy_language: str = "russian", activate: bool = True, overrides_path: str | Path | None = None) -> dict[str, Any]:
    db_path = Path(db_path)
    from .alignment_recovery import recover_project_translations
    recover_project_translations(db_path)
    output_dir = Path(output_dir)
    language = _safe_language_name(renpy_language)
    overrides = load_overrides(overrides_path)
    db = sqlite3.connect(db_path)
    try:
        rows = _translated_rows(db)
        ui_rows = fetch_ui_rows(db)
        meta = _metadata(db)
        helper_maps = _multilingual_maps(db, rows, overrides)
    finally:
        db.close()

    # Manual UI overrides have the highest priority and also work for rows that
    # were never sent to a provider. They affect known UI kinds and messages
    # extracted from interactive panels, without modifying ordinary dialogue.
    effective_rows = []
    for row in rows:
        r = dict(row)
        if (str(r.get("kind") or "") in RUNTIME_UI_KINDS or r.get("origin") == "interactive") and str(r.get("source_text") or "") in overrides:
            r["translation"] = overrides[str(r.get("source_text") or "")]
            r["status"] = "approved"
            r["provider"] = "manual_ui_override"
        effective_rows.append(r)
    known_ids = {str(r.get("segment_id") or "") for r in effective_rows}
    for row in ui_rows:
        source = str(row.get("source_text") or "")
        if source not in overrides or str(row.get("segment_id") or "") in known_ids:
            continue
        r = dict(row)
        r.update({"translation": overrides[source], "status": "approved", "provider": "manual_ui_override", "model": "manual"})
        effective_rows.append(r)
    rows = effective_rows

    effective_ui_rows = []
    for row in ui_rows:
        r = dict(row)
        source = str(r.get("source_text") or "")
        if source in overrides:
            r.update({"translation": overrides[source], "status": "approved", "provider": "manual_ui_override", "model": "manual"})
        effective_ui_rows.append(r)
    runtime_ui_map, runtime_ui_conflicts = _runtime_ui_map(effective_ui_rows, overrides)

    if output_dir.exists():
        shutil.rmtree(output_dir)
    tl_dir = output_dir / "tl" / language
    tl_dir.mkdir(parents=True, exist_ok=True)

    from .interactive_patch import phone_mapping, phone_bootstrap
    phone_map, phone_coverage = phone_mapping(rows, meta.get("interactive_inventory") or {})
    handled_phone_ids = {item["segment_id"] for item in phone_coverage if item["status"] == "context_adapter"}
    (output_dir / "interactive_coverage.json").write_text(json.dumps(phone_coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    # String translations are the safest default for an unsanctioned compiled
    # game: they preserve the original Say statement, voice, callbacks and other
    # runtime behaviour. Only source strings that received conflicting context-
    # dependent translations need ID-specific dialogue blocks.
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        source = str(row.get("source_text") or "")
        translated = str(row.get("translation") or "")
        if not source or not translated or source == translated:
            continue
        by_source.setdefault(source, []).append(row)

    string_pairs: list[tuple[str, str]] = []
    id_rows: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    for source, group in by_source.items():
        translations = {str(r.get("translation") or "") for r in group}
        if len(translations) == 1:
            string_pairs.append((source, next(iter(translations))))
            continue
        # A UI message needs a string translation even when an ordinary Say
        # uses a different wording. Say keeps its own translation-ID block.
        interactive_values = {r["translation"] for r in group if r.get("origin") == "interactive"}
        if len(interactive_values) == 1:
            string_pairs.append((source, next(iter(interactive_values))))
        for row in group:
            if row.get("segment_id") in handled_phone_ids:
                continue
            if row.get("origin") == "interactive" and len(interactive_values) == 1:
                continue
            if row.get("kind") in DIALOGUE_KINDS and row.get("translation_id") and _render_say(row):
                id_rows.append(row)
            else:
                conflicts.append(row)

    string_pairs.sort(key=lambda x: x[0])
    generated = [
        "# Generated by RPTS Stage 7.7. Do not edit while RPTS manages this patch.",
        "# String translations are preferred to preserve original Ren'Py runtime behaviour.",
        "",
    ]
    if string_pairs:
        generated.append(_string_pair_block(language, string_pairs).rstrip())
        generated.append("")
    for row in sorted(id_rows, key=lambda r: (str(r.get("source_file")), int(r.get("source_line") or 0), str(r.get("segment_id")))):
        tid = str(row["translation_id"])
        rendered = _render_say(row)
        if not rendered:
            conflicts.append(row)
            continue
        generated.extend([
            f"# {row.get('source_file','')}:{row.get('source_line',0)}",
            f"translate {language} {tid}:",
            f"    {rendered}",
            "",
        ])
    generated_path = tl_dir / GENERATED_NAME
    generated_path.write_text("\n".join(generated).rstrip() + "\n", encoding="utf-8")

    bootstrap_path = output_dir / BOOTSTRAP_NAME
    if activate or runtime_ui_map or helper_maps or phone_map:
        bootstrap = "# Generated by RPTS Stage 7.7. Managed file.\n"
        if activate:
            bootstrap += f"define config.language = {language!r}\n"
        bootstrap += _runtime_ui_bootstrap(runtime_ui_map, language)
        bootstrap += _multilingual_bootstrap(helper_maps, language)
        bootstrap += phone_bootstrap(phone_map, language)
        bootstrap_path.write_text(bootstrap, encoding="utf-8")

    if runtime_ui_map:
        rpath = output_dir / "runtime_ui_map.csv"
        with rpath.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["source_text", "translation"])
            for old, new in sorted(runtime_ui_map.items()):
                w.writerow([old, new])

    if runtime_ui_conflicts:
        rcpath = output_dir / "runtime_ui_conflicts.csv"
        fields = ["segment_id", "source_file", "source_line", "kind", "origin", "source_text", "translation"]
        with rcpath.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(runtime_ui_conflicts)

    if conflicts:
        cpath = output_dir / "patch_conflicts.csv"
        fields = ["segment_id", "source_file", "source_line", "kind", "source_text", "translation", "translation_id", "say_code"]
        with cpath.open("w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader(); w.writerows(conflicts)

    files = []
    for path in sorted(p for p in output_dir.rglob("*") if p.is_file() and p.suffix.lower() == ".rpy"):
        rel = path.relative_to(output_dir).as_posix()
        files.append({"path": rel, "sha256": _sha256_file(path), "size": path.stat().st_size})
    manifest = {
        "format": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "database": str(db_path.resolve()),
        "game_dir": str(meta.get("game_dir") or ""),
        "renpy_language": language,
        "activate": bool(activate),
        "translated_rows": len(rows),
        "string_pairs": len(string_pairs),
        "id_specific_blocks": len(id_rows),
        "runtime_ui_pairs": len(runtime_ui_map),
        "multilingual_helpers": {key: len(value) for key, value in helper_maps.items()},
        "runtime_ui_conflicts": len(runtime_ui_conflicts),
        "interactive_context_translations": len(phone_map),
        "interactive_untranslated": sum(r["status"] == "untranslated" for r in phone_coverage),
        "interactive_ambiguous": sum(r["status"] == "ambiguous_runtime_context" for r in phone_coverage),
        "manual_ui_overrides": len(overrides),
        "conflicts": len(conflicts),
        "files": files,
    }
    (output_dir / "patch_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def _load_patch_manifest(patch_dir: Path) -> dict[str, Any]:
    path = patch_dir / "patch_manifest.json"
    if not path.is_file():
        raise FileNotFoundError(f"Patch manifest not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _load_installed_manifest(game_dir: Path) -> dict[str, Any] | None:
    path = game_dir / MANIFEST_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def uninstall_patch(game_dir: str | Path) -> dict[str, Any]:
    game_dir = Path(game_dir).resolve()
    manifest_path = game_dir / MANIFEST_NAME
    manifest = _load_installed_manifest(game_dir)
    if not manifest:
        return {"removed": 0, "restored": 0, "preserved_modified": 0, "message": "RPTS patch is not installed"}
    removed = restored = preserved = 0
    for entry in reversed(manifest.get("files", [])):
        rel = Path(entry["path"])
        dst = game_dir / rel
        backup = entry.get("backup") or ""
        installed_hash = entry.get("sha256") or ""
        if dst.exists():
            # Do not delete a file the user changed after installation.
            try:
                current = _sha256_file(dst)
            except Exception:
                current = ""
            if installed_hash and current != installed_hash:
                preserved += 1
                continue
            dst.unlink()
            removed += 1
        # Ren'Py may have compiled our .rpy automatically.
        if dst.suffix.lower() == ".rpy":
            compiled = dst.with_suffix(".rpyc")
            if compiled.exists():
                try:
                    compiled.unlink(); removed += 1
                except OSError:
                    pass
        if backup:
            bpath = Path(backup)
            if bpath.is_file():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(bpath, dst)
                restored += 1
    try:
        manifest_path.unlink()
    except FileNotFoundError:
        pass
    # Temporary Stage 7.7+ runtime capture helper must never survive a full RPTS uninstall.
    for extra in (game_dir / "rpts_ui_capture.rpy", game_dir / "rpts_ui_capture.rpyc"):
        try:
            if extra.exists(): extra.unlink(); removed += 1
        except OSError:
            pass
    # Remove empty RPTS translation directory only, never the language folder if
    # the game has other files there.
    for d in sorted({(game_dir / Path(x["path"])).parent for x in manifest.get("files", [])}, key=lambda x: len(x.parts), reverse=True):
        try:
            d.rmdir()
        except OSError:
            pass
    return {"removed": removed, "restored": restored, "preserved_modified": preserved}


def install_patch(patch_dir: str | Path, game_dir: str | Path | None = None, *, backup_dir: str | Path | None = None) -> dict[str, Any]:
    patch_dir = Path(patch_dir).resolve()
    patch_manifest = _load_patch_manifest(patch_dir)
    if game_dir is None:
        raw = str(patch_manifest.get("game_dir") or "")
        if not raw:
            raise ValueError("Game directory is not known. Pass --game explicitly.")
        game_dir = raw
    game_dir = Path(game_dir).resolve()
    if not game_dir.is_dir():
        raise FileNotFoundError(f"Game directory not found: {game_dir}")

    previous = _load_installed_manifest(game_dir)
    previous_by_path = {str(x.get("path") or ""): x for x in (previous or {}).get("files", [])}
    language = _safe_language_name(str(patch_manifest.get("renpy_language") or "russian"))
    language_dir = game_dir / "tl" / language
    foreign_tl = []
    if language_dir.is_dir():
        owned = set(previous_by_path)
        for p in language_dir.rglob("*"):
            if not p.is_file() or p.suffix.lower() not in {".rpy", ".rpyc", ".rpym", ".rpymc"}:
                continue
            rel = p.relative_to(game_dir).as_posix()
            if rel in owned or p.name.lower() in {GENERATED_NAME.lower(), Path(GENERATED_NAME).with_suffix(".rpyc").name.lower()}:
                continue
            foreign_tl.append(p)
    if foreign_tl:
        shown = ", ".join(str(x.relative_to(game_dir)) for x in foreign_tl[:5])
        raise RuntimeError(f"Target language tl/{language} already contains non-RPTS translation files: {shown}")

    install_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_root = Path(backup_dir).resolve() if backup_dir else (patch_dir.parent / "backups" / install_id)
    rollback_root = backup_root / ".rollback"
    rollback_root.mkdir(parents=True, exist_ok=True)
    manifest_path = game_dir / MANIFEST_NAME
    old_manifest_bytes = manifest_path.read_bytes() if manifest_path.exists() else None
    new_paths = {Path(x["path"]).as_posix() for x in patch_manifest.get("files", [])}
    touched = set(new_paths) | set(previous_by_path)
    snapshots: dict[str, Path | None] = {}
    for rel_s in touched:
        dst = game_dir / Path(rel_s)
        if dst.is_file():
            snap = rollback_root / Path(rel_s)
            snap.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(dst, snap)
            snapshots[rel_s] = snap
        else:
            snapshots[rel_s] = None

    installed_files = []
    try:
        for entry in patch_manifest.get("files", []):
            rel = Path(entry["path"]); rel_s = rel.as_posix()
            src = patch_dir / rel; dst = game_dir / rel
            if not src.is_file():
                raise FileNotFoundError(f"Patch file missing: {src}")
            backup = ""
            prev = previous_by_path.get(rel_s)
            if prev and prev.get("backup"):
                backup = str(prev.get("backup"))
            elif dst.exists():
                bpath = backup_root / "original" / rel
                bpath.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(dst, bpath); backup = str(bpath)
            dst.parent.mkdir(parents=True, exist_ok=True)
            tmp = dst.with_name(dst.name + f".rpts-new-{os.getpid()}")
            shutil.copy2(src, tmp)
            expected = _sha256_file(src)
            if _sha256_file(tmp) != expected:
                tmp.unlink(missing_ok=True); raise RuntimeError(f"Patch staging verification failed: {rel_s}")
            os.replace(tmp, dst)
            if _sha256_file(dst) != expected:
                raise RuntimeError(f"Patch activation verification failed: {rel_s}")
            installed_files.append({"path": rel_s, "sha256": expected, "backup": backup})

        installed_manifest = {
            "format": 1, "installed_at": datetime.now(timezone.utc).isoformat(),
            "patch_dir": str(patch_dir), "renpy_language": patch_manifest.get("renpy_language"),
            "files": installed_files,
        }
        tmp_manifest = manifest_path.with_name(manifest_path.name + f".new-{os.getpid()}")
        tmp_manifest.write_text(json.dumps(installed_manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp_manifest, manifest_path)

        # Remove obsolete files from the previous RPTS patch only after the new
        # manifest is safely active. Never delete user-modified files.
        for rel_s, prev in previous_by_path.items():
            if rel_s in new_paths:
                continue
            dst = game_dir / Path(rel_s)
            if dst.exists() and prev.get("sha256") and _sha256_file(dst) == prev.get("sha256"):
                dst.unlink()
        shutil.rmtree(rollback_root, ignore_errors=True)
        return {"game_dir": str(game_dir), "files": len(installed_files), "previous_uninstall": None, "transactional": True}
    except Exception:
        for rel_s, snap in snapshots.items():
            dst = game_dir / Path(rel_s)
            if snap is None:
                try: dst.unlink(missing_ok=True)
                except Exception: pass
            elif snap.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(snap, dst)
        if old_manifest_bytes is None:
            manifest_path.unlink(missing_ok=True)
        else:
            manifest_path.write_bytes(old_manifest_bytes)
        raise


def patch_status(game_dir: str | Path) -> dict[str, Any]:
    game_dir = Path(game_dir).resolve()
    m = _load_installed_manifest(game_dir)
    if not m:
        return {"installed": False, "game_dir": str(game_dir)}
    good = modified = missing = 0
    for entry in m.get("files", []):
        path = game_dir / entry["path"]
        if not path.exists():
            missing += 1
        elif entry.get("sha256") and _sha256_file(path) == entry["sha256"]:
            good += 1
        else:
            modified += 1
    return {"installed": True, "game_dir": str(game_dir), "language": m.get("renpy_language"), "ok_files": good, "modified_files": modified, "missing_files": missing}


def find_game_executable(game_dir: str | Path) -> Path | None:
    game_dir = Path(game_dir).resolve()
    root = game_dir.parent if game_dir.name.lower() == "game" else game_dir
    candidates = []
    for p in root.glob("*.exe"):
        low = p.name.lower()
        if any(x in low for x in ("unins", "crash", "python", "renpy", "steam", "vc_redist")):
            continue
        candidates.append(p)
    if not candidates:
        return None
    root_name = root.name.lower().replace("-win", "").replace("_win", "")
    candidates.sort(key=lambda p: (0 if root_name and root_name.split("-")[0] in p.stem.lower() else 1, len(p.name), p.name.lower()))
    return candidates[0]


def launch_game(game_dir: str | Path) -> str:
    exe = find_game_executable(game_dir)
    if not exe:
        raise FileNotFoundError("Could not find a game executable next to the game/ directory")
    subprocess.Popen([str(exe)], cwd=str(exe.parent))
    return str(exe)


def _game_from_db(db_path: Path) -> str:
    db = sqlite3.connect(db_path)
    try:
        m = _metadata(db)
    finally:
        db.close()
    return str(m.get("game_dir") or "")


def main(argv=None):
    ap = argparse.ArgumentParser(description="RPTS Stage 7 Ren'Py patch builder/installer")
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("build")
    b.add_argument("database", type=Path)
    b.add_argument("--output", type=Path, default=Path("work/patch"))
    b.add_argument("--language", default="russian")
    b.add_argument("--no-activate", action="store_true")
    b.add_argument("--overrides", type=Path)

    i = sub.add_parser("install")
    i.add_argument("patch", type=Path, nargs="?", default=Path("work/patch"))
    i.add_argument("--game", type=Path)
    i.add_argument("--backup-dir", type=Path)

    u = sub.add_parser("uninstall")
    u.add_argument("--game", type=Path)
    u.add_argument("--database", type=Path)

    s = sub.add_parser("status")
    s.add_argument("--game", type=Path)
    s.add_argument("--database", type=Path)

    l = sub.add_parser("launch")
    l.add_argument("--game", type=Path)
    l.add_argument("--database", type=Path)

    bi = sub.add_parser("build-install")
    bi.add_argument("database", type=Path)
    bi.add_argument("--output", type=Path, default=Path("work/patch"))
    bi.add_argument("--language", default="russian")
    bi.add_argument("--game", type=Path)
    bi.add_argument("--launch", action="store_true")
    bi.add_argument("--overrides", type=Path)

    args = ap.parse_args(argv)
    try:
        if args.cmd == "build":
            result = build_patch(args.database, args.output, renpy_language=args.language, activate=not args.no_activate, overrides_path=args.overrides)
        elif args.cmd == "install":
            result = install_patch(args.patch, args.game, backup_dir=args.backup_dir)
        elif args.cmd in {"uninstall", "status", "launch"}:
            game = args.game
            if game is None and args.database is not None:
                game = _game_from_db(args.database)
            if not game:
                raise ValueError("Game directory is required (--game or --database with game_dir metadata)")
            if args.cmd == "uninstall":
                result = uninstall_patch(game)
            elif args.cmd == "status":
                result = patch_status(game)
            else:
                result = {"launched": launch_game(game)}
        else:
            result = build_patch(args.database, args.output, renpy_language=args.language, activate=True, overrides_path=args.overrides)
            game = args.game or result.get("game_dir") or _game_from_db(args.database)
            installed = install_patch(args.output, game)
            result = {"build": result, "install": installed}
            if args.launch:
                result["launched"] = launch_game(game)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
