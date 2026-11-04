"""Idempotent upgrade of stored audits; no game execution or model calls."""
from __future__ import annotations
import csv
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from .project_db import build_project_segments, catalogue_segments
from .russian import visible_text
from .multilingual import analyze_multilingual, interpolation_expression_rows, infer_variant_roles, script_counts


def ensure_revision_column(db):
    if "translation_revision" not in {r[1] for r in db.execute("PRAGMA table_info(segments)")}:
        db.execute("ALTER TABLE segments ADD COLUMN translation_revision TEXT NOT NULL DEFAULT ''")


def _upgrade_11_7(db_path, reports_dir):
    path = Path(reports_dir) / "source_candidates.csv"
    db = sqlite3.connect(db_path)
    try:
        ensure_revision_column(db)
        if not path.exists():
            db.commit(); return 0
        key = "audit_extraction_11_7"
        if db.execute("SELECT 1 FROM metadata WHERE key=?", (key,)).fetchone():
            db.commit(); return 0
        with path.open(encoding="utf-8-sig", newline="") as f:
            rows = list(csv.DictReader(f))
        for row in rows:
            row["dynamic"] = row.get("dynamic") in (True, "True", "true", 1, "1")
        declarations = [r for r in rows if r.get("kind") == "character_name"]
        db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("character_declarations", json.dumps(declarations, ensure_ascii=False)))
        explicit = {r[0]: [] for r in db.execute("SELECT DISTINCT function FROM multilingual_variants")}
        dynamic = [r for r in rows if r["dynamic"]]
        _, _, variants, _ = analyze_multilingual(dynamic + interpolation_expression_rows(rows), explicit_functions=explicit)
        counts = script_counts(" ".join(visible_text(str(r.get("text") or "")) for r in rows))
        dominant = max(counts, key=counts.get) if counts else "none"
        variants, _ = infer_variant_roles(variants, dominant)
        segments = build_project_segments([], [], [], [], variants, [])
        added = 0
        for row in segments:
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            columns = list(row)
            added += db.execute("INSERT OR IGNORE INTO segments("+",".join(columns)+") VALUES("+",".join("?" for _ in columns)+")", list(row.values())).rowcount
        # Match a fresh audit's call ordering (ordinary expressions, then interpolations).
        db.execute("DELETE FROM multilingual_variants")
        for v in variants:
            db.execute("INSERT INTO multilingual_variants VALUES(?,?,?,?,?,?,?)", (v["call_id"], v["function"], v["variant_index"], v["text"], v["role_guess"], v.get("file") or "", int(v.get("line") or 0)))
        db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (key, json.dumps({"added": added})))
        db.commit()
        return added
    finally:
        db.close()


def upgrade_project(db_path, reports_dir):
    added = _upgrade_11_7(db_path, reports_dir)
    db = sqlite3.connect(db_path)
    try:
        pairs = [{"file": f, "language": lang, "old": old, "old_line": line}
                 for f, lang, old, line in db.execute("SELECT source_file,language,source_text,old_line FROM translation_memory")]
        known = {r[0] for r in db.execute("SELECT source_text FROM segments")}
        rows = catalogue_segments(pairs, known)
        for row in rows:
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            columns = list(row)
            added += db.execute("INSERT OR IGNORE INTO segments(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", list(row.values())).rowcount
        db.commit()
        return added + _upgrade_interactive(db_path, reports_dir)
    finally:
        db.close()


def _upgrade_interactive(db_path, reports_dir):
    from .interactive import scan_game, message_segments, VERSION
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row
    try:
        metadata = {}
        for key, raw in db.execute("SELECT key,value FROM metadata"):
            try: metadata[key] = json.loads(raw)
            except (ValueError, TypeError): metadata[key] = raw
        if metadata.get("interactive_extraction_version") == VERSION:
            return 0
        inventory = scan_game(metadata.get("game_dir") or "__missing_game__")
        if inventory is None:
            Path(reports_dir).mkdir(parents=True, exist_ok=True)
            (Path(reports_dir) / "interactive_upgrade.json").write_text(json.dumps({"status": "game_unavailable", "game_dir": metadata.get("game_dir"), "action": "Select the installed game and repeat audit to recover ordered messages."}, ensure_ascii=False, indent=2), encoding="utf-8")
            return 0
        rows = message_segments(inventory)
        added = 0
        for row in rows:
            row["updated_at"] = datetime.now(timezone.utc).isoformat()
            # Retain human-approved wording; previous context-free AI wording
            # remains in the legacy row for review, never silently accepted.
            manual = list(db.execute("SELECT translation,provider,model,translation_revision FROM segments WHERE source_text=? AND status='approved' AND translation<>''", (row["source_text"],)))
            if manual and len({r["translation"] for r in manual}) == 1:
                row.update(dict(manual[0]), status="approved")
            columns = list(row)
            added += db.execute("INSERT OR IGNORE INTO segments(" + ",".join(columns) + ") VALUES(" + ",".join("?" for _ in columns) + ")", list(row.values())).rowcount
        for source in {r["source_text"] for r in rows}:
            db.execute("UPDATE segments SET channel='interactive_legacy' WHERE kind='localized_string' AND source_text=? AND origin<>'interactive'", (source,))
        db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("interactive_inventory", json.dumps(inventory, ensure_ascii=False)))
        if not inventory.get("scan_warnings") and not inventory.get("unresolved"):
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", ("interactive_extraction_version", json.dumps(VERSION)))
        db.commit()
        Path(reports_dir).mkdir(parents=True, exist_ok=True)
        (Path(reports_dir) / "interactive_messages.json").write_text(json.dumps(inventory, ensure_ascii=False, indent=2), encoding="utf-8")
        return added
    finally:
        db.close()
