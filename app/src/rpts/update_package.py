"""Portable update format and crash-safe installer. Stdlib only; copied outside the app before use."""
from __future__ import annotations

import contextlib
import ctypes
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path, PurePosixPath

SCHEMA = 1
MAX_BYTES = 3 * 1024**3
ROOT_FILES = {'README.md', 'AGENTS.md', 'RPTS.bat', 'RPTS.vbs', 'RPTS-Console.bat', 'RPTS-Debug.bat'}
REQUIRED = {'app/VERSION.txt', 'app/tools/launch_desktop.py', 'app/tools/apply_update.py',
            'app/src/rpts/update_package.py', 'app/src/rpts/updater.py', 'app/web/index.html',
            'app/desktop/bin/RPTS.Desktop.exe', 'runtime/python/python.exe', 'runtime/dotnet/dotnet.exe', 'RPTS.bat'}


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return {} if default is None else default


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def owned_path(value):
    """A distribution can never own games, models, projects or settings."""
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise ValueError('Недопустимый путь в обновлении.')
    parts = value.split('/')
    if any(not p or p in {'.', '..'} or p[-1:] in {'.', ' '} or
           re.search(r'[<>"|?*\x00-\x1f]', p) or
           re.fullmatch(r'(?i)(con|prn|aux|nul|com[0-9]|lpt[0-9])(?:\..*)?', p) for p in parts):
        raise ValueError('Недопустимый путь в обновлении: ' + value)
    if any(p.lower() in {'.git', '__pycache__'} for p in parts):
        raise ValueError('Служебный путь в обновлении: ' + value)
    allowed = value in ROOT_FILES or value.startswith(('app/', 'docs/', 'runtime/python/', 'runtime/dotnet/', 'runtime/installers/'))
    if not allowed or value.lower().endswith(('.gguf', '.sqlite', '.sqlite3', '.db', '.rpa', '.rpyc')):
        raise ValueError('Обновление пытается изменить пользовательские данные: ' + value)
    return value


def safe_target(root, rel):
    rel = owned_path(rel)
    path = Path(root) / rel
    for item in (Path(root), *path.parents, path):
        if item.is_symlink() or (hasattr(item, 'is_junction') and item.is_junction()):
            raise ValueError('Ссылка или junction в папке установки: ' + str(item))
    if path.exists() and not path.is_file():
        raise ValueError('Вместо файла найдена папка: ' + rel)
    return path


class FileLock:
    def __init__(self, path):
        self.path = Path(path)
        self.file = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open('a+b')
        try:
            if not self.path.stat().st_size:
                self.file.write(b'0'); self.file.flush()
            self.file.seek(0)
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close(); self.file = None
            raise RuntimeError('RPTS ещё работает в другом окне. Закрой его и повтори обновление.')
        return self

    def __exit__(self, *args):
        if self.file:
            if os.name == 'nt':
                import msvcrt
                self.file.seek(0); msvcrt.locking(self.file.fileno(), msvcrt.LK_UNLCK, 1)
            self.file.close(); self.file = None


def inspect_package(archive, expected=None):
    """Validate the entire file inventory before extracting or changing the installation."""
    with zipfile.ZipFile(archive) as z:
        infos = z.infolist()
        if len(infos) > 20000 or sum(x.file_size for x in infos) > MAX_BYTES:
            raise ValueError('Архив обновления слишком велик.')
        members = {}
        for info in infos:
            if info.is_dir():
                continue
            if not info.filename.startswith('RenPyTranslator/') or stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError('Неверная структура архива обновления.')
            rel = owned_path(info.filename[len('RenPyTranslator/'):])
            if rel.casefold() in members:
                raise ValueError('Повторяющиеся пути в архиве.')
            members[rel.casefold()] = (rel, info)
        manifest_name = 'app/release_manifest.json'
        if manifest_name not in members or members[manifest_name][1].file_size > 8*1024**2:
            raise ValueError('В архиве нет корректного манифеста.')
        manifest = json.loads(z.read(members[manifest_name][1]))
        if manifest.get('schema') != SCHEMA or manifest.get('channel') != 'dev' or manifest.get('platform') != 'win-x64':
            raise ValueError('Неподдерживаемый формат или канал обновления.')
        if not re.fullmatch(r'[0-9a-f]{40}', str(manifest.get('build_id', ''))) or not isinstance(manifest.get('build_number'), int):
            raise ValueError('Неверный номер сборки.')
        if expected and any(manifest.get(k) != expected.get(k) for k in ('build_id', 'build_number', 'version')):
            raise ValueError('Архив не соответствует объявленной сборке.')
        rows = manifest.get('files')
        if not isinstance(rows, list) or not rows:
            raise ValueError('Пустой состав обновления.')
        indexed = {}
        for row in rows:
            rel = owned_path(row['path'])
            if rel == manifest_name or rel.casefold() in indexed or not re.fullmatch(r'[a-f0-9]{64}', str(row.get('sha256', ''))):
                raise ValueError('Неверный состав обновления.')
            if type(row.get('bytes')) is not int or row['bytes'] < 0:
                raise ValueError('Неверный размер файла.')
            indexed[rel.casefold()] = row
        if set(members) != set(indexed) | {manifest_name} or not REQUIRED <= {r['path'] for r in rows}:
            raise ValueError('Архив неполон или содержит лишние файлы.')
        for key, row in indexed.items():
            info = members[key][1]
            if info.file_size != row['bytes']:
                raise ValueError('Неверный размер: ' + row['path'])
        return manifest, members


def extract_verified(archive, stage, expected=None):
    manifest, members = inspect_package(archive, expected)
    with zipfile.ZipFile(archive) as z:
        rows = manifest['files'] + [{'path': 'app/release_manifest.json', 'sha256': None}]
        for row in rows:
            path = safe_target(stage, row['path'])
            path.parent.mkdir(parents=True, exist_ok=True)
            with z.open(members[row['path'].casefold()][1]) as source, path.open('wb') as output:
                shutil.copyfileobj(source, output, 1024**2)
            if row['sha256'] and sha256(path) != row['sha256']:
                raise ValueError('Контрольная сумма не совпала: ' + row['path'])
    return manifest


def log(root, event, **fields):
    path = Path(root)/'data/logs/updater.log'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a', encoding='utf-8') as stream:
        stream.write(json.dumps({'time': time.time(), 'event': event, **fields}, ensure_ascii=False) + '\n')


def recover(root):
    """Replay a durable rollback journal after an interrupted installation."""
    root = Path(root)
    folder = root/'data/updates/rollback'
    journal = read_json(folder/'journal.json')
    if journal.get('status') != 'applying':
        return False
    for row in reversed(journal['operations']):
        target = safe_target(root, row['path'])
        backup = safe_target(folder/'files', row['path'])
        if row['existed']:
            if not backup.is_file():
                raise RuntimeError('Не найден файл отката: ' + row['path'])
            if target.is_file() and sha256(target) == sha256(backup):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.rpts-restore')
            shutil.copy2(backup, temporary)
            os.replace(temporary, target)
        else:
            target.unlink(missing_ok=True)
    journal['status'] = 'rolled_back'
    write_json(folder/'journal.json', journal)
    log(root, 'rolled_back', build_id=journal.get('build_id'))
    return True


def install(root, archive, expected, replace=os.replace):
    root = Path(root)
    updates = root/'data/updates'
    recover(root)
    stage = updates/'stage'
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)
    try:
        if sha256(archive) != expected['sha256']:
            raise ValueError('Архив изменился после загрузки.')
        manifest = extract_verified(archive, stage, expected)
        old = read_json(root/'app/release_manifest.json')
        rows = manifest['files'] + [{'path': 'app/release_manifest.json'}]
        current = {r['path'].casefold() for r in rows}
        obsolete = []
        for row in old.get('files', []):
            try:
                target = safe_target(root, row['path'])
            except ValueError:
                continue
            if row['path'].casefold() not in current and target.is_file() and sha256(target) == row.get('sha256'):
                obsolete.append(row)
        for row in rows + obsolete:
            safe_target(root, row['path'])
        backup = updates/'rollback'
        if backup.exists():
            shutil.rmtree(backup)
        journal = {'status': 'applying', 'build_id': manifest['build_id'], 'operations': []}
        write_json(backup/'journal.json', journal)
        try:
            for row in obsolete + rows:
                target = safe_target(root, row['path'])
                existed = target.is_file()
                if existed:
                    saved = safe_target(backup/'files', row['path'])
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(target, saved)
                # Intent reaches disk before every mutation, including new files and deletions.
                journal['operations'].append({'path': row['path'], 'existed': existed})
                write_json(backup/'journal.json', journal)
                if row in obsolete:
                    target.unlink()
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    replace(stage/row['path'], target)
            journal['status'] = 'committed'
            write_json(backup/'journal.json', journal)
        except BaseException:
            recover(root)
            raise
        log(root, 'installed', build_id=manifest['build_id'], files=len(rows))
        return manifest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


class HandoffCancelled(Exception):
    pass


def wait_for_parents(pids, ready, cancel, timeout=150):
    if os.name != 'nt':
        raise RuntimeError('Установка обновления поддерживается только в Windows.')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handles = []
    try:
        for pid in set(pids):
            if type(pid) is not int or pid <= 0 or pid == os.getpid():
                raise ValueError('Неверный процесс запуска.')
            handle = kernel.OpenProcess(0x100000, False, pid)
            if handle:
                handles.append(handle)
            elif ctypes.get_last_error() != 87:  # ERROR_INVALID_PARAMETER: already exited
                raise OSError(ctypes.get_last_error(), 'Не удалось дождаться закрытия RPTS.')
        Path(ready).touch()
        deadline = time.monotonic() + timeout
        for handle in handles:
            while kernel.WaitForSingleObject(handle, 250) == 258:
                if Path(cancel).exists():
                    raise HandoffCancelled()
                if time.monotonic() >= deadline:
                    raise TimeoutError('RPTS не закрылся. Файлы не заменены; повтори обновление.')
            if kernel.WaitForSingleObject(handle, 0) != 0:
                raise OSError('Не удалось проверить завершение RPTS.')
        if Path(cancel).exists():
            raise HandoffCancelled()
    finally:
        for handle in handles:
            kernel.CloseHandle(handle)


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    request = read_json(args[0])
    root = Path(request['root'])
    result = root/'data/updates/result.json'
    exited = False
    try:
        wait_for_parents(request['pids'], request['ready'], request['cancel'])
        exited = True
        # All entry points use these locks. Never replace files used by another GUI or CLI.
        with contextlib.ExitStack() as stack:
            for name in ('update.lock', 'desktop.lock', 'operation.lock'):
                stack.enter_context(FileLock(root/'data'/name))
            install(root, Path(request['archive']), request['release'])
            write_json(result, {'status': 'installed', 'version': request['release']['version'], 'time': time.time()})
        Path(request['archive']).unlink(missing_ok=True)
    except HandoffCancelled:
        return 0
    except Exception as exc:
        log(root, 'install_failed', error=str(exc))
        write_json(result, {'status': 'failed', 'message': str(exc), 'time': time.time()})
        ctypes.windll.user32.MessageBoxW(None, 'Обновление RPTS не установлено.\n'+str(exc)+'\nЖурнал: data\\logs\\updater.log', 'RPTS', 16)
    finally:
        if exited and (root/'RPTS.bat').is_file():
            subprocess.Popen([str(root/'runtime/python/pythonw.exe'), str(root/'app/tools/launch_desktop.py')],
                             cwd=root, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return 0
