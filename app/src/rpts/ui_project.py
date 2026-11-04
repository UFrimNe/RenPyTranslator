from __future__ import annotations

import csv
import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

UI_KINDS = {"ui", "notification", "marked_string", "character_name", "ui_value", "localized_string"}
DIALOGUE_KINDS = {"dialogue", "narration", "centered", "dialogue_extend"}
TRANSLATED_STATUSES = {"translated", "translated_tm", "approved"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def load_overrides(path: str | Path | None) -> dict[str, str]:
    if path is None:
        return {}
    path = Path(path)
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(raw, dict) and isinstance(raw.get("overrides"), dict):
        raw = raw["overrides"]
    if not isinstance(raw, dict):
        raise ValueError("UI overrides must be a JSON object or {\"overrides\": {...}}")
    out: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str) or not isinstance(v, str):
            continue
        if k and v:
            out[k] = v
    return out


def ensure_overrides(path: str | Path) -> Path:
    path = Path(path)
    if not path.exists():
        path.write_text(json.dumps({"overrides": {}}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def _segment_columns(db: sqlite3.Connection) -> set[str]:
    return {str(r[1]) for r in db.execute("PRAGMA table_info(segments)")}


def fetch_ui_rows(db: sqlite3.Connection) -> list[dict[str, Any]]:
    cols = _segment_columns(db)
    wanted = [
        "segment_id", "source_file", "source_line", "kind", "channel", "origin", "source_text",
        "dynamic", "expression", "status", "translation", "provider", "model", "context_json",
    ]
    selected = [c for c in wanted if c in cols]
    cur = db.execute(
        f"SELECT {','.join(selected)} FROM segments WHERE source_text<>'' ORDER BY source_file,source_line,segment_id"
    )
    rows = [dict(zip(selected, raw)) for raw in cur.fetchall()]
    return [r for r in rows if r.get("channel") != "interactive_legacy" and (str(r.get("kind") or "") in UI_KINDS or r.get("origin") == "interactive")]


def build_ui_map(rows: list[dict[str, Any]], overrides: dict[str, str] | None = None):
    overrides = overrides or {}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        src = str(row.get("source_text") or "")
        if not src:
            continue
        grouped.setdefault(src, []).append(row)

    mapping: dict[str, str] = {}
    conflicts: list[dict[str, Any]] = []
    untranslated: list[dict[str, Any]] = []
    for source, group in grouped.items():
        if source in overrides:
            mapping[source] = overrides[source]
            continue
        translations = {
            str(r.get("translation") or "")
            for r in group
            if str(r.get("status") or "") in TRANSLATED_STATUSES and str(r.get("translation") or "")
        }
        translations.discard(source)
        if len(translations) == 1:
            mapping[source] = next(iter(translations))
        elif len(translations) > 1:
            conflicts.extend(group)
        else:
            untranslated.extend(group)
    return mapping, conflicts, untranslated


def write_ui_coverage(db_path: str | Path, output_dir: str | Path, overrides_path: str | Path | None = None) -> dict[str, Any]:
    db_path = Path(db_path)
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    overrides = load_overrides(overrides_path)
    db = sqlite3.connect(db_path)
    try:
        rows = fetch_ui_rows(db)
        raw = db.execute("SELECT value FROM metadata WHERE key='interactive_inventory'").fetchone()
        inventory = json.loads(raw[0]) if raw else {}
    finally:
        db.close()
    mapping, conflicts, untranslated = build_ui_map(rows, overrides)
    from .interactive_patch import phone_mapping
    effective = [dict(r, translation=overrides[r["source_text"]], status="approved", provider="manual_ui_override") if r["source_text"] in overrides else r for r in rows]
    phone_map, phone_coverage = phone_mapping(effective, inventory)
    handled = {r["segment_id"] for r in phone_coverage if r["status"] == "context_adapter"}
    conflicts = [r for r in conflicts if r.get("segment_id") not in handled]
    untranslated = [r for r in untranslated if r.get("segment_id") not in handled]
    (out / "interactive_coverage.json").write_text(json.dumps(phone_coverage, ensure_ascii=False, indent=2), encoding="utf-8")

    fields = ["segment_id","source_file","source_line","kind","channel","origin","source_text","dynamic","expression","status","translation","provider","model"]
    with (out / "ui_all.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(rows)
    translated_rows = [r for r in rows if str(r.get("source_text") or "") in mapping or r.get("segment_id") in handled]
    with (out / "ui_translated.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(translated_rows)
    with (out / "ui_untranslated.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(untranslated)
    with (out / "ui_conflicts.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore"); w.writeheader(); w.writerows(conflicts)
    with (out / "ui_runtime_map.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f); w.writerow(["source_text", "translation", "source"])
        for source, translation in sorted(mapping.items()):
            w.writerow([source, translation, "override" if source in overrides else "database"])

    unique = {str(r.get("source_text") or "") for r in rows}
    translated_unique = set(mapping) | {source for source in unique if all(r.get("segment_id") in handled for r in rows if r.get("source_text") == source)}
    summary = {
        "ui_rows": len(rows),
        "ui_unique": len(unique),
        "ui_translated_unique": len(translated_unique),
        "ui_untranslated_unique": len(unique - translated_unique),
        "runtime_ui_pairs": len(mapping),
        "interactive_context_translations": len(phone_map),
        "conflict_rows": len(conflicts),
        "manual_overrides": len(overrides),
        "output_dir": str(out.resolve()),
    }
    (out / "ui_coverage.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return summary


def _unescape_tsv(value: str) -> str:
    out = []
    i = 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "t": out.append("\t"); i += 2; continue
            if nxt == "n": out.append("\n"); i += 2; continue
            if nxt == "r": out.append("\r"); i += 2; continue
            if nxt == "\\": out.append("\\"); i += 2; continue
        out.append(value[i]); i += 1
    return "".join(out)


def import_runtime_ui(db_path: str | Path, tsv_path: str | Path) -> dict[str, int]:
    db_path = Path(db_path)
    tsv_path = Path(tsv_path)
    if not tsv_path.is_file():
        raise FileNotFoundError(tsv_path)
    db = sqlite3.connect(db_path)
    inserted = skipped_known = skipped_invalid = 0
    try:
        existing = {r[0] for r in db.execute("SELECT source_text FROM segments WHERE source_text<>''")}
        now = datetime.now(timezone.utc).isoformat()
        for raw in tsv_path.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = raw.split("\t", 2)
            if len(parts) != 3:
                skipped_invalid += 1; continue
            filename, line_s, text_s = parts
            text = _unescape_tsv(text_s).strip()
            if not text or len(text) > 240 or "\n" in text:
                skipped_invalid += 1; continue
            if text in existing:
                skipped_known += 1; continue
            try: line = int(line_s)
            except Exception: line = 0
            sid = "ui:" + _sha(text)[:24]
            source_hash = _sha(text)
            context = json.dumps({"runtime_capture": True, "captured_file": _unescape_tsv(filename)}, ensure_ascii=False)
            cur = db.execute(
                """INSERT OR IGNORE INTO segments(
                segment_id,source_file,source_line,kind,channel,origin,source_text,speaker,label,
                translation_id,translation_id_source,translation_id_alternate,say_code,language,dynamic,expression,queue_reason,
                status,translation,provider,model,source_hash,context_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (sid, _unescape_tsv(filename), line, "ui_value", "semantic", "runtime_ui", text, "", "", "", "", "", "", "", 1,
                 "runtime_capture", "runtime_ui_capture", "pending", "", "", "", source_hash, context, now),
            )
            if int(cur.rowcount or 0) > 0:
                inserted += 1
                existing.add(text)
        db.commit()
    finally:
        db.close()
    return {"inserted": inserted, "skipped_known": skipped_known, "skipped_invalid": skipped_invalid}


def runtime_capture_source(dialogue_texts: set[str], known_ui: set[str]) -> str:
    # Generated script is Python 2/3 compatible for Ren'Py 6/7/8.
    def pyset(values: set[str]) -> str:
        return "set([" + ",".join("u" + json.dumps(x, ensure_ascii=True) for x in sorted(values)) + "])"
    return f'''# Generated by RPTS Stage 7.7. Temporary UI capture helper.\n# Remove after importing rpts_runtime_ui.tsv.\n\ninit 1000 python:\n    import os\n    import io\n    try:\n        _rpts_text_type = unicode\n    except NameError:\n        _rpts_text_type = str\n\n    _rpts_ui_capture_dialogue = {pyset(dialogue_texts)}\n    _rpts_ui_capture_known = {pyset(known_ui)}\n    _rpts_ui_capture_seen = set()\n    _rpts_ui_capture_previous = config.replace_text\n\n    def _rpts_ui_capture_location():\n        try:\n            fn = getattr(renpy, "get_filename_line", None)\n            if fn is not None:\n                value = fn()\n                if value and len(value) >= 2:\n                    return _rpts_text_type(value[0]), int(value[1])\n        except Exception:\n            pass\n        return "", 0\n\n    def _rpts_ui_capture_escape(value):\n        return value.replace("\\\\", "\\\\\\\\").replace("\\t", "\\\\t").replace("\\r", "\\\\r").replace("\\n", "\\\\n")\n\n    def _rpts_ui_capture_replace(value):\n        original = value\n        try:\n            if original is not None:\n                text = _rpts_text_type(original).strip()\n                if text and len(text) <= 240 and "\\n" not in text and text not in _rpts_ui_capture_dialogue and text not in _rpts_ui_capture_known and text not in _rpts_ui_capture_seen:\n                    _rpts_ui_capture_seen.add(text)\n                    filename, lineno = _rpts_ui_capture_location()\n                    path = os.path.join(config.gamedir, "rpts_runtime_ui.tsv")\n                    with io.open(path, "a", encoding="utf-8") as fh:\n                        fh.write(_rpts_ui_capture_escape(filename) + "\\t" + _rpts_text_type(lineno) + "\\t" + _rpts_ui_capture_escape(text) + "\\n")\n        except Exception:\n            pass\n        if _rpts_ui_capture_previous is not None:\n            return _rpts_ui_capture_previous(value)\n        return value\n\n    config.replace_text = _rpts_ui_capture_replace\n'''


def install_runtime_capture(db_path: str | Path) -> dict[str, Any]:
    db_path = Path(db_path)
    db = sqlite3.connect(db_path)
    try:
        meta = {}
        for k, v in db.execute("SELECT key,value FROM metadata"):
            try: meta[str(k)] = json.loads(v)
            except Exception: meta[str(k)] = v
        dialogue = {r[0] for r in db.execute("SELECT DISTINCT source_text FROM segments WHERE kind IN ('dialogue','narration','centered','dialogue_extend') AND source_text<>''")}
        ui = {r[0] for r in db.execute("SELECT DISTINCT source_text FROM segments WHERE kind IN ('ui','notification','marked_string','character_name','ui_value','localized_string') AND source_text<>''")}
    finally:
        db.close()
    game_dir = Path(str(meta.get("game_dir") or ""))
    if not game_dir.is_dir():
        raise FileNotFoundError("game_dir from project database is not available")
    path = game_dir / "rpts_ui_capture.rpy"
    path.write_text(runtime_capture_source(dialogue, ui), encoding="utf-8")
    return {"installed": str(path), "dialogue_exclusions": len(dialogue), "known_ui": len(ui)}


def remove_runtime_capture(db_path: str | Path, *, remove_log: bool = False) -> dict[str, int]:
    db = sqlite3.connect(db_path)
    try:
        raw = db.execute("SELECT value FROM metadata WHERE key='game_dir'").fetchone()
        if not raw: raise ValueError("game_dir metadata missing")
        try: game_dir = Path(json.loads(raw[0]))
        except Exception: game_dir = Path(raw[0])
    finally:
        db.close()
    removed = 0
    for name in ("rpts_ui_capture.rpy", "rpts_ui_capture.rpyc"):
        p = game_dir / name
        if p.exists(): p.unlink(); removed += 1
    if remove_log:
        p = game_dir / "rpts_runtime_ui.tsv"
        if p.exists(): p.unlink(); removed += 1
    return {"removed": removed}
