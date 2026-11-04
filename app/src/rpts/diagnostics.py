"""Local diagnostics only: bounded logs, exact AI exchanges, no credentials."""
from __future__ import annotations

import json
import os
import platform
import re
import sys
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.RLock()
_warned = False
_secrets: set[str] = set()


def remember_secrets(values) -> None:
    with _lock:
        for value in values:
            if isinstance(value, str) and len(value) >= 6:
                _secrets.add(value)
                if value.lower().startswith('bearer '):
                    _secrets.add(value[7:])


def redact(value):
    if isinstance(value, dict):
        return {str(k): '[REDACTED]' if re.search(r'(?i)(api[-_]?key|authorization|password|secret|access[-_]?token|runtime[-_]?api[-_]?key)', str(k))
                and not str(k).endswith('_env') else redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if not isinstance(value, str):
        return value
    with _lock:
        secrets = tuple(_secrets)
    for secret in secrets:
        value = value.replace(secret, '[REDACTED]')
    value = re.sub(r'(?i)(Bearer\s+)[A-Za-z0-9._~+/=-]+', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)((?:Proxy-)?Authorization\s*[:=]\s*(?:Basic|Bearer)\s+)\S+', r'\1[REDACTED]', value)
    value = re.sub(r'''(?ix)((?:--api-key|api[_-]?key|password|access[_-]?token|secret)["']?\s*[:=]\s*["']?)[^\s"'&,}\[\]]+''', r'\1[REDACTED]', value)
    value = re.sub(r'''(?i)(--api-key\s+)(?:"[^"]*"|'[^']*'|\S+)''', r'\1[REDACTED]', value)
    value = re.sub(r'(?i)(https?://)[^/@\s]+:[^/@\s]+@', r'\1[REDACTED]@', value)
    return value


def _write(path: Path, text: str, *, limit: int = 20 * 1024 * 1024, keep: int = 3) -> None:
    global _warned
    try:
        with _lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size >= limit:
                for index in range(keep, 0, -1):
                    previous = path if index == 1 else path.with_name(path.name + f'.{index - 1}')
                    if previous.exists():
                        previous.replace(path.with_name(path.name + f'.{index}'))
            with path.open('a', encoding='utf-8') as stream:
                stream.write(text)
    except (OSError, ValueError) as exc:
        if not _warned:
            _warned = True
            print(f'[WARN] Диагностика не записана: {type(exc).__name__}: {exc}', file=sys.__stderr__)


def event(kind: str, *, directory=None, stream='events.jsonl', **details) -> None:
    directory = directory or os.environ.get('RPTS_DIAGNOSTICS_DIR')
    if not directory:
        return
    record = {'time_utc': datetime.now(timezone.utc).isoformat(), 'session_id': os.environ.get('RPTS_SESSION_ID', ''),
              'command_id': os.environ.get('RPTS_COMMAND_ID', ''),
              'pid': os.getpid(), 'thread': threading.current_thread().name, 'event': kind, **details}
    _write(Path(directory) / stream, json.dumps(redact(record), ensure_ascii=False, default=str) + '\n')


def console(text: str, *, directory=None) -> None:
    directory = directory or os.environ.get('RPTS_DIAGNOSTICS_DIR')
    if directory:
        _write(Path(directory) / 'console.log', redact(text), limit=10 * 1024 * 1024, keep=2)


def begin_session(root: Path, version: str) -> Path:
    session = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ') + '-' + uuid.uuid4().hex[:8]
    directory = root / 'diagnostics' / 'sessions' / session
    os.environ['RPTS_SESSION_ID'] = session
    os.environ['RPTS_DIAGNOSTICS_DIR'] = str(directory)
    remember_secrets(v for k, v in os.environ.items() if re.search(r'(?i)(api.?key|password|secret|token)', k))
    event('session_start', version=version, python=sys.version, executable=sys.executable,
          platform=platform.platform(), architecture=platform.machine(), cpu_count=os.cpu_count(), cwd=str(root))
    return directory


def exception(kind='exception', **details) -> None:
    event(kind, traceback=traceback.format_exc(), **details)


def http_start(url, body, headers, timeout):
    remember_secrets(v for k, v in headers.items() if re.search(r'(?i)(authorization|key|token)', k))
    request_id = uuid.uuid4().hex
    event('http_request', stream='ai_requests.jsonl', request_id=request_id, url=url, timeout=timeout, request=body)
    return request_id, time.monotonic()


def http_end(trace, *, response=None, error=None, status=None):
    request_id, started = trace
    event('http_response', stream='ai_requests.jsonl', request_id=request_id, elapsed_seconds=round(time.monotonic()-started, 4),
          status=status, response=response, error=error)
