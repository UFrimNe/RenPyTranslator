from __future__ import annotations

import csv
import json
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .russian import repair_first_person, russian_gender_warning, russian_issues, coverage_warning
from .tokens import strip_protected_for_context

GENDERS = {"male", "female", "unknown"}


def is_english_language(value: str | None) -> bool:
    v = str(value or "").strip().lower().replace("_", "-")
    return v in {"en", "eng", "english", "en-us", "en-gb"} or v.startswith("en-")


def english_reference_maps(db: sqlite3.Connection) -> tuple[dict[str, str], dict[str, str]]:
    """Return (translation_id -> EN text, source_text -> EN text).

    The first map is the strongest semantic anchor for Ren'Py dialogue/narration.
    The second one covers string translations such as names and UI text.
    """
    by_id: dict[str, str] = {}
    try:
        for tid, language, text in db.execute(
            "SELECT translation_id,language,translated_text FROM dialogue_translations WHERE translated_text<>''"
        ):
            if tid and is_english_language(language) and tid not in by_id:
                by_id[str(tid)] = str(text)
    except sqlite3.Error:
        pass

    by_source: dict[str, str] = {}
    try:
        rows = db.execute(
            "SELECT source_text,language,translated_text,occurrence_count FROM translation_memory "
            "WHERE source_text<>'' AND translated_text<>'' ORDER BY occurrence_count DESC"
        ).fetchall()
        for source, language, text, _count in rows:
            if source and is_english_language(language) and str(source) not in by_source:
                by_source[str(source)] = str(text)
    except sqlite3.Error:
        pass
    return by_id, by_source


def reference_for_row(row: dict[str, Any], by_id: dict[str, str], by_source: dict[str, str]) -> str:
    tid = str(row.get("translation_id") or "")
    if tid and tid in by_id:
        return by_id[tid]
    return by_source.get(str(row.get("source_text") or ""), "")


def _character_decl_alias(source: str) -> str:
    # Covers common source/unrpyc forms:
    #   define e = Character("Eileen")
    #   e = Character("Eileen")
    m = re.match(r"\s*(?:(?:define|default)\s+)?([A-Za-z_]\w*)\s*=\s*Character\s*\(", source or "")
    return m.group(1) if m else ""


def _infer_gender_from_english_mentions(name: str, texts: list[str]) -> tuple[str, list[str]]:
    """Only direct titles, definitions and subject-bound reflexives are evidence."""
    name = str(name or "").strip()
    if not name or len(name) < 2:
        return "unknown", []
    n = re.escape(name)
    male_rx = [
        re.compile(rf"\b(?:mr|mister)\.?\s+{n}\b", re.I),
        re.compile(rf"\b{n}\b\s+(?:is|was)\s+(?:(?:a|the|my|our|his|her)\s+)?(?:male|man|boy|brother|son|father|husband|boyfriend)\b", re.I),
        re.compile(rf"\b{n}\b\s+(?:introduced|washed|dressed|blamed|hurt|looked\s+(?:down\s+)?at)\s+himself\b", re.I),
    ]
    female_rx = [
        re.compile(rf"\b(?:mrs|ms|miss)\.?\s+{n}\b", re.I),
        re.compile(rf"\b{n}\b\s+(?:is|was)\s+(?:(?:a|the|my|our|his|her)\s+)?(?:female|woman|girl|sister|daughter|mother|wife|girlfriend)\b", re.I),
        re.compile(rf"\b{n}\b\s+(?:introduced|washed|dressed|blamed|hurt|looked\s+(?:down\s+)?at)\s+herself\b", re.I),
    ]
    male: list[str] = []
    female: list[str] = []
    for text in texts:
        t = str(text or "")
        # Questions, negation, conditions and reported guesses are not facts.
        # Conservative exclusion is preferable to assigning a false gender.
        if "?" in t or re.search(r"\b(?:not|never|if|suppose|maybe|perhaps|think|thought|believe|believed)\b|n't\b", t, re.I):
            continue
        if any(rx.search(t) for rx in male_rx):
            male.append(t)
        if any(rx.search(t) for rx in female_rx):
            female.append(t)
    if male and not female:
        return "male", male[:5]
    if female and not male:
        return "female", female[:5]
    return "unknown", (male + female)[:5]


def _load_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Profile file root must be a JSON object")
        return value
    except Exception as exc:
        raise ValueError(f"Cannot read character profiles {path}: {exc}. Existing file was not overwritten.") from exc


def load_character_profiles(path: str | Path) -> dict[str, dict[str, Any]]:
    value = _load_json(Path(path))
    raw = value.get("characters", value)
    if not isinstance(raw, dict):
        raise ValueError(f"Invalid characters object in {path}. Existing file was not overwritten.")
    out: dict[str, dict[str, Any]] = {}
    for key, profile in raw.items():
        if not isinstance(profile, dict):
            raise ValueError(f"Invalid character profile {key!r} in {path}. Existing file was not overwritten.")
        p = dict(profile)
        gender = str(p.get("gender") or "unknown").lower()
        p["gender"] = gender if gender in GENDERS else "unknown"
        out[str(key)] = p
    return out


def save_character_profiles(path: str | Path, profiles: dict[str, dict[str, Any]]) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": 3,
        "note": (
            "gender is authoritative for translation when male/female. Set locked=true after manual correction. "
            "AI profiles cite source evidence and confidence. field_locks/voice_locked preserve partial manual edits."
        ),
        "characters": dict(sorted(profiles.items())),
    }
    tmp = p.with_name(p.name + ".tmp-" + uuid.uuid4().hex)
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(p)
    finally:
        tmp.unlink(missing_ok=True)
    return p


def discover_character_profiles(db: sqlite3.Connection, existing: dict[str, dict[str, Any]] | None = None) -> dict[str, dict[str, Any]]:
    from .characters import discover_profiles
    return discover_profiles(db, existing)


def auto_profile_unknown_genders(db, provider, profiles, *, enabled=True, max_profiles_per_call=8):
    from .characters import analyze_character_profiles
    return analyze_character_profiles(db, provider, profiles) if enabled else profiles


def profile_for_speaker(profiles: dict[str, dict[str, Any]], speaker: str | None) -> dict[str, Any]:
    if not speaker:
        return {"gender": "unknown", "display_name": "", "english_name": "", "style": "", "notes": ""}
    p = dict(profiles.get(str(speaker)) or {})
    p.setdefault("gender", "unknown")
    p.setdefault("display_name", str(speaker))
    p.setdefault("english_name", "")
    p.setdefault("style", "")
    p.setdefault("notes", "")
    return p


def compact_character_profile(profile: dict[str, Any]) -> dict[str, Any]:
    from .characters import locked_fields
    trusted = locked_fields(profile)
    limits = {"display_name": 80, "english_name": 80, "russian_name": 80, "style": 240, "notes": 360, "description": 160, "personality": 160, "humor": 60}
    out = {key: str(profile[key])[:limit] for key, limit in limits.items() if profile.get(key)}
    out["gender"] = profile.get("gender") if profile.get("gender") in GENDERS else "unknown"
    for key in ("register", "address", "profanity", "profanity_tendency", "verbosity", "entity_type"):
        if profile.get(key):
            out[key] = str(profile[key])[:40]
    if not profile.get("locked"):
        # Generated personality/register claims are hypotheses, not voice
        # instructions. Source dialogue sets tone; human-locked voice is kept.
        for key in ("description", "personality", "humor", "style", "register", "address", "verbosity", "profanity_tendency"):
            if key not in trusted:
                out.pop(key, None)
    for key in ("species", "interpolation_variables", "narrator_viewpoint"):
        if profile.get(key):
            out[key] = profile[key]
    relationships = profile.get("relationships", []) if (profile.get("locked") or "relationships" in trusted) else []
    if isinstance(relationships, list) and relationships:
        out["relationships"] = [{"speaker_id": r["speaker_id"], "relation": str(r.get("relation") or "")[:80], "address": r.get("address", "contextual"), "tone": str(r.get("tone") or "")[:80]} for r in relationships[:6] if isinstance(r, dict) and isinstance(r.get("speaker_id"), str)]
    return out


def auto_profile_character_voices(db, provider, profiles, *, model_identity="", max_lines=8):
    """Compatibility entry point for the unified complete-character analyzer."""
    from .characters import analyze_character_profiles
    updated = analyze_character_profiles(db, provider, profiles, model_identity=model_identity, options={"profile_sample_lines": max_lines})
    if updated is not profiles:
        profiles.clear(); profiles.update(updated)
    return profiles


def translation_mode(kind: str | None) -> str:
    k = str(kind or "")
    if k in {"dialogue", "dialogue_extend", "localized_string"}:
        return "dialogue"
    if k in {"narration", "centered"}:
        return "narration"
    return "ui"


def gender_warning(profile: dict[str, Any], translated: str) -> str:
    return russian_gender_warning(str(profile.get("gender") or "unknown"), str(translated or ""))



def deterministic_gender_repair(profile: dict[str, Any], translated: str) -> tuple[str, list[str]]:
    """Repair only explicit first-person gender forms already guarded by gender_warning.

    This is a last-resort deterministic fallback after an LLM repair still violates
    a locked/known character gender. It never guesses gender and never rewrites
    third-person references.
    """
    return repair_first_person(str(profile.get("gender") or "unknown"), translated)

def write_quality_report(db: sqlite3.Connection, profiles: dict[str, dict[str, Any]], output_dir: str | Path) -> dict[str, Any]:
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    warnings: list[dict[str, Any]] = []
    review_rows = []
    by_id, by_source = english_reference_maps(db)
    latest_errors = {}
    try:
        latest_errors = {sid: error for sid, error in db.execute(
            "SELECT a.segment_id,a.error FROM translation_attempts a JOIN "
            "(SELECT segment_id,MAX(attempt_id) AS last_id FROM translation_attempts GROUP BY segment_id) b "
            "ON a.attempt_id=b.last_id WHERE a.error<>''")}
    except sqlite3.Error:
        pass
    from .translation_engine import _all_context_rows, _context_for
    from .identity import resolve_scene_roles, identity_warning
    context_rows, context_index = _all_context_rows(db)
    profile_path = out / "character_profiles_review.csv"
    profile_rows = []
    for speaker, p in sorted(profiles.items()):
        profile_rows.append({"speaker": speaker, "display_name": p.get("display_name", ""), "english_name": p.get("english_name", ""), "russian_name": p.get("russian_name", ""),
            "gender": p.get("gender", "unknown"), "gender_confidence": p.get("gender_confidence", ""), "gender_source": p.get("gender_source", ""), "gender_reason": p.get("gender_reason", ""),
            "gender_evidence": json.dumps(p.get("gender_evidence", []), ensure_ascii=False), "entity_type": p.get("entity_type", "unknown"), "description": p.get("description", ""), "personality": p.get("personality", ""),
            "style": p.get("style", ""), "address": p.get("address", "contextual"), "relationships": json.dumps(p.get("relationships", []), ensure_ascii=False),
            "status": "locked" if p.get("locked") else p.get("profile_status", "pending"), "review_reason": "; ".join(p.get("profile_review_reasons", [])), "error": p.get("profile_error", "")})
    if profile_rows:
        with profile_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(profile_rows[0])); writer.writeheader(); writer.writerows(profile_rows)
    cur = db.execute("SELECT segment_id,source_file,source_line,kind,speaker,source_text,translation,translation_id,status,label,context_json,origin FROM segments ORDER BY source_file,source_line,segment_id")
    for sid, filename, line, kind, speaker, source, translated, tid, status, label, context_json, origin in cur:
        profile = profile_for_speaker(profiles, speaker)
        issues = russian_issues(str(source or ""), str(translated or ""), kind, include_style_warnings=True) if translated else []
        if translated and translation_mode(kind) != "ui":
            coverage = coverage_warning(source, translated, reference_for_row({"source_text": source, "translation_id": tid}, by_id, by_source))
            if coverage:
                issues.append(coverage)
        gender_issue = gender_warning(profile, str(translated or "")) if kind in {"dialogue", "dialogue_extend"} else ""
        identity_row = {"segment_id": sid, "source_file": filename, "source_line": line, "kind": kind, "speaker": speaker, "source_text": source, "label": label, "context_json":context_json, "origin":origin}
        context = _context_for(identity_row, context_rows, context_index, 4, 3)
        identity_row.update(resolve_scene_roles(identity_row, context, profiles))
        identity_issue = identity_warning(identity_row, translated, profiles) if translated else ""
        if identity_issue:
            issues.append(identity_issue)
        if gender_issue:
            issues.append(gender_issue)
        if gender_issue or "gender" in identity_issue:
            warnings.append({"segment_id": sid, "source_file": filename, "source_line": line, "speaker": speaker,
                "gender": (identity_row.get("viewpoint") or identity_row.get("addressee") or profile).get("gender", "unknown"),
                "source_text": source, "translation": translated, "reason": gender_issue or identity_issue})
        review_rows.append({"segment_id": sid, "source_file": filename, "source_line": line, "kind": kind, "speaker": speaker,
            "gender": profile.get("gender", "unknown"), "russian_name": profile.get("russian_name", ""), "style": profile.get("style", ""),
            "source_text": source, "reference_en": reference_for_row({"source_text": source, "translation_id": tid}, by_id, by_source),
            "translation": translated, "status": status, "review_reason": "; ".join(issues),
            "last_error": latest_errors.get(sid, "") if status == "error" else ""})
    review_path = out / "translation_review.csv"
    if review_rows:
        with review_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=list(review_rows[0]))
            writer.writeheader(); writer.writerows(review_rows)
    csv_path = out / "suspicious_gender.csv"
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        fields = ["segment_id", "source_file", "source_line", "speaker", "gender", "source_text", "translation", "reason"]
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader(); w.writerows(warnings)

    stats = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "characters": len(profiles),
        "gender_known": sum(1 for p in profiles.values() if p.get("gender") in {"male", "female"}),
        "gender_unknown": sum(1 for p in profiles.values() if p.get("gender") not in {"male", "female"}),
        "suspicious_gender_translations": len(warnings),
        "voice_profiles": sum(bool(p.get("style")) for p in profiles.values()),
        "voice_profile_errors": sum(bool(p.get("profile_error") or p.get("voice_error")) for p in profiles.values()),
        "ai_profiles": sum(bool(p.get("profile_version") and p.get("profile_source_hash")) for p in profiles.values()),
        "profiles_need_review": sum(p.get("profile_status") in {"needs_review", "error", "pending"} and not p.get("locked") for p in profiles.values()),
        "suspicious_translations": sum(bool(r["review_reason"]) for r in review_rows),
        "pending_or_error": sum(r["status"] in {"pending", "error", "runtime_pending"} for r in review_rows),
        "files": {"suspicious_gender": str(csv_path), "translation_review": str(review_path), "character_profiles_review": str(profile_path)},
    }
    (out / "quality_report.json").write_text(json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats
