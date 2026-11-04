"""Build one self-contained support ZIP without touching the game's files."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import sqlite3
import sys
import tempfile
import time
import zipfile
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from . import diagnostics


def build_report(*, root, app, reports, work, database, quality, server_log, config, settings, overrides, game=None):
    root, app, reports, work = map(Path, (root, app, reports, work))
    data = root / 'data' if (root / 'data' / 'settings').is_dir() else root
    report_output = data / 'reports' if data != root else root
    report_output.mkdir(parents=True, exist_ok=True)
    out = report_output / 'RenPyAuditReport.zip'
    temporary = out.with_name(out.name + '.tmp')
    included, warnings, missing = [], [], []
    manifest = {'created_utc': datetime.now(timezone.utc).isoformat(), 'included': included,
                'warnings': warnings, 'missing': missing, 'game': str(game or ''), 'work': str(work),
                'profiles_available': (quality / 'character_profiles.json').is_file(),
                'profile_review_available': (quality / 'character_profiles_review.csv').is_file(),
                'server_log_available': server_log.is_file()}
    diagnostics.remember_secrets(v for k, v in os.environ.items() if any(s in k.lower() for s in ('key', 'token', 'password', 'secret')))
    names = set()
    try:
        with tempfile.TemporaryDirectory(prefix='rpts-report-') as temp, zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_DEFLATED) as zf:
            def write(name, value):
                if name in names:
                    return
                text = value if isinstance(value, str) else json.dumps(diagnostics.redact(value), ensure_ascii=False, indent=2, default=str)
                zf.writestr(name, diagnostics.redact(text))
                included.append(name); names.add(name)

            def add(path, name):
                path = Path(path)
                if name in names or path.is_symlink():
                    return
                if not path.is_file():
                    missing.append(name); return
                try:
                    if path.suffix == '.sqlite':
                        snapshot = Path(temp) / (hashlib.sha256(name.encode()).hexdigest() + '.sqlite')
                        deadline = time.monotonic() + 15
                        def progress(status, remaining, total):
                            if time.monotonic() > deadline:
                                raise TimeoutError('SQLite snapshot exceeded 15 seconds; database may be busy')
                        with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True, timeout=2)) as source:
                            with closing(sqlite3.connect(snapshot)) as target:
                                source.backup(target, pages=256, progress=progress, sleep=.05)
                        zf.write(snapshot, name)
                        included.append(name); names.add(name)
                        if name == 'work/translation_project.sqlite':
                            with closing(sqlite3.connect(snapshot)) as db:
                                try:
                                    write('diagnostics/project_status.json', {
                                        'statuses': dict(db.execute('SELECT status,count(*) FROM segments GROUP BY status')),
                                        'kinds': dict(db.execute('SELECT kind,count(*) FROM segments GROUP BY kind')),
                                        'runs': [dict(zip([c[0] for c in cur.description], row)) for cur in [db.execute('SELECT * FROM translation_runs ORDER BY started_at DESC LIMIT 20')] for row in cur]})
                                except sqlite3.Error as exc:
                                    warnings.append({'file': name, 'summary_error': str(exc)})
                    else:
                        text = path.read_text(encoding='utf-8', errors='replace')
                        if path.suffix == '.json':
                            try: text = json.dumps(diagnostics.redact(json.loads(text)), ensure_ascii=False, indent=2)
                            except ValueError:
                                warnings.append({'file': name, 'error': 'Invalid JSON retained as text'})
                        write(name, text)
                except (OSError, ValueError, sqlite3.Error, TimeoutError) as exc:
                    warnings.append({'file': name, 'error': f'{type(exc).__name__}: {exc}'})

            def tree(folder, prefix, allowed=None):
                if not folder.is_dir():
                    missing.append(prefix); return
                try:
                    for path in sorted(folder.rglob('*')):
                        if path.is_file() and not path.is_symlink() and path.suffix not in {'.pyc', '.gguf', '.exe', '.dll'}:
                            if path.name.endswith(('-wal', '-shm')) or '__pycache__' in path.parts:
                                continue
                            if allowed is None or path.suffix in allowed:
                                add(path, prefix + '/' + path.relative_to(folder).as_posix())
                except OSError as exc:
                    warnings.append({'file': prefix, 'error': str(exc)})

            tree(reports, 'reports', {'.json', '.jsonl', '.csv', '.tsv', '.txt', '.md', '.html', '.sqlite'})
            add(database, 'work/translation_project.sqlite')
            tree(quality, 'work/quality')
            tree(data / 'jobs', 'diagnostics/desktop_jobs', {'.json', '.jsonl', '.log'})
            tree(data / 'logs', 'diagnostics/desktop', {'.log', '.txt', '.json'})
            add(data / 'layout_migration.json', 'diagnostics/layout_migration.json')
            tree(app / 'web', 'diagnostics/frontend', {'.html', '.css', '.js'})
            tree(app / 'desktop' / 'src', 'diagnostics/cef_host', {'.cs', '.csproj', '.manifest'})
            tree(app / 'desktop' / 'picker', 'diagnostics/native_picker', {'.cs', '.csproj'})
            add(app / 'VERSION.txt', 'diagnostics/VERSION.txt')
            add(app / 'release_manifest.json', 'diagnostics/release_manifest.json')
            add(data / 'updates' / 'result.json', 'diagnostics/update_result.json')
            add(data / 'updates' / 'rollback' / 'journal.json', 'diagnostics/update_journal.json')
            tree(work / 'ui', 'work/ui', {'.json', '.jsonl', '.csv', '.tsv', '.txt'})
            tree(work / 'patch', 'work/patch', {'.json', '.rpy', '.py', '.txt'})
            for path in sorted(server_log.parent.glob(server_log.name + '*')):
                add(path, 'work/local_ai/' + path.name)
            add(server_log.parent / "model_capabilities.json", "work/local_ai/model_capabilities.json")
            add(config, 'diagnostics/local_ai_config.json')
            add(settings, 'diagnostics/local_ai_settings.json')
            add(data / 'settings' / 'menu_preferences.json', 'diagnostics/menu_preferences.json')
            add(overrides, 'diagnostics/ui_overrides.json')
            for path in sorted((data / 'settings').glob('translation_config*.json')):
                if path != config:
                    add(path, 'diagnostics/settings/' + path.name)
            add(app / 'VERSION.txt', 'diagnostics/VERSION.txt')
            manifest['version'] = (app / 'VERSION.txt').read_text(encoding='utf-8').strip() if (app / 'VERSION.txt').is_file() else ''
            session_root = data / 'diagnostics' / 'sessions'
            sessions = sorted((p for p in session_root.glob('*') if p.is_dir()), reverse=True)
            manifest['sessions_included'] = [p.name for p in sessions[:10]]
            manifest['older_sessions_not_included'] = [p.name for p in sessions[10:]]
            manifest['log_policy'] = {'report_sessions': 10, 'ai_log_part_bytes': 20*1024*1024, 'ai_previous_parts': 3,
                                      'console_part_bytes': 10*1024*1024, 'console_previous_parts': 2}
            for session in sessions[:10]:
                tree(session, 'diagnostics/sessions/' + session.name)
            write('diagnostics/environment.json', {'python': sys.version, 'executable': sys.executable,
                'platform': platform.platform(), 'architecture': platform.machine(), 'cpu_count': os.cpu_count()})
            if game:
                game = Path(game)
                for folder, prefix in ((game, 'game'), (game.parent, 'game_root')):
                    for name in ('log.txt', 'traceback.txt', 'errors.txt', 'rpts_runtime_strings.tsv', 'rpts_runtime_ui.tsv', 'rpts_translation_manifest.json'):
                        if (folder / name).is_file(): add(folder / name, 'diagnostics/' + prefix + '/' + name)
            # Exact implementation and built-in prompt rules make custom/replaced files visible.
            hashes = {}
            for folder in (app / 'src' / 'rpts', app / 'tools', app / 'templates'):
                for path in sorted(folder.glob('*')):
                    if path.is_file() and path.suffix in {'.py', '.json'}:
                        name = path.relative_to(app).as_posix()
                        try: hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
                        except OSError as exc: warnings.append({'file': name, 'error': str(exc)})
                        add(path, 'diagnostics/application/' + name)
            write('diagnostics/application_sha256.json', hashes)
            if config.is_file():
                try:
                    from .translation_engine import _system_prompt, _review_system_prompt, _review_stages
                    from .characters import PROFILE_SYSTEM, PROFILE_SYSTEM_COMPACT
                    from .model_policy import model_family
                    cfg = json.loads(config.read_text(encoding='utf-8'))
                    write('diagnostics/prompt_snapshot.json', {'character_profiles': PROFILE_SYSTEM_COMPACT if model_family(cfg.get('provider', {})) == 'translategemma' else PROFILE_SYSTEM,
                        'translation': {mode: _system_prompt(cfg, mode) for mode in ('dialogue', 'narration', 'ui')},
                        'review': {stage: _review_system_prompt(cfg, 'dialogue', stage) for stage in _review_stages(cfg)}})
                except Exception as exc:
                    warnings.append({'file': 'diagnostics/prompt_snapshot.json', 'error': f'{type(exc).__name__}: {exc}'})
            write('diagnostics/README.txt', 'RPTS diagnostic bundle\nStart with manifest.json, project_status.json and sessions/*/events.jsonl.\n'
                  'ai_requests.jsonl links complete request/response pairs by request_id, including malformed answers.\n'
                  'work/quality contains profiles, batches, repair attempts and evaluations; database contains saved progress.\n'
                  'Missing/unreadable files are listed in manifest.json; the report may be partial.\n'
                  'API credentials are redacted. Game text, local paths and manual notes remain for diagnosis.\n'
                  'Game executables, media, models and saved games are not included.\n')
            # Write manifest last so failures and all members are visible.
            zf.writestr('diagnostics/manifest.json', json.dumps(diagnostics.redact(manifest), ensure_ascii=False, indent=2))
        temporary.replace(out)
    finally:
        temporary.unlink(missing_ok=True)
    return out, manifest
