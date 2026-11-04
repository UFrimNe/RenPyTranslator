"""Standard Windows picker in an isolated .NET process, outside the CEF host."""
from __future__ import annotations
import ctypes,json,os,subprocess
from pathlib import Path

def parent_window(pid):
    if os.name!='nt':return 0
    from ctypes import wintypes
    user32=ctypes.WinDLL('user32',use_last_error=True)
    callback_type=ctypes.WINFUNCTYPE(wintypes.BOOL,wintypes.HWND,wintypes.LPARAM)
    user32.EnumWindows.argtypes=[callback_type,wintypes.LPARAM];user32.EnumWindows.restype=wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes=[wintypes.HWND,ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes=[wintypes.HWND];user32.IsWindowVisible.restype=wintypes.BOOL
    found=[]
    @callback_type
    def inspect(hwnd,param):
        owner=wintypes.DWORD();user32.GetWindowThreadProcessId(hwnd,ctypes.byref(owner))
        if owner.value==pid and user32.IsWindowVisible(hwnd):found.append(int(hwnd));return False
        return True
    user32.EnumWindows(inspect,0)
    return found[0] if found else 0

def pick_folder(root,data):
    if os.name!='nt':raise ValueError('Системный выбор папки доступен в Windows. Можно вставить путь вручную.')
    kind=data.get('kind');initial=data.get('initial','')
    if kind not in {'game','translation'} or not isinstance(initial,str) or len(initial)>2048:raise ValueError('Неверные параметры выбора папки.')
    root=Path(root);runtime=root/'runtime/dotnet/dotnet.exe';helper=root/'app/desktop/bin/RPTS.FolderPicker.dll'
    if not runtime.is_file() or not helper.is_file():raise ValueError('Компонент выбора папки не найден. Полностью распакуй обновление или введи путь вручную.')
    env=os.environ.copy();env.update(DOTNET_ROOT=str(root/'runtime/dotnet'),DOTNET_ROOT_X64=str(root/'runtime/dotnet'),DOTNET_MULTILEVEL_LOOKUP='0')
    result=subprocess.run([str(runtime),str(helper)],input=json.dumps({'kind':kind,'initial':initial,'owner':parent_window(os.getppid())}),
                          capture_output=True,text=True,encoding='utf-8',errors='replace',cwd=root,env=env,
                          creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    try:reply=json.loads(result.stdout)
    except (ValueError,TypeError):reply={}
    if result.returncode or not isinstance(reply,dict) or reply.get('error') or 'path' not in reply:
        log=root/'data/logs/native-folder.log';log.parent.mkdir(parents=True,exist_ok=True)
        with log.open('a',encoding='utf-8') as f:f.write(f'Exit {result.returncode}\n{result.stderr}\n{result.stdout}\n')
        raise ValueError('Не удалось открыть системный выбор папки. Введи путь вручную; подробности: data/logs/native-folder.log.')
    if reply['path'] is not None and not isinstance(reply['path'],str):raise ValueError('Диалог вернул неверный путь.')
    return reply
