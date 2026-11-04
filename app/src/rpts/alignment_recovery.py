"""Quarantine outputs of the legacy Hy-MT2 multi-target protocol, with backup.

Provenance is taken from recorded runs, never from the currently selected model.
Approved/manual rows and outputs of unrelated models are left alone.
"""
from __future__ import annotations
from contextlib import closing
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sqlite3
from .model_policy import model_family


def _records(path):
    try:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    value = json.loads(line)
                    if isinstance(value, dict):
                        yield value
                except ValueError:
                    continue  # An interrupted final log record is not evidence.
    except OSError:
        return


def unsafe_runs(db_path):
    runs = {}
    # Normal installation: root/projects/game/work/db. Audit ZIP: root/work/db.
    for parent in list(Path(db_path).resolve().parents)[:4]:
        sessions = parent / "diagnostics" / "sessions"
        for path in sorted(sessions.glob("*/events.jsonl")):
            server_model = ""
            for event in _records(path):
                if event.get("event") == "server_start":
                    server_model = (event.get("settings") or {}).get("model_key", "")
                elif event.get("event") in {"server_stop", "server_stopped"}:
                    server_model = ""
                elif event.get("event") == "translation_run" and not event.get("dry_run"):
                    model = event.get("model_identity") or (server_model if event.get("model") == "rpts-local" else event.get("model"))
                    if (event.get("response_protocol") != "single_text"
                            and model_family({"model": model}) == "hy_mt2" and event.get("run_id")):
                        runs[event["run_id"]] = "recorded Hy-MT2 run using legacy multi-target output"
    quality = Path(db_path).parent / "quality"
    for path in quality.glob("translation_batches*.jsonl"):
        for batch in _records(path):
            if not batch.get("run_id") or batch.get("response_protocol") == "single_text":
                continue
            if model_family({"model_identity": batch.get("model_identity")}) == "hy_mt2":
                runs[batch["run_id"]] = "recorded Hy-MT2 batch using legacy multi-target output"
            # Old batch logs lack model identity. This exact instruction example
            # in unrelated output proves contamination, without guessing a model.
            sources = {r.get("id"): r.get("text", "") for r in batch.get("items", [])}
            for row in batch.get("response") or []:
                source = sources.get(row.get("id"))
                if (source is not None and "Рука подняла меня" in row.get("text", "")
                        and not re.search(r"手|臂|рук|\b(?:hand|arm)\b", source, re.I)):
                    runs[batch["run_id"]] = "instruction example leaked into unrelated translation in this run"
    return runs


def recover_legacy_alignment(db_path):
    return _quarantine(db_path, unsafe_runs(db_path))


def recover_legacy_review(db_path):
    runs, selected = {}, set()
    for path in (Path(db_path).parent / "quality").glob("translation_batches*.jsonl"):
        for batch in _records(path):
            if (batch.get("translation_protocol") not in {None, "", "11.17"}
                    or batch.get("response_protocol") != "single_text"
                    or model_family({"model_identity": batch.get("model_identity")}) != "hy_mt2"
                    or not batch.get("review_passes") or not batch.get("run_id")):
                continue
            run_id = batch["run_id"]
            runs[run_id] = "Hy-MT2 free editing in 11.17 can merge background turns and change source meaning"
            for item in batch.get("items") or []:
                if item.get("id"):
                    selected.add((run_id, item["id"]))
    return _quarantine(db_path, runs, selected=selected, version="11.18", report_name="review_recovery_11_18")


def recover_project_translations(db_path):
    legacy = recover_legacy_alignment(db_path)
    review = recover_legacy_review(db_path)
    return {"quarantined": legacy["quarantined"] + review["quarantined"], "alignment": legacy, "review": review}


def _quarantine(db_path, runs, *, selected=None, version="11.17", report_name="alignment_recovery_11_17"):
    db_path = Path(db_path)
    if not db_path.is_file():
        return {"quarantined": 0}
    if not runs:
        return {"quarantined": 0}
    with closing(sqlite3.connect(db_path)) as db:
        db.row_factory = sqlite3.Row
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"segments", "translation_attempts"} <= tables:
            return {"quarantined": 0}
        rows = []
        query = """SELECT s.segment_id,s.source_hash,s.source_text,s.translation,s.provider,s.model,
                          s.updated_at,a.run_id,a.status AS attempt_status,a.created_at
                   FROM segments s JOIN translation_attempts a ON a.attempt_id=(
                       SELECT max(a2.attempt_id) FROM translation_attempts a2 WHERE a2.segment_id=s.segment_id)
                   WHERE s.status IN ('translated','translated_tm')
                     AND s.provider IN ('openai_compatible','ollama') AND s.translation<>''"""
        for row in db.execute(query):
            if row["run_id"] not in runs or row["attempt_status"] not in {"ok", "repair_ok", "cached", "tm"}:
                continue
            if selected is not None and (row["run_id"], row["segment_id"]) not in selected:
                continue
            # Manual edits can retain a provider field. Never overwrite a row
            # updated after the recorded AI write, even if it is not approved.
            if row["updated_at"] > row["created_at"]:
                continue
            rows.append(dict(row))
        if not rows:
            return {"quarantined": 0}
        stamp = datetime.now(timezone.utc)
        backup = db_path.parent / "backups" / ("before-recovery-" + version + "-" + stamp.strftime("%Y%m%d-%H%M%S-%f") + ".sqlite")
        backup.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(backup)) as target:
            db.backup(target)
        changed = []
        with db:
            for row in rows:
                cur = db.execute("""UPDATE segments SET status='error',updated_at=?,
                    queue_reason=queue_reason || ?
                    WHERE segment_id=? AND updated_at=? AND status IN ('translated','translated_tm')
                      AND translation=? AND provider=? AND model=?""",
                    (stamp.isoformat(), f" [{version}: legacy AI output; retranslate]", row["segment_id"], row["updated_at"], row["translation"], row["provider"], row["model"]))
                if cur.rowcount:
                    changed.append(row)
                    if "translation_cache" in tables:
                        db.execute("DELETE FROM translation_cache WHERE source_hash=? AND provider=? AND model=?",
                                   (row["source_hash"], row["provider"], row["model"]))
            result = {"version": version, "quarantined": len(changed), "backup": str(backup),
                      "runs": {r["run_id"]: runs[r["run_id"]] for r in changed}, "rows": changed}
            if "metadata" in tables:
                db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)",
                           (report_name, json.dumps(result, ensure_ascii=False)))
    report = db_path.parent / "quality" / (report_name + ".json")
    report.parent.mkdir(parents=True, exist_ok=True)
    try:
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        print(f"[WARN] Отчёт восстановления сохранён в базе; запись файла не удалась: {exc}", flush=True)
    print(f"[RECOVERY] {len(changed)} строк старого ИИ-перевода отмечены для повторного перевода. Резервная копия: {backup}", flush=True)
    return result
