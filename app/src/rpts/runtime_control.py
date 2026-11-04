"""Cooperative desktop cancellation. Completed database writes remain committed."""
import os
from pathlib import Path

def cancel_requested():
    value=os.environ.get('RPTS_CANCEL_FILE')
    return bool(value and Path(value).is_file())

def check_cancelled():
    if cancel_requested():raise KeyboardInterrupt('Остановка запрошена пользователем')
