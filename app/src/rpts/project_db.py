from __future__ import annotations

import csv
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

TRANSLATION_REVISION = "11.14"

DIALOGUE_KINDS = {"dialogue", "narration", "centered", "dialogue_extend"}


def _sha(*parts) -> str:
    h = hashlib.sha256()
    for p in parts:
        h.update(str(p if p is not None else "").encode("utf-8"))
        h.update(b"\x1f")
    return h.hexdigest()


def _segment_id(row: dict, origin: str) -> str:
    tid = row.get("translation_id") or ""
    if tid and row.get("kind") in DIALOGUE_KINDS:
        return "renpy:" + tid
    return "seg:" + _sha(
        origin, row.get("file"), row.get("line"), row.get("kind"), row.get("speaker"),
        row.get("normalized_expression") or row.get("expression"), row.get("text")
    )[:24]


def _normalize_segment(row: dict, *, origin: str, channel: str, status: str = "pending") -> dict:
    text = row.get("text") or ""
    return {
        "segment_id": _segment_id(row, origin),
        "source_file": row.get("file") or "",
        "source_line": int(row.get("line") or 0),
        "kind": row.get("kind") or "",
        "channel": channel,
        "origin": origin,
        "source_text": text,
        "speaker": row.get("speaker") or "",
        "label": row.get("label") or "",
        "translation_id": row.get("translation_id") or "",
        "translation_id_source": row.get("translation_id_source") or "",
        "translation_id_alternate": row.get("translation_id_alternate") or "",
        "say_code": row.get("say_code") or "",
        "language": row.get("language") or "",
        "dynamic": 1 if row.get("dynamic") in (True, "True", "true", 1, "1") else 0,
        "expression": row.get("normalized_expression") or row.get("expression") or "",
        "queue_reason": row.get("queue_reason") or "",
        "status": status,
        "translation": "",
        "provider": "",
        "model": "",
        "source_hash": _sha(text),
        "context_json": json.dumps({
            "context_scope": row.get("context_scope"),
            "source": row.get("source") or "",
            "source_context": row.get("source_context") or "",
            "provenance": row.get("provenance") or "",
            "script_hint": row.get("script_hint") or "",
        }, ensure_ascii=False),
    }


def build_project_segments(
    translation_queue: list[dict],
    localization_queue: list[dict],
    dataflow_translation_queue: list[dict],
    dataflow_localization_queue: list[dict],
    multilingual_variants: list[dict],
    runtime_targets: list[dict],
    translation_pairs: list[dict] | None = None,
):
    segments: list[dict] = []
    for row in translation_queue:
        segments.append(_normalize_segment(row, origin="static", channel="semantic"))
    for row in localization_queue:
        segments.append(_normalize_segment(row, origin="static", channel="localization"))
    for row in dataflow_translation_queue:
        segments.append(_normalize_segment(row, origin="dataflow", channel="semantic"))
    for row in dataflow_localization_queue:
        segments.append(_normalize_segment(row, origin="dataflow", channel="localization"))

    # Only the source-like side of a proven multilingual helper is a translation
    # source. Alternate variants remain available in their own table as context/TM.
    for row in multilingual_variants:
        if row.get("role_guess") != "source_like":
            continue
        synthetic = dict(row)
        synthetic.setdefault("kind", "multilingual_variant")
        synthetic["speaker"] = ""
        synthetic["dynamic"] = False
        synthetic["expression"] = row.get("function") or ""
        segments.append(_normalize_segment(synthetic, origin="multilingual", channel="semantic"))

    # Runtime targets are tracked in the same project but not queued to a provider
    # until a runtime observation resolves them to concrete text.
    for row in runtime_targets:
        synthetic = dict(row)
        synthetic["text"] = ""
        segments.append(_normalize_segment(synthetic, origin="runtime", channel="runtime", status="runtime_pending"))

    # Preserve distinct source locations, but collapse exact same stable segment id.
    # For Ren'Py dialogue the translation id is the canonical identity.
    dedup = {}
    for s in segments:
        old = dedup.get(s["segment_id"])
        if old is None:
            dedup[s["segment_id"]] = s
            continue
        # Prefer entries carrying an exact AST translation id source over generated
        # or source-only forms if the same segment was observed twice.
        rank = {"ast_translate_say": 3, "ast_translate": 3, "generated_canonical": 2, "generated_fallback": 1, "": 0}
        if rank.get(s["translation_id_source"], 0) > rank.get(old["translation_id_source"], 0):
            dedup[s["segment_id"]] = s
    result = list(dedup.values())
    result.extend(catalogue_segments(translation_pairs or [], {r["source_text"] for r in result}))
    return result


def catalogue_segments(pairs: list[dict], known_sources: set[str]) -> list[dict]:
    """Recover original `old` strings even when their display is dynamic Python.

    A game's own translation catalogue proves the original string is a localization
    key. Never enqueue the foreign `new` side or invent a Say translation ID.
    """
    from .multilingual import has_human_language_payload
    result = []
    known = set(known_sources)
    # Prefer EN locations for stable neighbourhood/reference context across runs.
    ordered = sorted(pairs, key=lambda p: (str(p.get("language", "")).lower() not in {"en", "english"},
        str(p.get("file", "")), int(p.get("old_line") or 0), str(p.get("old", ""))))
    for pair in ordered:
        source = str(pair.get("old") or "")
        language = str(pair.get("language") or "").strip().lower()
        filename = str(pair.get("file") or "")
        if language in {"", "none"} or source in known or not has_human_language_payload(source):
            continue
        if re.search(r"(?:^|[/\\])common\.rpym?c?$", filename, re.I):
            # Ren'Py's bundled developer/error catalogue is not game dialogue.
            continue
        if re.search(r"(?:^|[/\\])rpts_[^/\\]*\.(?:rpy|rpyc)$", filename, re.I):
            continue
        row = {"text": source, "kind": "localized_string", "file": filename,
            "line": int(pair.get("old_line") or 0), "label": "catalogue:" + filename,
            "source": "translate strings: old", "queue_reason": "original_string_from_translation_catalogue",
            "provenance": "Existing game localization key; foreign translation is reference only"}
        segment = _normalize_segment(row, origin="localization_catalogue", channel="semantic")
        # Runtime string lookup is global by exact original, regardless of which
        # translation language supplied the evidence first.
        segment["segment_id"] = "catalogue:" + _sha(source)[:24]
        result.append(segment); known.add(source)
    return result


def translation_id_coverage(source_items: list[dict], translation_items: list[dict]):
    source_dialogue = [r for r in source_items if r.get("kind") in DIALOGUE_KINDS]
    source_core = [r for r in source_items if r.get("kind") in {"dialogue", "narration", "dialogue_extend"}]
    source_with_id = [r for r in source_dialogue if r.get("translation_id")]
    source_core_with_id = [r for r in source_core if r.get("translation_id")]
    source_exact = [r for r in source_with_id if str(r.get("translation_id_source") or "").startswith("ast_translate")]
    source_generated = [r for r in source_with_id if str(r.get("translation_id_source") or "").startswith("generated_")]
    source_ids = {r.get("translation_id") for r in source_with_id if r.get("translation_id")}
    core_source_ids = {r.get("translation_id") for r in source_core_with_id if r.get("translation_id")}

    by_language = {}
    for row in translation_items:
        if row.get("kind") not in DIALOGUE_KINDS or not row.get("translation_id"):
            continue
        lang = row.get("language") or "unknown"
        by_language.setdefault(lang, set()).add(row.get("translation_id"))

    rows = []
    for lang, ids in sorted(by_language.items()):
        matched = len(source_ids & ids)
        core_matched = len(core_source_ids & ids)
        rows.append({
            "language": lang,
            "source_ids": len(source_ids),
            "translation_ids": len(ids),
            "matched_ids": matched,
            "source_match_ratio": (matched / len(source_ids)) if source_ids else 0.0,
            "core_source_ids": len(core_source_ids),
            "core_matched_ids": core_matched,
            "core_source_match_ratio": (core_matched / len(core_source_ids)) if core_source_ids else 0.0,
        })
    stats = {
        "source_dialogue": len(source_dialogue),
        "source_core_dialogue": len(source_core),
        "source_with_translation_id": len(source_with_id),
        "source_exact_translation_id": len(source_exact),
        "source_generated_translation_id": len(source_generated),
        "source_without_translation_id": len(source_dialogue) - len(source_with_id),
        "unique_source_translation_ids": len(source_ids),
        "unique_core_source_translation_ids": len(core_source_ids),
    }
    return stats, rows


def write_project_database(path: str | Path, *, segments: list[dict], translation_pairs: list[dict], dialogue_translations: list[dict], multilingual_variants: list[dict], runtime_targets: list[dict], metadata: dict):
    path = Path(path)
    if path.exists():
        path.unlink()
    db = sqlite3.connect(path)
    try:
        db.execute("PRAGMA journal_mode=DELETE")
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
        CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE segments (
            segment_id TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            kind TEXT NOT NULL,
            channel TEXT NOT NULL,
            origin TEXT NOT NULL,
            source_text TEXT NOT NULL,
            speaker TEXT NOT NULL,
            label TEXT NOT NULL,
            translation_id TEXT NOT NULL,
            translation_id_source TEXT NOT NULL,
            translation_id_alternate TEXT NOT NULL,
            say_code TEXT NOT NULL,
            language TEXT NOT NULL,
            dynamic INTEGER NOT NULL,
            expression TEXT NOT NULL,
            queue_reason TEXT NOT NULL,
            status TEXT NOT NULL,
            translation TEXT NOT NULL,
            provider TEXT NOT NULL,
            model TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            context_json TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            translation_revision TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX idx_segments_status ON segments(status);
        CREATE INDEX idx_segments_translation_id ON segments(translation_id);
        CREATE INDEX idx_segments_source_file ON segments(source_file, source_line);
        CREATE TABLE translation_memory (
            tm_id TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            language TEXT NOT NULL,
            source_text TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            old_line INTEGER NOT NULL,
            new_line INTEGER NOT NULL,
            occurrence_count INTEGER NOT NULL
        );
        CREATE INDEX idx_tm_source ON translation_memory(source_text, language);
        CREATE TABLE dialogue_translations (
            translation_id TEXT NOT NULL,
            language TEXT NOT NULL,
            translated_text TEXT NOT NULL,
            speaker TEXT NOT NULL,
            kind TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            PRIMARY KEY(translation_id, language, source_file, source_line)
        );
        CREATE INDEX idx_dialogue_tl_id ON dialogue_translations(translation_id, language);
        CREATE TABLE multilingual_variants (
            call_id TEXT NOT NULL,
            function TEXT NOT NULL,
            variant_index INTEGER NOT NULL,
            text TEXT NOT NULL,
            role_guess TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            PRIMARY KEY(call_id, variant_index)
        );
        CREATE TABLE runtime_targets (
            runtime_id TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            source_line INTEGER NOT NULL,
            expression TEXT NOT NULL,
            screen_name TEXT NOT NULL,
            reason TEXT NOT NULL,
            source_context TEXT NOT NULL
        );
        """)
        now = datetime.now(timezone.utc).isoformat()
        for k, v in metadata.items():
            db.execute("INSERT INTO metadata(key,value) VALUES(?,?)", (str(k), json.dumps(v, ensure_ascii=False)))
        for s in segments:
            db.execute("""
                INSERT INTO segments(segment_id,source_file,source_line,kind,channel,origin,source_text,speaker,label,
                    translation_id,translation_id_source,translation_id_alternate,say_code,language,dynamic,expression,queue_reason,
                    status,translation,provider,model,source_hash,context_json,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                s["segment_id"], s["source_file"], s["source_line"], s["kind"], s["channel"], s["origin"], s["source_text"],
                s["speaker"], s["label"], s["translation_id"], s["translation_id_source"], s["translation_id_alternate"], s.get("say_code", ""), s["language"],
                s["dynamic"], s["expression"], s["queue_reason"], s["status"], s["translation"], s["provider"], s["model"],
                s["source_hash"], s["context_json"], now
            ))
        for p in translation_pairs:
            db.execute("INSERT OR REPLACE INTO translation_memory(tm_id,source_file,language,source_text,translated_text,old_line,new_line,occurrence_count) VALUES(?,?,?,?,?,?,?,?)", (
                p.get("pair_id") or _sha(p.get("file"),p.get("language"),p.get("old"),p.get("new")), p.get("file") or "",
                p.get("language") or "", p.get("old") or "", p.get("new") or "", int(p.get("old_line") or 0), int(p.get("new_line") or 0), int(p.get("occurrence_count") or 1)
            ))
        for row in dialogue_translations:
            if row.get("kind") not in DIALOGUE_KINDS or not row.get("translation_id"):
                continue
            db.execute("INSERT OR REPLACE INTO dialogue_translations(translation_id,language,translated_text,speaker,kind,source_file,source_line) VALUES(?,?,?,?,?,?,?)", (
                row.get("translation_id") or "", row.get("language") or "", row.get("text") or "", row.get("speaker") or "",
                row.get("kind") or "", row.get("file") or "", int(row.get("line") or 0)
            ))
        for v in multilingual_variants:
            db.execute("INSERT OR REPLACE INTO multilingual_variants(call_id,function,variant_index,text,role_guess,source_file,source_line) VALUES(?,?,?,?,?,?,?)", (
                v.get("call_id") or "", v.get("function") or "", int(v.get("variant_index") or 0), v.get("text") or "", v.get("role_guess") or "",
                v.get("file") or "", int(v.get("line") or 0)
            ))
        for r in runtime_targets:
            rid = "runtime:" + _sha(r.get("file"),r.get("line"),r.get("normalized_expression") or r.get("expression"))[:24]
            db.execute("INSERT OR REPLACE INTO runtime_targets(runtime_id,source_file,source_line,expression,screen_name,reason,source_context) VALUES(?,?,?,?,?,?,?)", (
                rid, r.get("file") or "", int(r.get("line") or 0), r.get("normalized_expression") or r.get("expression") or "",
                r.get("screen_name") or "", r.get("disposition_reason") or "", r.get("source_context") or ""
            ))
        db.commit()
    finally:
        db.close()


def write_project_segments_csv(path: str | Path, segments: list[dict]):
    path = Path(path)
    fields = [
        "segment_id","source_file","source_line","kind","channel","origin","source_text","speaker","label",
        "translation_id","translation_id_source","translation_id_alternate","say_code","language","dynamic","expression",
        "queue_reason","status","translation","provider","model","source_hash","context_json",
    ]
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader(); w.writerows(segments)
