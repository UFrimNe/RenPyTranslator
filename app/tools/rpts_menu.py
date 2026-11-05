from __future__ import annotations

import getpass
import builtins
import json
import os
import shutil
import sqlite3
import hashlib
import re
import subprocess
import sys
import tempfile
import time
import uuid
import urllib.request
import zipfile
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent if APP.name == "app" else APP
TEMPLATES = APP / "templates" if (APP / "templates").is_dir() else APP
SETTINGS = ROOT / "data" / "settings"
sys.path.insert(0, str(APP / "src"))
from rpts.local_ai import (
    MODEL_SPECS, LEGACY_LOCAL_NOTES, detect_hardware, recommend_model, recommended_backend, required_free_bytes,
    disk_info, enumerate_storage_locations, human_bytes, installation_status, load_settings,
    save_settings, install_engine, install_model, make_translation_config, start_server,
    stop_server, model_runtime_estimate, delete_local_ai, recommended_parallel_capacity,
    recommended_runtime_context, recommended_context_per_slot, benchmark_parallel_workers,
)
from rpts import diagnostics
from rpts.workspace import Workspace, OperationLock, read_json, write_json
WORKSPACE = Workspace(ROOT).initialize()
from rpts.model_policy import model_family, uses_source_only_review
REPORTS = WORKSPACE.reports
REPORT_DB = REPORTS / "translation_project.sqlite"
PROJECTS = WORKSPACE.projects
ACTIVE_PROJECT = ROOT
WORK = WORKSPACE.data / "work"
WORK_DB = WORK / "translation_project.sqlite"
FREE_GOOGLE_CFG = TEMPLATES / "translation_config.google_free.example.json"
CLOUD_GOOGLE_CFG = TEMPLATES / "translation_config.google_cloud.example.json"
MOCK_CFG = TEMPLATES / "translation_config.mock.json"
FREE_USER_CFG = SETTINGS / "translation_config.google_free.json"
UI_OVERRIDES = ROOT / "ui_overrides.json"
UI_OVERRIDES_EXAMPLE = TEMPLATES / "ui_overrides.example.json"
UI_WORK = WORK / "ui"
UI_KINDS = "ui,notification,marked_string,character_name,ui_value,localized_string"
LOCAL_AI_SETTINGS = SETTINGS / "local_ai_settings.json"
LOCAL_AI_CFG = SETTINGS / "translation_config.local_ai.json"
LOCAL_AI_CFG_EXAMPLE = TEMPLATES / "translation_config.local_ai.example.json"
LOCAL_AI_LOG = WORK / "local_ai" / "llama-server.log"
LOCAL_AI_BACKUPS = WORK / "backups"
LOCAL_AI_QUALITY = WORK / "quality"


def _metadata_value(db_path: Path, key: str) -> str:
    if not db_path.exists():
        return ""
    db = sqlite3.connect(db_path)
    try:
        row = db.execute("SELECT value FROM metadata WHERE key=?", (key,)).fetchone()
    except Exception:
        row = None
    finally:
        db.close()
    if not row:
        return ""
    try:
        value = json.loads(row[0])
        return str(value)
    except Exception:
        return str(row[0])


def _activate_project_workspace(*, migrate_legacy: bool = True) -> None:
    global ACTIVE_PROJECT, WORK, WORK_DB, UI_OVERRIDES, UI_WORK, LOCAL_AI_LOG, LOCAL_AI_BACKUPS, LOCAL_AI_QUALITY, REPORTS, REPORT_DB
    selected = os.environ.get("RPTS_PROJECT_ID") or read_json(SETTINGS / "desktop.json").get("active_project")
    project = None
    if selected:
        try: project = WORKSPACE.project(selected)
        except ValueError:
            if os.environ.get('RPTS_PROJECT_ID'): raise
    if project is not None:
        ACTIVE_PROJECT = project
        REPORTS = project / "reports"
        REPORT_DB = REPORTS / "translation_project.sqlite"
        WORK = project / "work"
        WORK_DB = WORK / "translation_project.sqlite"
        UI_OVERRIDES = project / "ui_overrides.json"
        if migrate_legacy and not UI_OVERRIDES.exists() and (ROOT / "ui_overrides.json").is_file():
            shutil.copy2(ROOT / "ui_overrides.json", UI_OVERRIDES)
        UI_WORK = WORK / "ui"
        LOCAL_AI_LOG = WORK / "local_ai" / "llama-server.log"
        LOCAL_AI_BACKUPS = WORK / "backups"
        LOCAL_AI_QUALITY = WORK / "quality"
        return
    game_dir = _metadata_value(REPORT_DB, "game_dir")
    if not game_dir:
        return
    game = Path(game_dir)
    display = game.parent.name or game.name or "game"
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", display).strip("._-")[:40] or "game"
    digest = hashlib.sha1(str(game.resolve()).lower().encode("utf-8", "replace")).hexdigest()[:10]
    project = PROJECTS / f"{slug}-{digest}"
    new_work = project / "work"
    legacy_work = WORKSPACE.data / "work"
    if migrate_legacy and not (new_work / "translation_project.sqlite").exists() and (legacy_work / "translation_project.sqlite").exists():
        old_game = _metadata_value(legacy_work / "translation_project.sqlite", "game_dir")
        if not old_game or Path(old_game).resolve() == game.resolve():
            new_work.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(legacy_work, new_work, dirs_exist_ok=True)
            print(f"[MIGRATE] Старый work перенесён в projects\\{project.name}\\work (оригинал оставлен как backup).")
    ACTIVE_PROJECT = project
    WORK = new_work
    WORK_DB = WORK / "translation_project.sqlite"
    UI_OVERRIDES = project / "ui_overrides.json"
    legacy_ui = ROOT / "ui_overrides.json"
    if migrate_legacy and not UI_OVERRIDES.exists() and legacy_ui.exists():
        shutil.copy2(legacy_ui, UI_OVERRIDES)
    UI_WORK = WORK / "ui"
    LOCAL_AI_LOG = WORK / "local_ai" / "llama-server.log"
    LOCAL_AI_BACKUPS = WORK / "backups"
    LOCAL_AI_QUALITY = WORK / "quality"
    project.mkdir(parents=True, exist_ok=True)


def _run(*args: str | Path, quiet: bool = False, capture: list[str] | None = None) -> int:
    script, *arguments = args
    cmd = [sys.executable, str(APP / script), *[str(x) for x in arguments]]
    command_id = uuid.uuid4().hex
    started = time.monotonic()
    diagnostics.event("command_start", command_id=command_id, command=cmd, project=str(ACTIVE_PROJECT))
    env = os.environ.copy()
    env.update(PYTHONUTF8="1", PYTHONIOENCODING="utf-8", PYTHONUNBUFFERED="1", RPTS_COMMAND_ID=command_id)
    proc = None
    try:
        proc = subprocess.Popen(cmd, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, encoding="utf-8", errors="replace", bufsize=1)
        for line in proc.stdout:
            diagnostics.console(line)
            if capture is not None:
                capture.append(line)
            if not quiet:
                print(line, end="", flush=True)
        rc = int(proc.wait())
        diagnostics.event("command_end", command_id=command_id, return_code=rc, elapsed_seconds=round(time.monotonic()-started, 3))
        return rc
    except KeyboardInterrupt:
        if proc and proc.poll() is None:
            # Ctrl+C reaches the child too; allow it to commit its interrupted run.
            try: proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                try: proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    proc.kill(); proc.wait()
        diagnostics.event("command_cancelled", command_id=command_id)
        print("\nОтменено пользователем.")
        return 130
    except Exception:
        diagnostics.exception("command_error", command_id=command_id)
        raise
    finally:
        if proc and proc.stdout:
            proc.stdout.close()


def _pause() -> None:
    try:
        _input("\nНажми Enter, чтобы продолжить...")
    except (EOFError, KeyboardInterrupt):
        pass


def _input(prompt: str) -> str:
    value = builtins.input(prompt)
    diagnostics.event("user_choice", prompt=prompt.strip(), value=value, project=str(ACTIVE_PROJECT))
    return value


def _perform(fn, *args, **kwargs):
    name = getattr(fn, "__name__", "action")
    diagnostics.event("action_start", action=name, project=str(ACTIVE_PROJECT))
    started = time.monotonic()
    try:
        rc = fn(*args, **kwargs)
    except KeyboardInterrupt:
        diagnostics.event("action_cancelled", action=name)
        print("\nОстановлено. Для продолжения выбери пункт 1 в главном меню.")
        return 130
    except Exception as exc:
        diagnostics.exception("action_error", action=name)
        print(f"[ERROR] {type(exc).__name__}: {exc}")
        print("Собери ZIP-отчёт: пункт 3 главного меню. Подробности сохранены в диагностике.")
        return 1
    diagnostics.event("action_end", action=name, result=rc, elapsed_seconds=round(time.monotonic()-started, 3))
    if rc:
        print("Если нужна помощь, выбери пункт 3 главного меню и отправь RenPyAuditReport.zip.")
    return rc


def _new_text_mode() -> str:
    try:
        value = json.loads((SETTINGS / "menu_preferences.json").read_text(encoding="utf-8")).get("text_mode")
        if value in {"balanced", "reviewed", "full_review"}:
            return value
    except (OSError, ValueError, AttributeError):
        pass
    return "reviewed"


def _apply_text_mode(cfg: dict, mode: str) -> None:
    quality = cfg.setdefault("quality", {})
    quality.update(text_mode=mode, editorial_pass=mode == "full_review", fidelity_pass=mode == "full_review",
                   combined_review=mode == "reviewed", stage_checkpoints=True)
    if mode != "balanced":
        quality.update(enabled=True, russian_localization=True, scene_aware_batches=True,
                       shared_scene_context=True, use_english_reference=True, deterministic_gender_fallback=False)
    cfg.setdefault("translation", {})["refresh_legacy_ai"] = True


def _clear() -> None:
    os.system("cls" if os.name == "nt" else "clear")


def _open(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(path))  # type: ignore[attr-defined]
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path)])


def _ensure_free_google_config() -> None:
    FREE_USER_CFG.parent.mkdir(parents=True, exist_ok=True)
    if not FREE_USER_CFG.exists():
        if FREE_GOOGLE_CFG.exists():
            shutil.copy2(FREE_GOOGLE_CFG, FREE_USER_CFG)
            print("[OK] Создан translation_config.google_free.json.")
        return
    # Stage 7.2 used one HTTP request per Ren'Py segment. Upgrade only that old
    # conservative shape; keep the user's language and unrelated custom values.
    try:
        cfg = json.loads(FREE_USER_CFG.read_text(encoding="utf-8"))
        if str((cfg.get("provider") or {}).get("type") or "").lower() not in {"google_free", "google-free", "google_web", "google-web", "google_unofficial"}:
            return
        batch = cfg.setdefault("batch", {})
        provider = cfg.setdefault("provider", {})
        translation = cfg.setdefault("translation", {})
        old_segments = int(batch.get("max_segments", 1) or 1)
        changed = False
        if old_segments <= 1:
            batch["max_segments"] = 64
            batch["max_chars"] = 2200
            changed = True
        if float(provider.get("request_delay_seconds", 0.0) or 0.0) < 0.2:
            provider["request_delay_seconds"] = 0.35
            changed = True
        if int(provider.get("max_retries", 2) or 2) > 2:
            provider["max_retries"] = 2
            changed = True
        old_endpoint = str(provider.get("endpoint") or "")
        if (not old_endpoint) or "translate.googleapis.com/translate_a/single" in old_endpoint:
            provider["endpoint"] = "https://translate.google.com/_/TranslateWebserverUi/data/batchexecute"
            provider["model"] = "google-web-rpc"
            changed = True
        translation["progress_every_batches"] = 1
        if changed:
            FREE_USER_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print("[OK] Google Free конфиг обновлён: browser RPC + пакетный режим.")
    except Exception as e:
        print(f"[WARN] Не удалось автоматически обновить translation_config.google_free.json: {e}")




def _latest_audit_complete() -> bool:
    report_path = REPORTS / "report.json"
    if not report_path.exists():
        return True
    try:
        data = json.loads(report_path.read_text(encoding="utf-8"))
        failed = int((data.get("summary") or {}).get("files_failed", 0) or 0)
    except Exception as exc:
        print(f"[ERROR] Не удалось проверить reports\\report.json: {exc}")
        return False
    if failed > 0:
        print(f"[BLOCK] Последний аудит неполный: failed files = {failed}.")
        print("[BLOCK] Сначала повтори аудит и добейся 0 failed; перевод по неполной базе запрещён.")
        return False
    summary = data.get("summary") or {}
    parser_errors = int(summary.get("parser_errors", 0) or 0)
    if parser_errors:
        print(f"[BLOCK] Ошибки чтения или синтаксиса исходников: {parser_errors}.")
        print("[BLOCK] Проверь reports\\extraction_warnings.csv и повтори аудит после исправления файлов.")
        return False
    backends = summary.get("backend_files") or {}
    native_count = int(backends.get("native", 0) or 0)
    unrpyc_ok = bool(summary.get("unrpyc_available"))
    if native_count > 0 and not unrpyc_ok:
        print(f"[BLOCK] Последний compiled-аудит выполнен только native backend ({native_count} файлов).")
        print("[BLOCK] Для полного перевода сначала установи unrpyc и повтори аудит.")
        return False
    return True


def _sync_work_from_report() -> bool:
    """Refresh work DB from the latest audit while preserving completed translations."""
    WORK.mkdir(parents=True, exist_ok=True)
    if not WORK_DB.exists():
        shutil.copy2(REPORT_DB, WORK_DB)
        print(f"[OK] Создан {WORK_DB.relative_to(ROOT)}")
        return True

    tmp = WORK / ".translation_project.refresh.sqlite"
    try:
        tmp.unlink(missing_ok=True)
        shutil.copy2(REPORT_DB, tmp)
        old = sqlite3.connect(WORK_DB)
        new = sqlite3.connect(tmp)
        try:
            old_cols = [r[1] for r in old.execute("PRAGMA table_info(segments)")]
            new_cols = [r[1] for r in new.execute("PRAGMA table_info(segments)")]
            has_hash = "source_hash" in old_cols and "source_hash" in new_cols
            select_cols = "segment_id,translation,status,provider,model,updated_at" + (",source_hash" if has_hash else "")
            translated = old.execute(f"SELECT {select_cols} FROM segments WHERE translation<>'' OR status='error'").fetchall()
            carried = 0
            changed_source = 0
            for values in translated:
                segment_id, translation, status, provider, model, updated_at = values[:6]
                if has_hash:
                    source_hash = values[6]
                    cur = new.execute(
                        "UPDATE segments SET translation=?,status=?,provider=?,model=?,updated_at=? WHERE segment_id=? AND source_hash=?",
                        (translation, status, provider, model, updated_at, segment_id, source_hash),
                    )
                    if not cur.rowcount and new.execute("SELECT 1 FROM segments WHERE segment_id=?", (segment_id,)).fetchone():
                        changed_source += 1
                else:
                    # Compatibility only for pre-Stage-5/legacy test databases that
                    # never had source_hash. Current RPTS databases always take the
                    # strict hash-protected path above.
                    cur = new.execute(
                        "UPDATE segments SET translation=?,status=?,provider=?,model=?,updated_at=? WHERE segment_id=?",
                        (translation, status, provider, model, updated_at, segment_id),
                    )
                if cur.rowcount and "translation_revision" in old_cols and "translation_revision" in new_cols:
                    revision = old.execute("SELECT translation_revision FROM segments WHERE segment_id=?", (segment_id,)).fetchone()[0]
                    new.execute("UPDATE segments SET translation_revision=? WHERE segment_id=?", (revision, segment_id))
                carried += int(cur.rowcount or 0)

            # Preserve UI strings learned from the runtime capture. They do not
            # exist in a fresh static audit, so copying translations by segment_id
            # alone would silently discard them.
            runtime_rows = []
            if old_cols and "origin" in old_cols:
                runtime_rows = old.execute(
                    f"SELECT {','.join(old_cols)} FROM segments WHERE origin='runtime_ui'"
                ).fetchall()
            if runtime_rows:
                placeholders = ",".join("?" for _ in old_cols)
                new.executemany(
                    f"INSERT OR IGNORE INTO segments({','.join(old_cols)}) VALUES({placeholders})", runtime_rows
                )

            # Retain context-free phone translations as review history when a
            # fresh audit replaces catalogue rows with ordered message rows.
            phone_sources = {r[0] for r in new.execute("SELECT source_text FROM segments WHERE origin='interactive'")}
            if phone_sources and "channel" in old_cols:
                for values in old.execute(f"SELECT {','.join(old_cols)} FROM segments WHERE kind='localized_string'"):
                    legacy = dict(zip(old_cols, values))
                    if legacy.get("source_text") not in phone_sources:
                        continue
                    legacy["channel"] = "interactive_legacy"
                    new.execute(f"INSERT OR IGNORE INTO segments({','.join(old_cols)}) VALUES({','.join('?' for _ in old_cols)})", [legacy[c] for c in old_cols])
                for source in phone_sources:
                    approved = old.execute("SELECT translation,provider,model FROM segments WHERE source_text=? AND status='approved' AND translation<>''", (source,)).fetchall()
                    if approved and len({r[0] for r in approved}) == 1:
                        new.execute("UPDATE segments SET translation=?,provider=?,model=?,status='approved' WHERE origin='interactive' AND source_text=? AND translation=''", (*approved[0], source))

            # Preserve provider cache/run diagnostics when they exist. A fresh audit DB
            # intentionally does not contain these Stage 6 tables yet.
            for table in ("translation_cache", "translation_runs", "translation_attempts"):
                schema = old.execute(
                    "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
                ).fetchone()
                if not schema or not schema[0]:
                    continue
                new.execute(schema[0])
                cols = [r[1] for r in old.execute(f"PRAGMA table_info({table})")]
                if not cols:
                    continue
                rows = old.execute(f"SELECT {','.join(cols)} FROM {table}").fetchall()
                if rows:
                    placeholders = ",".join("?" for _ in cols)
                    new.executemany(
                        f"INSERT OR IGNORE INTO {table}({','.join(cols)}) VALUES({placeholders})", rows
                    )
            new.commit()
        finally:
            old.close()
            new.close()
        tmp.replace(WORK_DB)
        print(f"[OK] Рабочая база синхронизирована с новым аудитом; сохранено переводов: {carried}.")
        if changed_source:
            print(f"[SAFE] Исходник изменился у {changed_source} сегмент(а/ов): старый перевод НЕ перенесён.")
        return True
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"[ERROR] Не удалось синхронизировать рабочую базу: {type(exc).__name__}: {exc}")
        return False


def ensure_work() -> bool:
    _activate_project_workspace()
    if not REPORT_DB.exists() and WORK_DB.exists():
        # A portable/restored working database may have no original audit folder.
        # Static phone extraction must still run, without requiring game capture.
        from rpts.project_upgrade import upgrade_project
        upgrade_project(WORK_DB, REPORTS)
        return require_work()
    if not REPORT_DB.exists():
        print("[ERROR] reports\\translation_project.sqlite не найден. Сначала выполни аудит игры.")
        return False
    if not _latest_audit_complete():
        return False
    from rpts.project_upgrade import upgrade_project
    upgrade_project(REPORT_DB, REPORTS)
    if WORK_DB.exists():
        upgrade_project(WORK_DB, REPORTS)
    if not _sync_work_from_report():
        return False
    from rpts.alignment_recovery import recover_project_translations
    recover_project_translations(WORK_DB)
    return True


def require_work() -> bool:
    _activate_project_workspace()
    if not WORK_DB.exists():
        print("[ERROR] Рабочая база не найдена. Сначала подготовь рабочий проект.")
        return False
    from rpts.alignment_recovery import recover_project_translations
    recover_project_translations(WORK_DB)
    return True


def _ensure_ui_overrides() -> Path:
    if not UI_OVERRIDES.exists():
        if UI_OVERRIDES_EXAMPLE.exists():
            try:
                data = json.loads(UI_OVERRIDES_EXAMPLE.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    data["overrides"] = {}
                UI_OVERRIDES.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception:
                UI_OVERRIDES.write_text('{"overrides": {}}\n', encoding="utf-8")
        else:
            UI_OVERRIDES.write_text('{"overrides": {}}\n', encoding="utf-8")
        print("[OK] Создан ui_overrides.json для ручных UI-исправлений.")
    return UI_OVERRIDES


def _game_dir_from_work() -> Path | None:
    if not WORK_DB.exists():
        return None
    db = sqlite3.connect(WORK_DB)
    try:
        row = db.execute("SELECT value FROM metadata WHERE key='game_dir'").fetchone()
    finally:
        db.close()
    if not row:
        return None
    try:
        return Path(json.loads(row[0]))
    except Exception:
        return Path(str(row[0]))


def ui_coverage() -> int:
    if not require_work():
        return 1
    _ensure_ui_overrides()
    UI_WORK.mkdir(parents=True, exist_ok=True)
    return _run("tools/ui_project.py", "coverage", WORK_DB, "--output", UI_WORK, "--overrides", UI_OVERRIDES)


def ui_translate_missing() -> int:
    return _local_ai_translate_cycle(install_and_launch=False, kinds=UI_KINDS)


def ui_google_translate_missing() -> int:
    if not ensure_work():
        return 1
    _ensure_free_google_config()
    print("[UI] Перевожу недостающий интерфейс и сообщения интерактивных панелей.")
    return _run(
        "tools/translate_project.py", WORK_DB, "--config", FREE_USER_CFG,
        "--kinds", UI_KINDS, "--include-interactive", "--channels", "semantic,localization"
    )


def ui_edit_overrides() -> int:
    if not require_work():
        return 1
    path = _ensure_ui_overrides()
    if os.name == "nt":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        print(path)
    return 0


def ui_enable_capture() -> int:
    if not require_work():
        return 1
    rc = _run("tools/ui_project.py", "enable-capture", WORK_DB)
    if rc == 0:
        print("[NEXT] Запусти игру, открой проблемные меню/настройки, затем вернись и выбери импорт runtime UI.")
    return rc


def ui_import_runtime() -> int:
    if not require_work():
        return 1
    game = _game_dir_from_work()
    if not game:
        print("[ERROR] Не удалось определить папку game из project DB.")
        return 1
    log = game / "rpts_runtime_ui.tsv"
    if not log.exists():
        print(f"[ERROR] Runtime UI log ещё не создан: {log}")
        print("Сначала включи capture, запусти игру и открой проблемное меню.")
        return 1
    rc = _run("tools/ui_project.py", "import-runtime", WORK_DB, log)
    if rc == 0:
        print("[NEXT] Теперь выбери 'Перевести только недостающий UI', затем пересобери патч.")
    return rc


def ui_disable_capture() -> int:
    if not require_work():
        return 1
    return _run("tools/ui_project.py", "disable-capture", WORK_DB)


def google_cloud_key() -> bool:
    if os.environ.get("RPTS_GOOGLE_API_KEY"):
        return True
    print("\nНужен API key Google Cloud Translation Basic v2.")
    print("Ключ используется только в текущем процессе RPTS и не сохраняется в файлы.")
    try:
        value = getpass.getpass("Google API key: ").strip()
    except (EOFError, KeyboardInterrupt):
        value = ""
    if not value:
        print("[ERROR] API key не введён.")
        return False
    os.environ["RPTS_GOOGLE_API_KEY"] = value
    return True


def audit(target: str | None = None) -> int:
    if not target:
        print("\nМожно также перетащить папку игры или EXE прямо на RPTS.bat.")
        target = _input("Путь к игре: ").strip().strip('"')
    if not target:
        return 0

    identity = WORKSPACE.add_game(target)
    preferences = read_json(SETTINGS / "desktop.json")
    preferences["active_project"] = identity
    write_json(SETTINGS / "desktop.json", preferences)
    _activate_project_workspace()
    unrpyc_root = APP / "vendor" / "unrpyc"
    unrpyc_ready = (unrpyc_root / "unrpyc.py").is_file() and (unrpyc_root / "decompiler").is_dir()
    if not unrpyc_ready:
        print("\n[INFO] unrpyc не найден. Пытаюсь установить его автоматически для полного аудита...")
        if install_unrpyc() != 0:
            print("[WARN] unrpyc установить не удалось. Продолжаю безопасным native backend.")
            print("[WARN] Native mode может пропустить часть Screen Language/UI текста.")
        else:
            unrpyc_ready = True
    print(f"[BACKEND] {'unrpyc + AST supplement' if unrpyc_ready else 'native compatibility mode'}")

    print(f"\n[AUDIT] {target}")
    rc = _run("tools/audit_game.py", target, "--backend", "auto", "--prefer-compiled", "--reports", REPORTS)
    if rc:
        print("\n[ERROR] Аудит завершился с ошибкой.")
    else:
        _activate_project_workspace()
        print("\n[OK] reports\\translation_project.sqlite создан.")
        print(f"[PROJECT] {ACTIVE_PROJECT.relative_to(ROOT) if ACTIVE_PROJECT != ROOT else ACTIVE_PROJECT}")
        print("[OK] Игра выбрана. Перевод запускается через пункт 1 главного меню.")
    return rc


def google_free_dry() -> int:
    if not ensure_work():
        return 1
    _ensure_free_google_config()
    return _run("tools/translate_project.py", WORK_DB, "--config", FREE_USER_CFG, "--dry-run")


def reset_mock(quiet: bool = False) -> int:
    if not require_work():
        return 1
    return _run("tools/translate_project.py", WORK_DB, "--config", MOCK_CFG, "--reset-mock", quiet=quiet)


def google_free_translate_only() -> int:
    if not ensure_work():
        return 1
    _ensure_free_google_config()
    reset_mock(quiet=True)
    return _run("tools/translate_project.py", WORK_DB, "--config", FREE_USER_CFG)


def google_free_full() -> int:
    _clear()
    print("=" * 64)
    print("  GOOGLE TRANSLATE БЕСПЛАТНО - ПОЛНЫЙ ЦИКЛ")
    print("=" * 64)
    print("\n[INFO] Используется неофициальный consumer endpoint Google Translate.")
    print("API key, Google Cloud и биллинг НЕ нужны.")
    print("Строки отправляются пакетами, чтобы не делать тысячи запросов. При лимите прогресс сохранится.")
    if not ensure_work():
        return 1
    _ensure_free_google_config()
    print("\n[PLAN] Показываю объём. Интернет-запросы ещё не выполняются.")
    rc = _run("tools/translate_project.py", WORK_DB, "--config", FREE_USER_CFG, "--dry-run")
    if rc:
        return rc
    confirm = _input("\nНапечатай YES, чтобы начать бесплатный Google Translate: ").strip()
    if confirm.upper() != "YES":
        print("Отменено.")
        return 0

    print("\n[1/4] Удаляю только тестовые mock-переводы, если они были...")
    reset_mock(quiet=True)

    print("[2/4] Перевожу pending/error через бесплатный Google Translate...")
    rc = _run("tools/translate_project.py", WORK_DB, "--config", FREE_USER_CFG)
    if rc:
        print("\n[ERROR] Перевод остановлен. Патч пока не установлен.")
        print("Уже сохранённые строки НЕ потеряны. Повтори пункт позже — перевод продолжится.")
        return rc

    print("\n[3/4] Собираю и устанавливаю tl/russian патч...")
    rc = _run("tools/patch_project.py", "build-install", WORK_DB, "--output", WORK / "patch", "--language", "russian", "--overrides", _ensure_ui_overrides())
    if rc:
        print("[ERROR] Не удалось установить патч. Переводы в SQLite сохранены.")
        return rc

    print("\n[4/4] Запускаю игру...")
    rc = _run("tools/patch_project.py", "launch", "--database", WORK_DB)
    if rc:
        print("[WARN] Патч установлен, но EXE не удалось определить автоматически. Запусти игру вручную.")
        return 0
    print("\n[OK] Бесплатный Google-перевод установлен как отдельный Ren'Py tl/russian патч.")
    print("Оригинальные .rpa/.rpyc не изменялись.")
    return 0


def google_cloud_translate_only() -> int:
    if not ensure_work() or not google_cloud_key():
        return 1
    reset_mock(quiet=True)
    return _run("tools/translate_project.py", WORK_DB, "--config", CLOUD_GOOGLE_CFG)

def translation_status() -> int:
    if not require_work():
        return 1
    return _run("tools/translate_project.py", WORK_DB, "--config", MOCK_CFG, "--status")


def mock_test() -> int:
    if not ensure_work():
        return 1
    return _run("tools/translate_project.py", WORK_DB, "--config", MOCK_CFG, "--limit", "12", "--allow-mock")


def prepare_work() -> int:
    if not ensure_work():
        return 1
    print(f"\n[OK] Рабочая база: {WORK_DB}")
    return 0


def edit_config() -> int:
    _ensure_free_google_config()
    if os.name == "nt":
        subprocess.Popen(["notepad.exe", str(FREE_USER_CFG)])
    else:
        print(FREE_USER_CFG)
    return 0


def build_install() -> int:
    if not require_work():
        return 1
    return _run("tools/patch_project.py", "build-install", WORK_DB, "--output", WORK / "patch", "--language", "russian", "--overrides", _ensure_ui_overrides())


def patch_status() -> int:
    if not require_work():
        return 1
    return _run("tools/patch_project.py", "status", "--database", WORK_DB)


def uninstall_patch() -> int:
    if not require_work():
        return 1
    print("\nБудут удалены только файлы, принадлежащие RPTS manifest. Оригинальные файлы не трогаются.")
    if _input("Напечатай YES для удаления RPTS-патча: ").strip().upper() != "YES":
        print("Отменено.")
        return 0
    return _run("tools/patch_project.py", "uninstall", "--database", WORK_DB)


def launch_game() -> int:
    if not require_work():
        return 1
    return _run("tools/patch_project.py", "launch", "--database", WORK_DB)


def check_project() -> int:
    db = WORK_DB if WORK_DB.exists() else REPORT_DB
    if not db.exists():
        print("[ERROR] Project DB не найден. Сначала выполни аудит.")
        return 1
    return _run("tools/project_summary.py", db)


def pack_report() -> int:
    from rpts.diagnostic_report import build_report
    print("\nСобираю отчёт: ошибки, запросы ИИ, настройки и состояние перевода...")
    diagnostics.event("report_start", project=str(ACTIVE_PROJECT))
    try:
        game = _game_dir_from_work() if WORK_DB.is_file() else None
    except (OSError, sqlite3.Error, ValueError):
        game = None
    try:
        out, manifest = build_report(root=ROOT, app=APP, reports=REPORTS, work=WORK, database=WORK_DB,
            quality=LOCAL_AI_QUALITY, server_log=LOCAL_AI_LOG, config=LOCAL_AI_CFG, settings=LOCAL_AI_SETTINGS,
            overrides=UI_OVERRIDES, game=game)
    except (OSError, sqlite3.Error, ValueError) as exc:
        diagnostics.exception("report_error")
        print(f"[ERROR] Не удалось упаковать отчёт: {type(exc).__name__}: {exc}")
        return 1
    print(f"[OK] {out}")
    print(f"Отправь этот ZIP целиком. Добавлено файлов: {len(manifest['included'])}.")
    if manifest['warnings']:
        print(f"[WARN] Часть данных недоступна: {len(manifest['warnings'])}. Причины указаны внутри архива.")
    diagnostics.event("report_complete", path=str(out), warnings=manifest['warnings'])
    return 0


def runtime_log(log: str | None = None) -> int:
    if not log:
        log = _input("Путь к rpts_runtime_strings.tsv: ").strip().strip('"')
    if not log:
        return 0
    p = Path(log)
    if not p.exists():
        print(f"[ERROR] Файл не найден: {p}")
        return 1
    if not (REPORTS / "runtime_known_inventory.csv").exists():
        print("[ERROR] Сначала выполни аудит игры.")
        return 1
    return _run("tools/analyze_runtime_log.py", p, "--reports", REPORTS)


def install_unrpyc() -> int:
    vendor = APP / "vendor"
    target = vendor / "unrpyc"
    tmp_zip = WORK / "unrpyc-master.zip"
    tmp_dir = WORK / "unrpyc-extract"
    WORK.mkdir(parents=True, exist_ok=True)
    vendor.mkdir(parents=True, exist_ok=True)
    print("Загружаю официальный CensoredUsername/unrpyc...")
    try:
        urllib.request.urlretrieve(
            "https://github.com/CensoredUsername/unrpyc/archive/refs/heads/master.zip",
            tmp_zip,
        )
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)
        with zipfile.ZipFile(tmp_zip) as zf:
            zf.extractall(tmp_dir)
        src = tmp_dir / "unrpyc-master"
        if target.exists():
            shutil.rmtree(target)
        shutil.move(str(src), str(target))
        print(f"[OK] {target}")
        return 0
    except Exception as exc:
        print(f"[ERROR] Не удалось установить unrpyc: {type(exc).__name__}: {exc}")
        return 1
    finally:
        try:
            tmp_zip.unlink(missing_ok=True)
        except Exception:
            pass
        try:
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir)
        except Exception:
            pass


def open_folders() -> int:
    if REPORTS.exists():
        _open(REPORTS)
    if WORK.exists():
        _open(WORK)
    return 0



def _local_ai_hw_report() -> tuple[object, object]:
    hw = detect_hardware()
    rec = recommend_model(hw)
    print("\nОборудование:")
    print(f"  CPU: {hw.cpu} | потоков: {hw.logical_cores}")
    print(f"  RAM: {human_bytes(hw.ram_bytes)}")
    if hw.gpus:
        for gpu in hw.gpus:
            v = human_bytes(gpu.vram_bytes) if gpu.vram_bytes else "VRAM неизвестна"
            free = f" | свободно {human_bytes(gpu.free_vram_bytes)}" if getattr(gpu, "free_vram_bytes", 0) else ""
            print(f"  GPU: {gpu.name} | {v}{free} | {gpu.vendor}")
    else:
        print("  GPU: не обнаружена -> CPU режим")
    print(f"  Рекомендуемый backend: {recommended_backend(hw).upper()}")
    print(f"  Рекомендуемая модель: {rec.title}")
    print(f"  Размер модели: {human_bytes(rec.size_bytes)}")
    est = model_runtime_estimate(rec, hw)
    print(f"  Режим: {est['mode']}")
    return hw, rec


def _best_local_ai_home(spec, current: str | None = None) -> Path:
    if current:
        return Path(current).expanduser()
    preferred = WORKSPACE.ai
    try:
        if disk_info(preferred)["free"] >= required_free_bytes(spec):
            return preferred
    except Exception:
        pass
    choices = enumerate_storage_locations()
    if choices:
        best = max(choices, key=lambda x: int(x.get("free") or 0))
        if os.name == "nt":
            return Path(str(best["path"])) / "RPTS_LocalAI"
    return preferred


def _choose_local_model(hw, recommended) -> object | None:
    print("\nМодели:")
    keys = ["qwen3-1.7b", "qwen3-4b", "qwen3-8b", "qwen3-14b", "deepseek-r1-7b"]
    for i, key in enumerate(keys, 1):
        spec = MODEL_SPECS[key]
        est = model_runtime_estimate(spec, hw)
        mark = " [РЕКОМЕНДУЕТСЯ]" if spec.key == recommended.key else ""
        fit = "OK" if est["comfortable"] else "тяжело для этого ПК"
        print(f"  {i}. {spec.title} - {human_bytes(spec.size_bytes)} - {fit}{mark}")
        print(f"     {spec.quality}")
    raw = _input(f"\nВыбор модели [Enter = {recommended.key}]: ").strip()
    if not raw:
        return recommended
    if not raw.isdigit() or not (1 <= int(raw) <= len(keys)):
        print("[ERROR] Неверный выбор модели.")
        return None
    return MODEL_SPECS[keys[int(raw)-1]]


def _choose_local_backend(hw) -> str | None:
    rec = recommended_backend(hw)
    print("\nBackend llama.cpp:")
    print("  1. AUTO / рекомендуемый")
    print("  2. CUDA (NVIDIA)")
    print("  3. Vulkan (AMD / Intel / NVIDIA)")
    print("  4. CPU (без GPU)")
    raw = _input(f"Выбор [Enter = {rec.upper()}]: ").strip()
    if not raw or raw == "1": return rec
    return {"2":"cuda", "3":"vulkan", "4":"cpu"}.get(raw)


def local_ai_setup(*, simple: bool = False) -> int:
    _clear()
    print("=" * 64)
    print("  LOCAL AI - УСТАНОВКА / ПОДБОР МОДЕЛИ")
    print("=" * 64)
    if simple:
        hw = detect_hardware()
        rec = recommend_model(hw)
    else:
        hw, rec = _local_ai_hw_report()
    spec = rec if simple else _choose_local_model(hw, rec)
    if not spec:
        return 1
    backend = recommended_backend(hw) if simple else _choose_local_backend(hw)
    if not backend:
        print("[ERROR] Неверный backend.")
        return 1

    existing = _load_local_ai_settings()
    default_home = _best_local_ai_home(spec, existing.get("home"))
    print("\nХранилище Local AI:")
    print(f"  По умолчанию: {default_home}")
    if not simple:
        for row in enumerate_storage_locations():
            print(f"  {row['path']} свободно {human_bytes(int(row['free']))}")
    raw = "" if simple else _input("Путь хранения [Enter = по умолчанию]: ").strip().strip('"')
    home = Path(raw).expanduser() if raw else default_home
    free = disk_info(home)["free"]
    engine_present = installation_status({"home":str(home),"model_key":spec.key,"backend":backend}).get("engine_present", False)
    need = required_free_bytes(spec, engine_installed=bool(engine_present))
    diagnostics.event("model_setup_plan", model=spec.key, backend=backend, home=str(home),
                      free_bytes=free, required_bytes=need, hardware=asdict(hw))
    print(f"\nМодель: {spec.title}")
    print(f"Движок: {backend.upper()}")
    print(f"Размер GGUF: {human_bytes(spec.size_bytes)}")
    print(f"Нужно свободного места с запасом: {human_bytes(need)}")
    print(f"Сейчас свободно: {human_bytes(free)}")
    print(f"После установки останется не меньше: {human_bytes(max(0, free - need))}")
    if free < need:
        print("[ERROR] На выбранном диске недостаточно свободного места.")
        return 1
    if not model_runtime_estimate(spec, hw)["comfortable"]:
        print("[WARN] Эта модель выше комфортной рекомендации для данного ПК и может работать очень медленно/через RAM.")
    if simple:
        print("\nЭто первая установка: потребуется интернет. Затем перевод работает локально.")
        choice = _input("Enter — скачать и продолжить; Н — выбрать настройки; 0 — отмена: ").strip().lower()
        if choice in {"н", "n"}:
            return local_ai_setup()
        confirmed = not choice
    else:
        confirmed = _input("\nНапечатай YES для установки: ").strip().upper() == "YES"
    if not confirmed:
        print("Отменено.")
        return 0

    capacity = recommended_parallel_capacity(hw, spec, backend)
    settings = {
        "home": str(home.resolve()), "model_key": spec.key, "backend": backend, "port": 8091,
        "parallel_capacity": capacity, "runtime_context_size": recommended_runtime_context(spec, capacity),
        "runtime_context_per_slot": recommended_context_per_slot(spec, capacity),
    }
    try:
        status = installation_status(settings)
        if not status.get("engine_present") or spec.key in {"hy-mt2-7b-q6", "hy-mt2-7b-q4", "qwen3.5-9b", "translategemma-12b"}:
            print(f"\n[1/2] Скачиваю официальный llama.cpp ({backend})...")
            result = install_engine(home, backend)
            print(f"[OK] llama.cpp {result.get('release')} установлен.")
        else:
            print("\n[1/2] llama.cpp уже установлен.")
        print(f"[2/2] Проверяю модель {spec.title}...")
        mp = install_model(home, spec)
        print(f"[OK] Модель: {mp}")
        save_settings(LOCAL_AI_SETTINGS, settings)
        LOCAL_AI_CFG.parent.mkdir(parents=True, exist_ok=True)
        cfg = make_translation_config(spec, port=8091, hw=hw, backend=backend)
        # Model policy controls whether optional free editing is appropriate.
        if LOCAL_AI_CFG.exists():
            old = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
            for project_key in ("glossary", "notes"):
                if project_key in old:
                    cfg[project_key] = old[project_key]
            for key in ("text_mode", "editorial_pass", "fidelity_pass", "combined_review"):
                if key in old.get("quality", {}): cfg["quality"][key] = old["quality"][key]
            for key in ("character_profiles_file", "quality_dir", "auto_profile_characters", "stop_on_profile_error"):
                if key in old.get("quality", {}): cfg["quality"][key] = old["quality"][key]
        else:
            _apply_text_mode(cfg, _new_text_mode())
        LOCAL_AI_CFG.write_text(
            json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print("\n[OK] Local AI готов. Интернет после установки модели для перевода больше не нужен.")
        return 0
    except Exception as exc:
        diagnostics.exception("model_setup_error")
        print(f"[ERROR] Установка Local AI не завершена: {type(exc).__name__}: {exc}")
        return 1


def local_ai_status() -> int:
    _clear(); print("="*64); print("  LOCAL AI - СТАТУС"); print("="*64)
    hw, rec = _local_ai_hw_report()
    settings = _load_local_ai_settings()
    status = installation_status(settings)
    if not status.get("configured"):
        print("\nLocal AI ещё не настроен.")
        return 0
    spec = MODEL_SPECS[str(settings["model_key"])]
    print(f"\nХранилище: {settings['home']}")
    print(f"Модель: {spec.title} ({human_bytes(spec.size_bytes)})")
    print(f"Backend: {settings['backend'].upper()}")
    print(f"Engine: {'OK' if status.get('engine_present') else 'НЕТ'}")
    print(f"Model: {'OK' if status.get('model_present') else 'НЕТ'}")
    print(f"Готовность: {'READY' if status.get('ready') else 'NOT READY'}")
    try:
        free=disk_info(settings['home'])['free']; print(f"Свободно на диске: {human_bytes(free)}")
    except Exception: pass
    return 0


def _load_local_ai_settings() -> dict:
    """Reuse installed files when only local_ai was carried into a clean release."""
    try:
        settings = load_settings(LOCAL_AI_SETTINGS)
    except (ValueError, OSError):
        settings = {}
    if isinstance(settings, dict) and installation_status(settings).get("ready"):
        return settings
    # The old root file identifies custom storage locations; no other old
    # translation parameters are needed for a clean installation.
    try:
        previous = load_settings(ROOT / "local_ai_settings.json")
    except (ValueError, OSError):
        previous = {}
    if isinstance(previous, dict) and installation_status(previous).get("ready"):
        save_settings(LOCAL_AI_SETTINGS, previous)
        print("[OK] Подключены уже установленные модель и движок.")
        return previous
    home = WORKSPACE.ai
    if not home.is_dir():
        return settings if isinstance(settings, dict) else {}
    hw = detect_hardware()
    preferred_model = recommend_model(hw)
    preferred_backend = recommended_backend(hw)
    specs = sorted(MODEL_SPECS.values(), key=lambda s: (s.key != preferred_model.key, -s.size_bytes))
    backends = [preferred_backend]
    if preferred_backend == "cuda":
        backends.append("vulkan")
    if "cpu" not in backends:
        backends.append("cpu")
    for spec in specs:
        model = home / "models" / spec.filename
        if not model.is_file() or model.stat().st_size < int(spec.size_bytes * 0.95):
            continue
        for backend in backends:
            recovered = {"home": str(home.resolve()), "model_key": spec.key, "backend": backend}
            if installation_status(recovered).get("ready"):
                save_settings(LOCAL_AI_SETTINGS, recovered)
                print(f"[OK] Найдена установленная модель: {spec.title}; движок: {backend.upper()}.")
                return recovered
    return settings if isinstance(settings, dict) else {}


def _ensure_local_ai_ready(*, simple: bool = False) -> dict | None:
    settings = _load_local_ai_settings()
    if not installation_status(settings).get("ready"):
        print("[INFO] Local AI ещё не установлен. Запускаю мастер настройки.")
        if local_ai_setup(simple=simple) != 0:
            return None
        settings = load_settings(LOCAL_AI_SETTINGS)
        if not installation_status(settings).get("ready"):
            return None
    spec = MODEL_SPECS.get(str(settings.get("model_key")))
    if not spec:
        return None
    hw = detect_hardware()
    backend = str(settings.get("backend") or recommended_backend(hw))
    settings["backend"] = backend
    capacity = recommended_parallel_capacity(hw, spec, backend)
    settings["parallel_capacity"] = capacity
    settings["runtime_context_size"] = recommended_runtime_context(spec, capacity)
    settings["runtime_context_per_slot"] = recommended_context_per_slot(spec, capacity)
    save_settings(LOCAL_AI_SETTINGS, settings)
    desired = make_translation_config(
        spec, port=int(settings.get('port') or 8091), hw=hw, backend=backend
    )
    desired.setdefault("quality", {})["character_profiles_file"] = str(LOCAL_AI_QUALITY / "character_profiles.json")
    desired["quality"]["quality_dir"] = str(LOCAL_AI_QUALITY)
    bench_key = f"11.18:{settings.get('model_key')}:{backend}:{capacity}"
    if settings.get("performance_benchmark_key") == bench_key and settings.get("performance_workers"):
        chosen = max(1, min(capacity, int(settings.get("performance_workers") or 1)))
        desired["translation"]["concurrent_batches"] = chosen
        desired["performance"]["chosen_workers"] = chosen
        desired["performance"]["benchmark"] = settings.get("performance_benchmark") or {}
    migrated = False
    if LOCAL_AI_CFG.exists():
        try:
            config_text = LOCAL_AI_CFG.read_text(encoding="utf-8")
            current = json.loads(config_text)
            if not isinstance(current, dict):
                current = desired
            provider = current.setdefault("provider", {})
            translation = current.setdefault("translation", {})
            quality = current.setdefault("quality", {})
            performance = current.setdefault("performance", {})
            model_changed = provider.get("model_identity") != desired["provider"]["model_identity"]
            upgrading = performance.get("profile_version") != "11.18"
            protocol_upgrade = quality.get("translation_protocol") != desired["quality"]["translation_protocol"]
            profiling_upgrade = quality.get("profile_protocol") != desired["quality"]["profile_protocol"]
            # Runtime transport follows the selected model. Editorial settings
            # remain editable after the one-time migration.
            for key in (
                "type","model","model_identity","base_url","endpoint","api_key_env",
                "context_limit_tokens","enable_thinking","cache_prompt",
            ):
                if provider.get(key) != desired["provider"].get(key):
                    provider[key] = desired["provider"].get(key); migrated = True
            for key, value in desired["provider"].items():
                if key not in provider or upgrading and key in {"structured_json", "compact_json", "token_preflight"}:
                    provider[key] = value; migrated = True
            for key in ("split_on_response_error","continue_on_single_response_error","no_reasoning","concurrent_batches"):
                if translation.get(key) != desired["translation"].get(key):
                    translation[key] = desired["translation"].get(key); migrated = True
            for key, value in desired.get("quality", {}).items():
                migrate_profile_default = profiling_upgrade and key == "profile_output_tokens" and quality.get(key) == 1400
                if (key not in quality or key in {"character_profiles_file", "quality_dir"}
                    or upgrading and key in {"auto_profile_characters", "enabled"}
                    or profiling_upgrade and key == "profile_protocol" or migrate_profile_default):
                    quality[key] = value; migrated = True
            if model_changed:
                for key in ("temperature", "top_p", "top_k", "min_p", "repeat_penalty", "presence_penalty", "max_output_tokens", "output_token_factor", "output_token_min", "constrain_russian", "response_protocol", "compact_json", "structured_json"):
                    provider[key] = desired["provider"].get(key)
                for key in ("profile_max_chars", "profile_output_tokens", "scene_context_max_chars"):
                    quality[key] = desired["quality"][key]
                quality["native_free_review"] = False
                current["batch"] = desired["batch"]
                current["context"] = desired["context"]
                performance.update(desired["performance"])
                migrated = True
            if protocol_upgrade:
                if current.get("notes") == LEGACY_LOCAL_NOTES:
                    current["notes"] = desired["notes"]
                # Migrate only the previous built-in sampling default. Preserve
                # deliberate user tuning and the chosen editorial mode.
                if provider.get("temperature") == 0.12:
                    provider["temperature"] = desired["provider"]["temperature"]
                if spec.thinking:
                    # Upgrade only the former built-in Qwen settings accidentally
                    # used for R1. Keep deliberate non-default sampling values.
                    for key, old_default in (("temperature", 0.2), ("top_p", 0.8), ("top_k", 20)):
                        if provider.get(key) == old_default:
                            provider[key] = desired["provider"][key]
                current["batch"] = desired["batch"]
                current["context"] = desired["context"]
                for key in ("compact_json", "structured_json", "response_protocol", "max_output_tokens", "output_token_factor", "output_token_min", "constrain_russian"):
                    provider[key] = desired["provider"][key]
                for key in ("translation_protocol", "scene_context_max_chars"):
                    quality[key] = desired["quality"][key]
                translation["refresh_legacy_ai"] = True
                translation["skip_nonlinguistic"] = True
                migrated = True
            if upgrading:
                performance.update(desired.get("performance", {}))
                migrated = True
            for key in ("target_language", "target_language_code", "renpy_language"):
                if current.get(key) != desired[key]:
                    current[key] = desired[key]; migrated = True
            cfg = current
        except Exception:
            diagnostics.exception("config_recovery", config_file=str(LOCAL_AI_CFG), previous_text=locals().get("config_text", ""))
            cfg = desired; migrated = True
    else:
        cfg = desired; migrated = True
        _apply_text_mode(cfg, _new_text_mode())
    if migrated:
        LOCAL_AI_CFG.parent.mkdir(parents=True, exist_ok=True)
        LOCAL_AI_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("[OK] Настройки локального перевода готовы.")
    return settings


def _local_ai_translate_cycle(*, install_and_launch: bool = True, kinds: str | None = None, simple: bool = False) -> int:
    if not ensure_work():
        return 1
    settings = _ensure_local_ai_ready(simple=simple)
    if not settings:
        return 1
    selection = ["--kinds", kinds] if kinds else []
    if kinds == UI_KINDS:
        selection.append("--include-interactive")
    cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
    quality = cfg.get("quality") or {}
    if uses_source_only_review(cfg):
        print("[ТЕКСТ] Один перевод с контекстом, проверки и исправление обнаруженных ошибок.")
    elif quality.get("fidelity_pass"):
        print("[ТЕКСТ] Полная редактура: перевод -> русский редактор -> сверка с оригиналом.")
        print("[ТЕКСТ] Каждый сюжетный фрагмент проходит три этапа. Перевод займёт больше времени.")
        print("[ТЕКСТ] Автоматическая проверка не заменяет окончательную вычитку человеком.")
    elif quality.get("combined_review"):
        print("[ТЕКСТ] Качество и скорость: перевод -> совместная редактура и сверка смысла.")
        print("[ТЕКСТ] Два прохода для сюжета и сообщений. Промежуточные этапы сохраняются.")
    elif quality.get("editorial_pass"):
        print("[ТЕКСТ] Перевод и русская редактура всех сюжетных строк.")
    print("\nПодготавливаю перевод. Готовый прогресс будет учтён автоматически.")
    plan_output = []
    rc = _run("tools/translate_project.py", WORK_DB, "--config", LOCAL_AI_CFG, "--dry-run", *selection,
              quiet=simple, capture=plan_output)
    if rc:
        if simple:
            print("[ERROR] Не удалось подготовить перевод:")
            print(''.join(plan_output[-6:]).rstrip())
        return rc
    if simple:
        try:
            raw = ''.join(plan_output)
            plan = json.loads(raw[raw.index('{'):])
            print(f"К переводу: {plan.get('selected', 0)} строк; пакетов: {(plan.get('plan') or {}).get('estimated_batches', 0)}.")
        except (ValueError, TypeError):
            print("План сохранён в диагностике. Подробный статус доступен в инструментах.")
        print("После перевода программа установит русский патч и запустит игру.")
        print("Интернет к модели не используется. Для остановки — Ctrl+C; продолжение через пункт 1.")
        confirmed = not _input("\nEnter — начать; 0 — отмена: ").strip()
    else:
        confirmed = _input("\nНапечатай YES, чтобы запустить локальный перевод: ").strip().upper() == "YES"
    if not confirmed:
        print("Отменено."); return 0

    diagnostics.event("translation_start", project=str(ACTIVE_PROJECT), config=cfg, settings=settings, kinds=kinds,
                      hardware=asdict(detect_hardware()))

    proc = None
    try:
        print("\n[1/4] Запускаю локальный ИИ...")
        from rpts.local_ai import prepare_runtime_server
        runtime_settings = prepare_runtime_server(settings)
        proc = start_server(runtime_settings, LOCAL_AI_LOG, detect_hardware())
        port = int(runtime_settings["port"])
        cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
        cfg.setdefault("provider", {})["base_url"] = f"http://127.0.0.1:{port}/v1"
        cfg["provider"]["api_key_env"] = "RPTS_LOCAL_AI_KEY"
        LOCAL_AI_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        settings = runtime_settings
        print(f"[OK] Local AI server готов на защищённом localhost:{port}.")

        bench_key = f"11.18:{settings.get('model_key')}:{settings.get('backend')}:{settings.get('parallel_capacity', 1)}"
        if settings.get("performance_benchmark_key") != bench_key:
            try:
                capacity = int(settings.get("parallel_capacity") or 1)
                if capacity > 1:
                    print(f"[PERF] Одноразовый benchmark: 1..{capacity} параллельных batch...")
                    bench = benchmark_parallel_workers(settings)
                    chosen = max(1, min(capacity, int(bench.get("workers") or 1)))
                    settings["performance_workers"] = chosen
                    settings["performance_benchmark"] = bench
                    settings["performance_benchmark_key"] = bench_key
                    save_settings(LOCAL_AI_SETTINGS, settings)
                    cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
                    cfg.setdefault("translation", {})["concurrent_batches"] = chosen
                    cfg.setdefault("performance", {})["chosen_workers"] = chosen
                    cfg["performance"]["benchmark"] = bench
                    LOCAL_AI_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                    details = ", ".join(
                        f"{k}w={v.get('seconds', 0):.2f}s" for k, v in (bench.get("results") or {}).items()
                    )
                    print(f"[PERF] Выбрано workers={chosen}. {details}")
            except Exception as exc:
                print(f"[WARN] Benchmark не удался ({type(exc).__name__}: {exc}); использую безопасный профиль.")
        elif settings.get("performance_workers"):
            chosen = max(1, min(int(settings.get("parallel_capacity") or 1), int(settings.get("performance_workers") or 1)))
            try:
                cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
                if int(cfg.setdefault("translation", {}).get("concurrent_batches") or 1) != chosen:
                    cfg["translation"]["concurrent_batches"] = chosen
                    LOCAL_AI_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except Exception:
                pass

        reset_mock(quiet=True)
        print("[2/4] Перевожу оставшиеся строки и повторяю строки с ошибками...")
        rc = _run("tools/translate_project.py", WORK_DB, "--config", LOCAL_AI_CFG, *selection)
        if rc:
            print(f"[ERROR] Локальный перевод остановлен. Лог сервера: {LOCAL_AI_LOG}")
            return rc
        if not install_and_launch:
            return 0
        print("[3/4] Собираю и устанавливаю русский перевод...")
        rc = build_install()
        if rc: return rc
        print("[4/4] Запускаю игру...")
        rc = launch_game()
        return 0 if rc == 0 else rc
    except Exception as exc:
        diagnostics.exception("translation_cycle_error")
        print(f"[ERROR] Local AI: {type(exc).__name__}: {exc}")
        print(f"Лог: {LOCAL_AI_LOG}")
        return 1
    finally:
        stop_server(proc)


def local_ai_full() -> int:
    _clear(); print("="*64); print("  LOCAL AI - ПЕРЕВЕСТИ -> УСТАНОВИТЬ -> ЗАПУСТИТЬ"); print("="*64)
    print("\nПолностью локально после первой загрузки llama.cpp и модели. API key не нужен.")
    return _local_ai_translate_cycle(install_and_launch=True)


def _set_local_ai_text_mode(full_review: bool, *, combined_review: bool = False) -> int:
    mode = "full_review" if full_review else "reviewed" if combined_review else "balanced"
    native = False
    SETTINGS.mkdir(parents=True, exist_ok=True)
    (SETTINGS / "menu_preferences.json").write_text(json.dumps({"text_mode": mode}, ensure_ascii=False) + "\n", encoding="utf-8")
    if LOCAL_AI_CFG.is_file():
        cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
        _apply_text_mode(cfg, mode)
        native = model_family(cfg.get("provider") or {}) == "hy_mt2"
        if native:
            cfg["quality"]["native_free_review"] = bool(full_review or combined_review)
        LOCAL_AI_CFG.write_text(json.dumps(cfg, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    label = "Полная редактура" if full_review else "Качество и скорость" if combined_review else "Обычный перевод"
    if native:
        label = label + " (эксперимент Hy-MT2)" if full_review or combined_review else "Перевод с контекстом, проверками и исправлением ошибок"
    print("[OK] Режим текста: " + label)
    return 0


def local_ai_full_review() -> int:
    if _set_local_ai_text_mode(True):
        return 1
    return _local_ai_translate_cycle(install_and_launch=True)


def local_ai_efficient_review() -> int:
    if _set_local_ai_text_mode(False, combined_review=True):
        return 1
    return _local_ai_translate_cycle(install_and_launch=True)


def local_ai_text_mode() -> int:
    settings = _load_local_ai_settings()
    if model_family({"model": settings.get("model_key")}) == "hy_mt2":
        print("\n  1. Перевод с контекстом, проверками и исправлением ошибок (рекомендуется)")
        print("  2. Перевод + свободная редактура (эксперимент)")
        print("  3. Перевод + редактура + сверка (эксперимент)")
        print("  В отчёте Hy-MT2 свободная редактура добавляла соседние реплики и меняла смысл.")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        if choice not in {"1", "2", "3"}:
            return 0
        return _set_local_ai_text_mode(choice == "3", combined_review=choice == "2")
    print("\n  1. Полная редактура: перевод, редактор, сверка с оригиналом (медленнее)")
    print("  2. Обычный перевод: адаптация за один проход (быстрее)")
    print("  3. Качество и скорость: перевод + редактура со сверкой (2 прохода)")
    print("  0. Назад")
    choice = _input("\nВыбор: ").strip()
    if choice not in {"1", "2", "3"}:
        return 0
    return _set_local_ai_text_mode(choice == "1", combined_review=choice == "3")


def local_ai_retranslate_all() -> int:
    if not ensure_work(): return 1
    print("\nЭто заменит уже готовый Google/другой перевод локальным AI для semantic/localization сегментов.")
    print("Перед изменением будет создан полный backup SQLite.")
    if _input("Напечатай RETRANSLATE: ").strip().upper() != "RETRANSLATE": return 0
    LOCAL_AI_BACKUPS.mkdir(parents=True, exist_ok=True)
    stamp = __import__('datetime').datetime.now().strftime('%Y%m%d-%H%M%S')
    backup = LOCAL_AI_BACKUPS / f"translation_project_before_local_{stamp}.sqlite"
    shutil.copy2(WORK_DB, backup)
    db=sqlite3.connect(WORK_DB)
    try:
        cur=db.execute("""
            UPDATE segments SET translation='',status='pending',provider='',model=''
            WHERE channel IN ('semantic','localization') AND COALESCE(translation,'')<>''
        """)
        count=int(cur.rowcount or 0); db.commit()
    finally: db.close()
    print(f"[OK] Backup: {backup}")
    print(f"[OK] На локальный переперевод отправлено: {count}")
    return _local_ai_translate_cycle(install_and_launch=True)


def local_ai_restore_backup() -> int:
    if not LOCAL_AI_BACKUPS.exists():
        print("[INFO] Backup'ов нет."); return 0
    files=sorted(LOCAL_AI_BACKUPS.glob('translation_project_before_local_*.sqlite'), reverse=True)
    if not files:
        print("[INFO] Backup'ов нет."); return 0
    latest=files[0]
    print(f"Последний backup: {latest}")
    if _input("Напечатай RESTORE: ").strip().upper() != "RESTORE": return 0
    shutil.copy2(latest, WORK_DB)
    print("[OK] Рабочая база восстановлена. Чтобы вернуть перевод в игру, пересобери патч.")
    return 0


def local_ai_remove() -> int:
    settings=load_settings(LOCAL_AI_SETTINGS)
    if not settings:
        print("[INFO] Local AI не настроен."); return 0
    home=settings.get('home')
    print(f"Будет удалён Local AI storage: {home}")
    print("Переводы SQLite и патч игры НЕ удаляются.")
    if _input("Напечатай DELETE: ").strip().upper() != "DELETE": return 0
    delete_local_ai(settings, remove_models=True)
    LOCAL_AI_SETTINGS.unlink(missing_ok=True); LOCAL_AI_CFG.unlink(missing_ok=True)
    print("[OK] Local AI удалён, место освобождено.")
    return 0


def local_ai_quality(*, rebuild: bool = False) -> int:
    if not ensure_work():
        return 1
    settings = _ensure_local_ai_ready()
    if not settings:
        return 1
    proc = None
    profile_cfg = LOCAL_AI_QUALITY / "profiling_config.json"
    try:
        from rpts.local_ai import prepare_runtime_server
        runtime = prepare_runtime_server(settings)
        print("[CHARACTERS] Запускаю ИИ для формирования полных профилей персонажей...")
        proc = start_server(runtime, LOCAL_AI_LOG, detect_hardware())
        cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
        cfg["provider"]["base_url"] = f"http://127.0.0.1:{runtime['port']}/v1"
        cfg["provider"]["api_key_env"] = "RPTS_LOCAL_AI_KEY"
        LOCAL_AI_QUALITY.mkdir(parents=True, exist_ok=True)
        profile_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        args = [WORK_DB, "--config", profile_cfg, "--profiles-only"]
        if rebuild: args.append("--rebuild-profiles")
        rc = _run("tools/translate_project.py", *args)
        print(f"[CHARACTERS] Профили/основания/ошибки: {LOCAL_AI_QUALITY}")
        print("locked=true закрепляет профиль; gender_locked/voice_locked и field_locks закрепляют отдельные настройки.")
        print("character_profiles_review.csv содержит пол, уверенность, основания, характер и обращения.")
        _open(LOCAL_AI_QUALITY)
        return rc
    except Exception as exc:
        print(f"[ERROR] Формирование персонажей: {type(exc).__name__}: {exc}")
        return 1
    finally:
        stop_server(proc)
        profile_cfg.unlink(missing_ok=True)


def local_ai_evaluate_russian() -> int:
    settings = _ensure_local_ai_ready()
    if not settings:
        return 1
    proc = None
    eval_cfg = LOCAL_AI_QUALITY / "evaluation_config.json"
    try:
        from rpts.local_ai import prepare_runtime_server
        runtime = prepare_runtime_server(settings)
        print("[RU EVAL] Запускаю локальную модель для коротких проверочных сцен...")
        proc = start_server(runtime, LOCAL_AI_LOG, detect_hardware())
        cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8"))
        cfg["provider"]["base_url"] = f"http://127.0.0.1:{runtime['port']}/v1"
        cfg["provider"]["api_key_env"] = "RPTS_LOCAL_AI_KEY"
        LOCAL_AI_QUALITY.mkdir(parents=True, exist_ok=True)
        eval_cfg.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        evaluation_out = LOCAL_AI_QUALITY / "russian_evaluation" / (str(settings["model_key"]) + "_" + time.strftime("%Y%m%d_%H%M%S"))
        rc = _run("tools/evaluate_russian.py", "--config", eval_cfg, "--out", evaluation_out)
        print("[RU EVAL] Проверь russian_evaluation.csv: смысл, естественность и голос оценивает человек.")
        _open(evaluation_out)
        return rc
    except Exception as exc:
        print(f"[ERROR] Проверка русского перевода: {type(exc).__name__}: {exc}")
        return 1
    finally:
        stop_server(proc)
        eval_cfg.unlink(missing_ok=True)


def local_ai_menu() -> None:
    while True:
        _clear(); print("="*64); print("  RPTS - МОДЕЛЬ ЛОКАЛЬНОГО ИИ"); print("="*64)
        print("\n  1. Установить / сменить модель, движок или диск")
        print("  2. Проверить оборудование и установленную модель")
        print("  3. Удалить модель и движок (освободить место)")
        print("  0. Назад")
        choice=_input("\nВыбор: ").strip()
        if choice=='0': return
        actions={'1':local_ai_setup,'2':local_ai_status,'3':local_ai_remove}
        fn=actions.get(choice)
        if fn: _perform(fn); _pause()


def edit_character_profiles() -> int:
    if not require_work():
        return 1
    path = LOCAL_AI_QUALITY / "character_profiles.json"
    if not path.is_file():
        print("[INFO] Сначала сформируй AI-профили персонажей — пункт 1 этого раздела.")
        return 1
    if os.name == "nt":
        subprocess.Popen(["notepad.exe", str(path)])
    else:
        print(path)
    print("[INFO] Для закрепления изменений используй locked, gender_locked, voice_locked или field_locks.")
    return 0


def character_menu() -> None:
    while True:
        _clear(); print("="*64); print("  RPTS - ПЕРСОНАЖИ И КАЧЕСТВО"); print("="*64)
        print("\n  1. Сформировать AI-профили персонажей / отчёт качества")
        print("  2. Открыть профили и отчёты")
        print("  3. Изменить настройки персонажей вручную")
        print("  4. Пересоздать AI-профили (ручные настройки сохраняются)")
        print("  5. Перевести готовые строки заново (с резервной копией)")
        print("  6. Восстановить последнюю резервную копию перевода")
        print("  7. Проверить русский перевод на коротких сценах")
        print("  8. Выбрать режим: обычный / качество и скорость / полная редактура")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        if choice == "0": return
        actions = {"1": local_ai_quality, "2": lambda: _open(LOCAL_AI_QUALITY), "3": edit_character_profiles,
                   "4": lambda: local_ai_quality(rebuild=True), "5": local_ai_retranslate_all,
                   "6": local_ai_restore_backup, "7": local_ai_evaluate_russian,
                   "8": local_ai_text_mode}
        fn = actions.get(choice)
        if fn: _perform(fn); _pause()


def translation_menu() -> None:
    while True:
        _clear()
        print("=" * 64)
        print("  RPTS - GOOGLE БЕСПЛАТНО / ДРУГИЕ ПРОВАЙДЕРЫ")
        print("=" * 64)
        print("\n  1. Бесплатный Google: перевести -> установить -> запустить")
        print("  2. Бесплатный Google: показать объём перевода")
        print("  3. Бесплатный Google: продолжить без установки")
        print("  4. Открыть настройки бесплатного Google")
        print("  5. Google Cloud API (платный, опционально)")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        actions = {"1": google_free_full, "2": google_free_dry, "3": google_free_translate_only,
                   "4": edit_config, "5": google_cloud_translate_only}
        if choice == "0":
            return
        fn = actions.get(choice)
        if fn:
            _perform(fn)
            _pause()


def ui_menu() -> None:
    while True:
        _clear()
        print("=" * 64)
        print("  RPTS - ИНТЕРФЕЙС И МЕНЮ ИГРЫ")
        print("=" * 64)
        print("\n  1. Проверить покрытие UI")
        print("  2. Перевести недостающий интерфейс локальным ИИ")
        print("  3. Ручные исправления ui_overrides.json")
        print("  4. Включить runtime UI capture")
        print("  5. Импортировать runtime UI из игры")
        print("  6. Выключить runtime UI capture")
        print("  7. Пересобрать + установить патч")
        print("  8. Открыть work\\ui")
        print("  9. Перевести недостающий интерфейс через Google")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        if choice == "0":
            return
        if choice == "8":
            _open(UI_WORK); continue
        actions = {
            "1": ui_coverage, "2": ui_translate_missing, "3": ui_edit_overrides,
            "4": ui_enable_capture, "5": ui_import_runtime, "6": ui_disable_capture,
            "7": build_install,
            "9": ui_google_translate_missing,
        }
        fn = actions.get(choice)
        if fn:
            _perform(fn); _pause()


def patch_menu() -> None:
    while True:
        _clear()
        print("=" * 64)
        print("  RPTS - Патч игры")
        print("=" * 64)
        print("\n  1. Собрать + установить русский перевод")
        print("  2. Статус установленного патча")
        print("  3. Удалить RPTS-перевод из игры")
        print("  4. Запустить игру")
        print("  5. Открыть work\\patch")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        if choice == "0":
            return
        if choice == "5":
            _open(WORK / "patch")
            continue
        actions = {"1": build_install, "2": patch_status, "3": uninstall_patch, "4": launch_game}
        fn = actions.get(choice)
        if fn:
            _perform(fn)
            _pause()


def tools_menu() -> None:
    while True:
        _clear()
        print("=" * 64)
        print("  RPTS - Диагностика / сервис")
        print("=" * 64)
        print("\n  1. Проверить project DB / статус перевода")
        print("  2. Упаковать аудит и диагностику ИИ в RenPyAuditReport.zip")
        print("  3. Установить / обновить unrpyc")
        print("  4. Анализ runtime log (.tsv)")
        print("  5. Открыть reports / work")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        if choice == "0": return
        actions = {"1": check_project, "2": pack_report, "3": install_unrpyc, "4": runtime_log, "5": open_folders}
        fn = actions.get(choice)
        if fn: _perform(fn); _pause()

def self_check() -> int:
    required = [
        APP / "tools/audit_game.py",
        APP / "tools/translate_project.py",
        APP / "tools/patch_project.py",
        APP / "tools/project_summary.py",
        APP / "tools/ui_project.py",
        APP / "tools/local_ai.py",
        APP / "tools/evaluate_russian.py",
        APP / "tools/analyze_runtime_log.py",
        APP / "src/rpts/local_ai.py",
        TEMPLATES / "russian_evaluation_cases.json",
        FREE_GOOGLE_CFG,
        CLOUD_GOOGLE_CFG,
        LOCAL_AI_CFG_EXAMPLE,
        MOCK_CFG,
        UI_OVERRIDES_EXAMPLE,
    ]
    missing = [str(x.relative_to(ROOT)) for x in required if not x.exists()]
    if missing:
        print("[ERROR] Не хватает файлов:")
        for item in missing:
            print(" -", item)
        return 1
    print("[OK] RPTS launcher self-check passed.")
    print(f"Python: {sys.version.split()[0]}")
    print(f"Root: {ROOT}")
    return 0


def additional_menu() -> None:
    while True:
        _clear(); print("="*64); print("  RPTS - НАСТРОЙКИ И ИНСТРУМЕНТЫ"); print("="*64)
        print("\n  1. Выбрать другую игру / повторить проверку файлов")
        print("  2. Режим перевода: скорость или подробная редактура")
        print("  3. Модель ИИ, оборудование и место хранения")
        print("  4. Персонажи, проверка качества и повторный перевод")
        print("  5. Интерфейс, телефон и непереведённые строки")
        print("  6. Установить / удалить перевод или запустить игру")
        print("  7. Google и другие способы перевода")
        print("  8. Диагностика, обслуживание и папки проекта")
        print("  0. Назад")
        choice = _input("\nВыбор: ").strip()
        if choice == "0": return
        menus = {"3": local_ai_menu, "4": character_menu, "5": ui_menu, "6": patch_menu,
                 "7": translation_menu, "8": tools_menu}
        if choice in menus: _perform(menus[choice])
        elif choice == "1": _perform(audit); _pause()
        elif choice == "2": _perform(local_ai_text_mode); _pause()
        else: print("Выбери номер из списка.")


def project_overview() -> None:
    game = _metadata_value(WORK_DB if WORK_DB.is_file() else REPORT_DB, "game_dir")
    if not game:
        print("\n  Игра: ещё не выбрана. Начни с пункта 1.")
    else:
        print(f"\n  Игра: {Path(game).parent.name or Path(game).name}")
        db_path = WORK_DB if WORK_DB.is_file() else REPORT_DB
        try:
            with closing(sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)) as db:
                row = db.execute("SELECT count(*), sum(status IN ('translated','translated_tm','approved')), sum(status='error') FROM segments WHERE source_text<>'' AND channel IN ('semantic','localization')").fetchone()
            print(f"  Перевод: {row[1] or 0} / {row[0]} строк; ошибок: {row[2] or 0}")
        except sqlite3.Error:
            print("  Статус базы доступен в настройках → диагностике.")
    try:
        settings = load_settings(LOCAL_AI_SETTINGS)
        spec = MODEL_SPECS.get(str(settings.get("model_key")))
        ready = installation_status(settings).get("ready")
        if spec:
            print(f"  Модель: {spec.title} — {'готова' if ready else 'нужна настройка'}")
        elif WORKSPACE.ai.is_dir():
            print("  Модель: сохранена папка local_ai; подключится при переводе.")
        else:
            print("  Модель: ещё не настроена. Мастер откроется при первом переводе.")
    except (OSError, ValueError, AttributeError):
        print("  Модель: проверь настройки в пункте 2.")
    try:
        cfg = json.loads(LOCAL_AI_CFG.read_text(encoding="utf-8")) if LOCAL_AI_CFG.is_file() else {}
        quality = cfg.get("quality") or {}
        mode = "full_review" if quality.get("fidelity_pass") else "reviewed" if quality.get("combined_review") else quality.get("text_mode", _new_text_mode())
        label = "перевод с контекстом и исправлением ошибок" if uses_source_only_review(cfg) else {"full_review": "полная редактура (медленнее)", "reviewed": "качество и скорость", "balanced": "обычный перевод"}.get(mode, str(mode))
        if model_family(cfg.get("provider") or {}) == "hy_mt2" and quality.get("native_free_review"):
            label += " (эксперимент Hy-MT2)"
        print("  Режим: " + label)
    except (OSError, ValueError, AttributeError):
        print("  Режим: настройки требуют проверки.")


def translate_current_game(target: str | None = None) -> int:
    if target is not None or not REPORT_DB.is_file():
        if target is None:
            print("\nУкажи папку игры или её EXE. Можно перетащить их в это окно.")
            target = _input("Путь к игре (Enter — назад): ").strip().strip('"')
        if not target:
            return 0
        if audit(target) != 0 or not REPORT_DB.is_file():
            return 1
    return _local_ai_translate_cycle(simple=True)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--self-check":
        return self_check()
    version = (APP / "VERSION.txt").read_text(encoding="utf-8").strip()
    diagnostics.begin_session(WORKSPACE.data, version)
    _activate_project_workspace()
    if argv:
        arg = argv[0].strip('"')
        rc = _perform(runtime_log, arg) if Path(arg).suffix.lower() == ".tsv" else _perform(translate_current_game, arg)
        _pause()
        return rc or 0

    while True:
        _clear()
        print("=" * 64)
        print(f"  RPTS — бесплатный переводчик игр | {version}")
        print("=" * 64)
        project_overview()
        print("\n  1. Перевести игру / продолжить перевод")
        print("  2. Настройки и все инструменты")
        print("  3. Собрать ZIP-отчёт для помощи")
        print("  0. Выход")
        print("\n  Начни с 1: выбери игру → подтверди запуск → дождись перевода.")
        print("  Модель подберётся автоматически. Свои настройки — в пункте 2.")
        try:
            choice = _input("\nВыбор: ").strip()
        except (EOFError, KeyboardInterrupt):
            print(); return 0
        if choice == "0":
            diagnostics.event("session_end")
            return 0
        if choice == "1": _perform(translate_current_game); _pause()
        elif choice == "2": _perform(additional_menu)
        elif choice == "3": _perform(pack_report); _pause()
        else: print("Выбери 1, 2, 3 или 0."); _pause()


if __name__ == "__main__":
    try:
        with OperationLock(WORKSPACE.data / "operation.lock"):
            raise SystemExit(main())
    except Exception as exc:
        print("\n" + "=" * 64)
        print("RPTS CRASH")
        print("=" * 64)
        print(f"{type(exc).__name__}: {exc}")
        print("\nПолный traceback:")
        import traceback
        traceback.print_exc()
        diagnostics.exception("launcher_crash")
        pack_report()
        _pause()
        raise SystemExit(1)
