"""Fixed GitHub dev channel, background download and detached Windows handoff."""
from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import update_package as package

REPO = 'UFrimNe/RenPyTranslator'
FEED = f'https://github.com/{REPO}/releases/download/dev-latest/dev-update.json'
BUSY = {'downloading', 'preparing', 'restarting'}


class GitHubRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        url = urllib.parse.urlsplit(newurl)
        if url.scheme != 'https' or url.username or url.password or url.hostname not in {
            'github.com', 'objects.githubusercontent.com', 'release-assets.githubusercontent.com'}:
            raise ValueError('Неожиданный адрес загрузки GitHub.')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def open_url(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'RPTS-dev-updater/1', 'Cache-Control': 'no-cache'})
    return urllib.request.build_opener(GitHubRedirects()).open(request, timeout=30)


def validate_release(value):
    if not isinstance(value, dict) or value.get('schema') != 1 or value.get('channel') != 'dev' or value.get('platform') != 'win-x64':
        raise ValueError('Неверный канал или формат обновления.')
    if value.get('min_updater', 1) > package.SCHEMA:
        raise ValueError('Нужна новая полная сборка с GitHub: этот установщик устарел.')
    if not re.fullmatch(r'[a-f0-9]{40}', str(value.get('build_id', ''))) or not re.fullmatch(r'[a-f0-9]{64}', str(value.get('sha256', ''))):
        raise ValueError('Неверная контрольная сумма или ID сборки.')
    if type(value.get('build_number')) is not int or value['build_number'] <= 0:
        raise ValueError('Неверный номер сборки.')
    if not re.fullmatch(r'[0-9]+(?:\.[0-9]+){1,3}', str(value.get('version', ''))):
        raise ValueError('Неверная версия обновления.')
    if any(type(value.get(k)) is not int or not 0 < value[k] <= package.MAX_BYTES for k in ('bytes', 'unpacked_bytes')):
        raise ValueError('Неверный размер обновления.')
    url = urllib.parse.urlsplit(str(value.get('package_url', '')))
    if url.scheme != 'https' or url.netloc != 'github.com' or url.query or url.fragment or not re.fullmatch(
        '/' + REPO + r'/releases/download/dev-' + str(value['build_number']) + r'/RenPyTranslator_[A-Za-z0-9_.-]+\.zip', url.path):
        raise ValueError('Архив обновления должен находиться в релизах нашего репозитория.')
    return {k: value[k] for k in ('schema', 'channel', 'platform', 'version', 'build_id', 'build_number', 'sha256',
                                 'bytes', 'unpacked_bytes', 'package_url')} | {'notes': str(value.get('notes', ''))[:4000]}


def download(release, target, progress=lambda done, total: None, opener=open_url):
    partial = Path(target).with_suffix('.part')
    try:
        with opener(release['package_url']) as source, partial.open('wb') as output:
            done = 0
            while block := source.read(1024**2):
                done += len(block)
                if done > release['bytes']:
                    raise ValueError('Получен слишком большой архив обновления.')
                output.write(block)
                progress(done, release['bytes'])
        if done != release['bytes'] or package.sha256(partial) != release['sha256']:
            raise ValueError('Загрузка неполная или контрольная сумма не совпала. Повтори попытку.')
        package.inspect_package(partial, release)
        os.replace(partial, target)
    finally:
        partial.unlink(missing_ok=True)


def close_host(pid):
    """Close only our own CEF window, after the independent installer is ready."""
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    try:
        size = ctypes.c_uint32(32768); name = ctypes.create_unicode_buffer(size.value)
        if not handle or not kernel.QueryFullProcessImageNameW(handle, 0, name, ctypes.byref(size)) or Path(name.value).name.lower() != 'rpts.desktop.exe':
            raise RuntimeError('Не найдено окно RPTS. Запусти приложение через RPTS.bat.')
    finally:
        if handle: kernel.CloseHandle(handle)
    user = ctypes.WinDLL('user32', use_last_error=True)
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_ssize_t)
    user.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    user.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
    user.EnumWindows.argtypes = [callback_type, ctypes.c_ssize_t]
    closed = []
    @callback_type
    def callback(window, _):
        owner = ctypes.c_uint32()
        user.GetWindowThreadProcessId(window, ctypes.byref(owner))
        if owner.value == pid and user.PostMessageW(window, 0x0010, 0, 0):
            closed.append(True)
        return 1
    user.EnumWindows(callback, 0)
    if not closed:
        raise RuntimeError('Не удалось закрыть окно. Закрой RPTS самостоятельно для установки обновления.')


class Updater:
    def __init__(self, root):
        self.root = Path(root)
        self.folder = self.root/'data/updates'
        self.lock = threading.RLock()
        self.value = {'status': 'idle', 'channel': 'dev', 'message': 'Проверка обновлений ещё не выполнялась.', 'progress': None}
        self.release = None

    def snapshot(self):
        with self.lock:
            return dict(self.value, last_result=package.read_json(self.folder/'result.json'))

    def active(self):
        with self.lock:
            return self.value['status'] in BUSY

    def set(self, **value):
        with self.lock:
            self.value.update(value)

    def check(self):
        with self.lock:
            if self.active() or self.value['status'] == 'checking':
                return self.snapshot()
            self.set(status='checking', message='Проверяю сборку в dev…', progress=None)
        threading.Thread(target=self._check, daemon=True).start()
        return self.snapshot()

    def _check(self):
        try:
            old_helper = package.read_json(self.folder/'helper.json').get('path', '')
            if old_helper:
                old_helper = Path(old_helper)
                if old_helper.parent == Path(tempfile.gettempdir()) and old_helper.name.startswith('rpts-update-'):
                    shutil.rmtree(old_helper, ignore_errors=True)
            with open_url(FEED) as response:
                raw = response.read(128*1024 + 1)
            if len(raw) > 128*1024:
                raise ValueError('Слишком большой манифест обновления.')
            release = validate_release(json.loads(raw))
            current = package.read_json(self.root/'app/release_manifest.json')
            latest = release['build_id'] == current.get('build_id') or release['build_number'] <= current.get('build_number', 0)
            with self.lock:
                self.release = None if latest else release
                self.set(status='current' if latest else 'available', message='Установлена актуальная сборка dev.' if latest else 'Доступна новая сборка dev.',
                         checked_at=time.time(), release=release)
            package.log(self.root, 'checked', build_id=release['build_id'], available=not latest)
        except urllib.error.HTTPError as exc:
            self.set(status='unpublished' if exc.code == 404 else 'error',
                     message='Первая сборка dev ещё не опубликована.' if exc.code == 404 else f'GitHub недоступен (HTTP {exc.code}). Попробуй позже.', checked_at=time.time())
            package.log(self.root, 'check_failed', error=str(exc))
        except Exception as exc:
            self.set(status='error', message='Не удалось проверить обновления: '+str(exc), checked_at=time.time())
            package.log(self.root, 'check_failed', error=str(exc))

    def begin(self):
        with self.lock:
            if self.active() or not self.release or self.value['status'] != 'available':
                raise ValueError('Сначала проверь наличие обновления.')
            if os.name != 'nt':
                raise ValueError('Автообновление предназначено для Windows x64.')
            # Reserve the same operation lock as translation and the console, before downloading.
            reservation = package.FileLock(self.root/'data/operation.lock')
            reservation.__enter__()
            self.set(status='downloading', progress=0, message='Скачиваю обновление…')
            threading.Thread(target=self._install, args=(dict(self.release), reservation), daemon=True).start()
        return self.snapshot()

    def _install(self, release, reservation):
        helper = None
        try:
            self.folder.mkdir(parents=True, exist_ok=True)
            needed = release['bytes'] + release['unpacked_bytes']*3 + 128*1024**2
            if shutil.disk_usage(self.root).free < needed:
                raise ValueError('Недостаточно свободного места для архива, установки и резервной копии.')
            archive = self.folder/'download.zip'
            download(release, archive, lambda done, total: self.set(progress=round(done*100/total, 1)))
            self.set(status='preparing', progress=None, message='Готовлю установку и перезапуск…')
            # The installer has its own interpreter: no DLL or Python file in the installation stays locked.
            helper = Path(tempfile.mkdtemp(prefix='rpts-update-'))
            package.write_json(self.folder/'helper.json', {'path': str(helper)})
            shutil.copytree(self.root/'runtime/python', helper/'python', ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            shutil.copy2(self.root/'app/tools/apply_update.py', helper/'apply_update.py')
            shutil.copy2(Path(__file__).with_name('update_package.py'), helper/'update_package.py')
            pids = [os.getpid(), os.getppid()]
            if os.environ.get('RPTS_BOOTSTRAP_PID', '').isdigit():
                pids.append(int(os.environ['RPTS_BOOTSTRAP_PID']))
            request = {'root': str(self.root), 'archive': str(archive), 'release': release, 'pids': pids,
                       'ready': str(helper/'ready'), 'cancel': str(helper/'cancel')}
            package.write_json(helper/'request.json', request)
            flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
            subprocess.Popen([str(helper/'python/pythonw.exe'), str(helper/'apply_update.py'), str(helper/'request.json')],
                             cwd=helper, creationflags=flags, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 15
            while not (helper/'ready').exists():
                if time.monotonic() > deadline:
                    raise RuntimeError('Установщик не запустился. Программа не была закрыта.')
                time.sleep(.1)
            self.set(status='restarting', message='Закрываю RPTS. После обновления окно откроется снова…')
            package.log(self.root, 'handoff', build_id=release['build_id'])
            close_host(os.getppid())
        except Exception as exc:
            self.set(status='available', message='Не удалось установить обновление: '+str(exc), progress=None)
            if helper:
                try:
                    (helper/'cancel').touch()
                except OSError:
                    pass  # A full temporary drive must not leave the UI permanently busy.
            package.log(self.root, 'prepare_failed', error=str(exc))
        finally:
            reservation.__exit__(None, None, None)
