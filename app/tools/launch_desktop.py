"""Portable CEF launcher with persistent startup diagnostics, including pre-Main failures."""
from pathlib import Path
import ctypes,hashlib,json,os,platform,re,struct,subprocess,sys,time,traceback,zipfile
ROOT=Path(__file__).resolve().parents[2]
CRITICAL=(
 'app/tools/desktop_server.py','app/desktop/bin/RPTS.Desktop.exe','app/desktop/bin/RPTS.Desktop.dll',
 'app/desktop/bin/RPTS.Desktop.deps.json','app/desktop/bin/RPTS.Desktop.runtimeconfig.json',
 'app/desktop/bin/CefSharp.dll','app/desktop/bin/CefSharp.Core.dll','app/desktop/bin/CefSharp.Core.Runtime.dll',
 'app/desktop/bin/CefSharp.WinForms.dll','app/desktop/bin/CefSharp.BrowserSubprocess.Core.dll','app/desktop/bin/Ijwhost.dll',
 'app/desktop/bin/libcef.dll','app/desktop/bin/chrome_elf.dll','app/desktop/bin/resources.pak',
 'app/desktop/bin/icudtl.dat','app/desktop/bin/v8_context_snapshot.bin',
 'runtime/dotnet/dotnet.exe','runtime/python/python.exe')

def redact(text):
    text=re.sub(r'(?i)(Bearer\s+)[^\s\"\x27,;]+',r'\1[redacted]',str(text))
    text=re.sub(r'(RPTS_URL\s+[^\s#]+#)[^\s]+',r'\1[redacted]',text)
    return re.sub(r'''(?i)((?:["']?(?:api[_-]?key|password|token|secret)["']?)\s*[:=]\s*["']?)[^"'\s,;]+''',r'\1[redacted]',text)

def record(root,event,**fields):
    path=Path(root)/'data/logs/launcher.log';path.parent.mkdir(parents=True,exist_ok=True)
    value={'time':time.strftime('%Y-%m-%d %H:%M:%S'),'event':event,**fields}
    with path.open('a',encoding='utf-8') as f:f.write(redact(json.dumps(value,ensure_ascii=False))+'\n')

def installation_status(root):
    root=Path(root);expected={}
    try:expected={x['path']:x for x in json.loads((root/'app/release_manifest.json').read_text(encoding='utf-8'))['files']}
    except (OSError,ValueError,KeyError,TypeError):pass
    rows=[]
    for rel in CRITICAL:
        path=root/rel;item={'path':rel,'exists':path.is_file()}
        if item['exists']:
            item['bytes']=path.stat().st_size
            if rel in expected:item['expected_bytes']=expected[rel]['bytes']
            if path.stat().st_size and path.stat().st_size<16*1024*1024 and rel.startswith('app/desktop/bin/'):
                with path.open('rb') as f:item['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
                if rel in expected:item['expected_sha256']=expected[rel]['sha256']
        rows.append(item)
    return {'version':(root/'app/VERSION.txt').read_text(encoding='utf-8').strip() if (root/'app/VERSION.txt').exists() else '',
            'python':sys.version,'python_bits':struct.calcsize('P')*8,'machine':platform.machine(),'platform':platform.system()+' '+platform.release(),'files':rows}

def preflight(root):
    status=installation_status(root);issues=[]
    for item in status['files']:
        if not item['exists'] or item.get('bytes')==0:issues.append('Нет файла: '+item['path'])
        elif item.get('expected_bytes',item['bytes'])!=item['bytes']:issues.append('Неверный размер: '+item['path'])
        elif item.get('expected_sha256',item.get('sha256'))!=item.get('sha256'):issues.append('Файл изменён или повреждён: '+item['path'])
    if issues:raise RuntimeError('Обновление распаковано не полностью или файлы повреждены.\n'+'\n'.join(issues[:6])+'\nПолностью распакуй исправление с заменой файлов.')
    return status

def ensure_vcruntime(root):
    try:ctypes.WinDLL('msvcp140.dll');ctypes.WinDLL('vcruntime140_1.dll')
    except OSError:
        installer=Path(root)/'runtime/installers/VC_redist.x64.exe'
        result=subprocess.run([str(installer),'/install','/norestart'])
        if result.returncode not in (0,1638,3010):raise RuntimeError('Установка Visual C++ Runtime не завершена. Код: '+str(result.returncode))

def run_desktop(root,diagnostic=False):
    root=Path(root).resolve();logs=root/'data/logs';logs.mkdir(parents=True,exist_ok=True)
    record(root,'preflight',diagnostic=diagnostic)
    status=preflight(root)
    (logs/'startup_inventory.json').write_text(json.dumps(status,ensure_ascii=False,indent=2),encoding='utf-8')
    env=os.environ.copy();runtime=str(root/'runtime/dotnet')
    env.update(DOTNET_ROOT=runtime,DOTNET_ROOT_X64=runtime,DOTNET_MULTILEVEL_LOOKUP='0')
    if diagnostic:env.update(COREHOST_TRACE='1',COREHOST_TRACE_VERBOSITY='3')
    executable=root/'app/desktop/bin/RPTS.Desktop.exe';started=time.monotonic();process_log=logs/'desktop-process.log'
    # Capture even hostfxr/CLR failures that happen before the C# Main catch block.
    with process_log.open('ab') as output:
        output.write(('\n=== RPTS launch '+time.strftime('%Y-%m-%d %H:%M:%S')+' ===\n').encode());output.flush()
        process=subprocess.Popen([str(executable)],cwd=root,env=env,stdout=output,stderr=subprocess.STDOUT,
                                 creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        record(root,'process_started',pid=process.pid)
        code=process.wait()
    record(root,'process_exit',code=code,hex=f'0x{code & 0xffffffff:08X}',seconds=round(time.monotonic()-started,2))
    if code:
        raise RuntimeError(f'Графическое окно RPTS завершилось с кодом 0x{code & 0xffffffff:08X}.\n'
                           'Подробности: data\\logs\\desktop-process.log\nДля отчёта запусти RPTS-Debug.bat.')
    return 0

def startup_report(root):
    root=Path(root);out=root/'data/reports/RenPyStartupReport.zip';out.parent.mkdir(parents=True,exist_ok=True)
    temp=out.with_suffix('.zip.tmp')
    with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('startup_inventory.json',json.dumps(installation_status(root),ensure_ascii=False,indent=2))
        for name in ['launcher.log','desktop-process.log','desktop-host.log','desktop-backend.log','cef.log','native-folder.log']:
            path=root/'data/logs'/name
            if path.is_file():
                with path.open('rb') as f:
                    f.seek(max(0,path.stat().st_size-2*1024*1024));text=f.read().decode('utf-8',errors='replace')
                z.writestr('logs/'+name,redact(text))
        for name in ['RPTS.Desktop.deps.json','RPTS.Desktop.runtimeconfig.json','RPTS.FolderPicker.runtimeconfig.json']:
            p=root/'app/desktop/bin'/name
            if p.is_file():z.writestr('host/'+name,p.read_bytes())
    os.replace(temp,out);return out

def announce(text,error=False):
    stream=sys.stderr if error else sys.stdout
    if stream is not None:print(text,file=stream,flush=True)

def main(argv=None):
    args=sys.argv[1:] if argv is None else argv;diagnostic='--diagnostic' in args;code=0
    try:
        if '--report-only' in args:
            announce('Отчёт запуска: '+str(startup_report(ROOT)));return 0
        if os.name!='nt':raise RuntimeError('Эта сборка предназначена для Windows 10/11 x64.')
        record(ROOT,'launcher_start')
        if diagnostic:announce('RPTS: проверяю файлы и запускаю графическое окно. Журналы: data/logs')
        ensure_vcruntime(ROOT)
        code=run_desktop(ROOT,diagnostic)
    except Exception as ex:
        code=1
        try:record(ROOT,'launch_failed',error=str(ex),traceback=traceback.format_exc())
        except OSError:pass
        message=str(ex)
        try:message+='\n\nОтчёт запуска: '+str(startup_report(ROOT))
        except Exception:message+='\n\nЗапусти RPTS-Debug.bat для диагностики.'
        if diagnostic:announce(message,True)
        elif os.name=='nt':ctypes.windll.user32.MessageBoxW(None,message,'RPTS: запуск не удался',16)
    finally:
        if '--make-report' in args:
            try:announce('Отчёт запуска: '+str(startup_report(ROOT)))
            except Exception as ex:announce('Не удалось собрать отчёт: '+str(ex),True)
    return code
if __name__=='__main__':raise SystemExit(main())
