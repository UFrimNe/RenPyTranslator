"""Authenticated loopback API for the CEF desktop. No arbitrary shell execution."""
from __future__ import annotations
import argparse
import hashlib
import json
import mimetypes
import os
import re
import secrets
import shutil
import sqlite3
import string
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from contextlib import closing
from datetime import datetime,timezone
from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
from pathlib import Path
from .workspace import Workspace,OperationLock,read_json,write_json
from . import diagnostics

ACTIONS={'audit','translate','translate_ui','profiles','build','install','import_translation','report','launch','check',
         'capture_on','capture_off','import_runtime','coverage','uninstall','retranslate','restore','evaluate','unrpyc','google_translate','install_model'}
ACTIVE={'running','waiting_input','stopping'}

class Desktop:
    def __init__(self,root):
        self.ws=Workspace(root).initialize();self.app=self.ws.root/'app'
        self.token=secrets.token_urlsafe(32);diagnostics.remember_secrets([self.token])
        self.picker_lock=threading.Lock()
        self.lock=threading.RLock();self.job=None;self.process=None;self.server=None
        from .updater import Updater
        self.updater=Updater(self.ws.root)
        self.hardware=None;self.hardware_error=''
        self.history=[]
        for path in sorted(self.ws.jobs.glob('*/job.json'),reverse=True)[:20]:
            item=read_json(path)
            if item.get('status') in ACTIVE:
                item.update(status='interrupted',message='Предыдущий запуск прерван. Готовые строки сохранены; можно продолжить.')
                write_json(path,item)
            self.history.append(item)
        threading.Thread(target=self._hardware,daemon=True).start()
    def _hardware(self):
        try:
            from .local_ai import detect_hardware
            self.hardware=detect_hardware()
        except Exception as e:self.hardware_error=str(e)
    def busy(self):return bool(self.job and self.job['status'] in ACTIVE)
    def state(self):
        from .local_ai import MODEL_SPECS,installation_status,human_bytes,recommend_model,recommended_backend
        from .model_policy import uses_source_only_review
        with self.lock:
            settings=read_json(self.ws.settings/'local_ai_settings.json')
            preferences=read_json(self.ws.settings/'desktop.json')
            config=read_json(self.ws.settings/'translation_config.local_ai.json')
            selected=settings.get('model_key','');recommended=recommend_model(self.hardware).key if self.hardware else ''
            models=[]
            for spec in MODEL_SPECS.values():
                status=installation_status({**settings,'home':settings.get('home',str(self.ws.ai)),'model_key':spec.key,
                    'backend':settings.get('backend') or (recommended_backend(self.hardware) if self.hardware else 'cpu')})
                models.append({'key':spec.key,'title':spec.title,'size':human_bytes(spec.size_bytes),'ram':spec.min_ram_gb,
                    'vram':spec.recommended_vram_gb,'description':spec.quality,'installed':bool(status.get('model_present')),
                    'ready':bool(status.get('ready')),'recommended':spec.key==recommended})
            quality=config.get('quality',{})
            mode='balanced' if uses_source_only_review(config) else 'full_review' if quality.get('fidelity_pass') else 'reviewed' if quality.get('combined_review') else 'balanced'
            hw={'loading':self.hardware is None and not self.hardware_error,'error':self.hardware_error}
            if self.hardware:
                hw.update(ram=human_bytes(self.hardware.ram_bytes),gpus=[{'name':g.name,'memory':human_bytes(g.vram_bytes)} for g in self.hardware.gpus],backend=recommended_backend(self.hardware))
            return {'version':(self.app/'VERSION.txt').read_text(encoding='utf-8').strip() if (self.app/'VERSION.txt').exists() else 'dev',
                'update':self.updater.snapshot(),'projects':self.ws.projects_list(),'translations':self.ws.translations(),'models':models,
                'selected_model':selected or recommended,'configured_model':selected,'backend':settings.get('backend') or hw.get('backend','cpu'),
                'preferences':preferences,'mode':mode,'job':self.job,'history':self.history[:10],'hardware':hw,
                'notes':config.get('notes',''),'glossary':config.get('glossary',{}),
                'paths':{'projects':str(self.ws.projects),'translations':str(self.ws.library),'models':str(settings.get('home',self.ws.ai)),'reports':str(self.ws.reports)},
                'warnings':read_json(self.ws.data/'layout_migration.json').get('warnings',[]),
                'report_available':(self.ws.reports/'RenPyAuditReport.zip').is_file()}
    def _save_job(self):
        if self.job:write_json(self.ws.jobs/self.job['id']/'job.json',self.job)
    def start(self,action,identity='',params=None):
        if action not in ACTIONS:raise ValueError('Неизвестная операция.')
        if action not in {'report','unrpyc','install_model'}:self.ws.project(identity)
        with self.lock:
            if self.updater.active():raise ValueError('Дождись установки обновления и перезапуска RPTS.')
            if self.busy():raise ValueError('Дождись текущей операции или останови её.')
            stamp=datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+secrets.token_hex(4)
            folder=self.ws.jobs/stamp;folder.mkdir()
            task={'action':action,'project_id':identity,'params':params or {}}
            self.job={'id':stamp,**task,'status':'running','phase':'Подготовка','progress':None,'done':0,'total':0,
                'errors':0,'started_at':time.time(),'logs':[],'input':None,'message':''}
            self._save_job()
            env=os.environ.copy();env.update(PYTHONUTF8='1',PYTHONIOENCODING='utf-8',PYTHONUNBUFFERED='1',RPTS_CANCEL_FILE=str(folder/'cancel'))
            flags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0
            try:
                self.process=subprocess.Popen([sys.executable,'-u',str(self.app/'tools/desktop_worker.py')],cwd=self.ws.root,env=env,
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding='utf-8',errors='replace',bufsize=1,creationflags=flags)
                self.process.stdin.write(json.dumps(task,ensure_ascii=False)+'\n');self.process.stdin.flush()
            except BaseException as e:
                self.job.update(status='failed',message=str(e));self._save_job();raise
            threading.Thread(target=self._listen,args=(self.process,folder),daemon=True).start()
            return {'id':stamp}
    def _listen(self,process,folder):
        try:
            with (folder/'console.log').open('w',encoding='utf-8') as log:
                for raw in process.stdout:
                    line=diagnostics.redact(raw.rstrip())
                    log.write(line+'\n');log.flush()
                    with self.lock:
                        if line.startswith('@@RPTS '):
                            try:event=json.loads(line[7:])
                            except ValueError:event={}
                            kind=event.get('type')
                            if kind=='phase':self.job.update(phase=event['label'],progress=None,eta='')
                            elif kind=='input':self.job.update(status='waiting_input',input=event.get('prompt',''))
                            elif kind=='result':self.job.update(result=event['result'],message=event['result'].get('message',''))
                            elif kind in {'failure','cancelled'}:self.job.update(message=event.get('message',''))
                        elif line.strip():
                            self.job['logs']=(self.job['logs']+[line])[-160:]
                            progress=re.search(r'completed (\d+)/(\d+).*errors (\d+).*ETA ([\d:-]+)',line)
                            if progress:
                                done,total,errors,eta=progress.groups();total=int(total)
                                self.job.update(done=int(done),total=total,errors=int(errors),eta=eta,
                                    progress=round(100*(int(done)+int(errors))/total,1) if total else 100)
                            elif '[CHARACTERS]' in line:self.job.update(phase='Профили персонажей',detail=line.replace('[CHARACTERS]','').strip())
                            elif re.search(r'\((\d+(?:\.\d+)?)%\)',line):self.job['progress']=min(100,float(re.search(r'\((\d+(?:\.\d+)?)%\)',line)[1]))
                        self._save_job()
            rc=process.wait()
            with self.lock:
                self.job.update(status='cancelled' if rc in {130,-2} else 'failed' if rc else 'completed',finished_at=time.time(),input=None,return_code=rc)
                if not self.job['message']:self.job['message']='Готово.' if rc==0 else 'Операция не завершена. Подробности в журнале.'
                self._save_job();self.history.insert(0,dict(self.job));self.history=self.history[:20]
        except Exception as e:
            with self.lock:self.job.update(status='failed',message=str(e));self._save_job()
        finally:
            if process.stdout:process.stdout.close()
            if process.stdin:process.stdin.close()
    def cancel(self):
        with self.lock:
            if not self.busy():return {'stopped':True}
            (self.ws.jobs/self.job['id']/'cancel').touch()
            if self.job['status']=='waiting_input':
                try:self.process.stdin.write('{"cancel":true}\n');self.process.stdin.flush()
                except (OSError,ValueError):pass
            self.job.update(status='stopping',input=None,message='Завершаю текущий запрос и сохраняю результат…');self._save_job()
            return {'stopping':True}
    def respond(self,value):
        with self.lock:
            if not self.job or self.job['status']!='waiting_input':raise ValueError('Программа сейчас не ожидает ответа.')
            self.process.stdin.write(json.dumps({'value':str(value)})+'\n');self.process.stdin.flush()
            self.job.update(status='running',input=None);self._save_job()
            return {'ok':True}
    def mutate(self,route,data):
        if route=='/api/update/check':return self.updater.check()
        if route=='/api/update/install':
            with self.lock:
                if self.busy():raise ValueError('Сначала заверши текущую операцию. Перевод и готовые строки сохранятся.')
                return self.updater.begin()
        if route=='/api/pick-folder':
            if not self.picker_lock.acquire(blocking=False):raise ValueError('Окно выбора папки уже открыто.')
            try:
                from .native_folder import pick_folder
                return pick_folder(self.ws.root,data)
            finally:self.picker_lock.release()
        if route=='/api/job':return self.start(data.get('action'),data.get('project_id',''),data.get('params'))
        if route=='/api/cancel':return self.cancel()
        if route=='/api/respond':return self.respond(data.get('value',''))
        if route=='/api/shutdown':
            if self.busy():self.cancel();return {'busy':True}
            threading.Thread(target=self.server.shutdown,daemon=True).start();return {'busy':False}
        with self.lock:
            if self.updater.active():raise ValueError('Идёт обновление RPTS. Дождись перезапуска.')
            if self.busy() and route not in {'/api/open'}:raise ValueError('Сначала дождись завершения текущей операции.')
            if route=='/api/open':return self._mutate_idle(route,data)
            with OperationLock(self.ws.data/'operation.lock'):
                return self._mutate_idle(route,data)
    def _mutate_idle(self,route,data):
        if route=='/api/games':
            identity=self.ws.add_game(data.get('path',''))
            preferences=read_json(self.ws.settings/'desktop.json');preferences['active_project']=identity
            write_json(self.ws.settings/'desktop.json',preferences);return {'id':identity}
        if route=='/api/select':
            self.ws.project(data['id']);preferences=read_json(self.ws.settings/'desktop.json');preferences['active_project']=data['id']
            write_json(self.ws.settings/'desktop.json',preferences);return {'ok':True}
        if route=='/api/settings':
            from .local_ai import MODEL_SPECS,save_settings,make_translation_config,detect_hardware
            from .model_policy import model_family
            key=data.get('model');backend=data.get('backend');mode=data.get('mode','balanced')
            if key not in MODEL_SPECS or backend not in {'cpu','cuda','vulkan'} or mode not in {'balanced','reviewed','full_review'}:raise ValueError('Некорректные настройки модели.')
            path=self.ws.settings/'local_ai_settings.json';settings=read_json(path)
            changed=settings.get('model_key')!=key
            settings.update(model_key=key,backend=backend);settings.setdefault('home',str(self.ws.ai))
            config_path=self.ws.settings/'translation_config.local_ai.json';old=read_json(config_path)
            config=make_translation_config(MODEL_SPECS[key],hw=self.hardware or detect_hardware(),backend=backend) if changed or not old else old
            if changed:
                for field in ('notes','glossary'):config[field]=old.get(field,config.get(field,'' if field=='notes' else {}))
            quality=config.setdefault('quality',{})
            quality.update(text_mode=mode,combined_review=mode=='reviewed',editorial_pass=mode=='full_review',fidelity_pass=mode=='full_review',native_free_review=mode!='balanced' if model_family({'model':key})=='hy_mt2' else False)
            if 'notes' in data:config['notes']=str(data['notes'])[:8000]
            if 'glossary' in data:
                if not isinstance(data['glossary'],dict) or any(not isinstance(k,str) or not isinstance(v,str) for k,v in data['glossary'].items()):raise ValueError('Глоссарий должен содержать пары «исходник»: «перевод».')
                config['glossary']=data['glossary']
            save_settings(path,settings)
            write_json(config_path,config);write_json(self.ws.settings/'menu_preferences.json',{'text_mode':mode})
            return {'ok':True}
        if route=='/api/open':
            target=data.get('target');identity=data.get('project_id')
            paths={'projects':self.ws.projects,'translations':self.ws.library,'models':Path(read_json(self.ws.settings/'local_ai_settings.json').get('home',self.ws.ai)),
                'reports':self.ws.reports,'report':self.ws.reports/'RenPyAuditReport.zip','data':self.ws.data}
            if identity:paths.update(game=self.ws.game(identity),project=self.ws.project(identity),quality=self.ws.project(identity)/'work/quality')
            if target=='icon_credit':
                url='https://www.flaticon.com/uicons'
                if os.name=='nt':os.startfile(url)
                else:subprocess.Popen(['xdg-open',url],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                return {'ok':True}
            if target=='console':
                if self.busy():raise ValueError('Сначала заверши текущую операцию.')
                if os.name!='nt':raise ValueError('Консольный запуск в этой сборке предназначен для Windows.')
                subprocess.Popen([sys.executable,str(self.app/'tools/rpts_menu.py')],cwd=self.ws.root,creationflags=subprocess.CREATE_NEW_CONSOLE)
                return {'ok':True}
            path=paths.get(target)
            if path is None or not path.exists():raise ValueError('Этот файл или папка пока не созданы.')
            if os.name=='nt':os.startfile(str(path))
            else:subprocess.Popen(['xdg-open',str(path)],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return {'ok':True}
        if route=='/api/save-row':
            project=self.ws.project(data['project_id']);path=project/'work/translation_project.sqlite'
            if not path.is_file():raise ValueError('Сначала выполни анализ игры.')
            with closing(sqlite3.connect(path)) as db:
                db.row_factory=sqlite3.Row
                row=db.execute('SELECT * FROM segments WHERE segment_id=?',(data['id'],)).fetchone()
                if not row or row['updated_at']!=data.get('updated_at'):raise ValueError('Строка уже изменилась. Обнови список.')
                text=str(data.get('translation','')).strip()
                from .tokens import validate_preservation
                ok,why=validate_preservation(row['source_text'],text)
                if not text or not ok:raise ValueError(why or 'Перевод не может быть пустым.')
                backup=project/'work/backups'/('before-manual-'+str(time.time_ns())+'.sqlite');backup.parent.mkdir(parents=True,exist_ok=True)
                with closing(sqlite3.connect(backup)) as copy:db.backup(copy)
                db.execute("UPDATE segments SET translation=?,status='approved',provider='manual',model='',updated_at=? WHERE segment_id=?",(text,datetime.now(timezone.utc).isoformat(),row['segment_id']))
                db.commit()
            return {'ok':True}
        raise ValueError('Неизвестная команда.')
    def browse(self,path):
        if not path:
            roots=[Path(c+':\\') for c in string.ascii_uppercase if Path(c+':\\').is_dir()] if os.name=='nt' else [Path.home(),Path('/workspace'),Path('/tmp')]
            return {'path':'','parent':'','directories':[{'name':str(p),'path':str(p)} for p in roots if p.exists()]}
        current=Path(path).expanduser().resolve()
        if not current.is_dir():raise ValueError('Папка не найдена.')
        entries=[]
        for p in current.iterdir():
            try:
                if p.is_dir() and not p.name.startswith('.'):entries.append({'name':p.name,'path':str(p)})
            except OSError:continue
        entries.sort(key=lambda x:x['name'].lower())
        return {'path':str(current),'parent':str(current.parent) if current.parent!=current else '', 'directories':entries[:300], 'truncated':len(entries)>300}
    def rows(self,identity,status='',query='',offset=0):
        path=self.ws.project(identity)/'work/translation_project.sqlite'
        if not path.exists():return {'rows':[],'total':0}
        with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=.5)) as db:
            db.row_factory=sqlite3.Row
            where=[];args=[]
            if status=='error':where.append("status='error'")
            if query:where.append('(source_text LIKE ? OR translation LIKE ?)');args.extend(['%'+query+'%']*2)
            suffix=' WHERE '+' AND '.join(where) if where else ''
            total=db.execute('SELECT count(*) FROM segments'+suffix,args).fetchone()[0]
            rows=[dict(r) for r in db.execute('SELECT segment_id,source_text,translation,speaker,status,source_file,source_line,updated_at FROM segments'+suffix+' ORDER BY source_file,source_line LIMIT 30 OFFSET ?',args+[max(0,offset)])]
            return {'rows':rows,'total':total}

class Handler(BaseHTTPRequestHandler):
    server_version='RPTS';sys_version=''
    def log_message(self,*args):pass
    def reply(self,status,value,mime='application/json; charset=utf-8'):
        raw=json.dumps(value,ensure_ascii=False).encode() if not isinstance(value,bytes) else value
        self.send_response(status);self.send_header('Content-Type',mime);self.send_header('Content-Length',str(len(raw)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Content-Security-Policy',"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'")
        self.send_header('Referrer-Policy','no-referrer');self.end_headers();self.wfile.write(raw)
    def authorized(self):
        app=self.server.app;host=f'127.0.0.1:{self.server.server_port}'
        return self.headers.get('Host')==host and secrets.compare_digest(self.headers.get('Authorization',''),'Bearer '+app.token) and self.headers.get('Origin','http://'+host)=='http://'+host
    def do_GET(self):
        parsed=urllib.parse.urlsplit(self.path);path=parsed.path;app=self.server.app
        try:
            if self.headers.get('Host')!=f'127.0.0.1:{self.server.server_port}':return self.reply(403,{'error':'Недопустимый адрес.'})
            if path.startswith('/api/'):
                if not self.authorized():return self.reply(403,{'error':'Недействительная сессия. Перезапусти окно RPTS.'})
                query=urllib.parse.parse_qs(parsed.query);get=lambda k,d='':query.get(k,[d])[0]
                if path=='/api/state':return self.reply(200,app.state())
                if path=='/api/browse':return self.reply(200,app.browse(get('path')))
                if path=='/api/rows':return self.reply(200,app.rows(get('project_id'),get('status'),get('q'),int(get('offset','0'))))
                return self.reply(404,{'error':'Не найдено.'})
            files={'/':'index.html','/app.js':'app.js','/views.js':'views.js','/selects.js':'selects.js','/icons.js':'icons.js','/native-picker.js':'native-picker.js','/style.css':'style.css'}
            if path not in files:return self.reply(404,{'error':'Не найдено.'})
            file=app.app/'web'/files[path]
            self.reply(200,file.read_bytes(),mimetypes.guess_type(str(file))[0]+'; charset=utf-8')
        except Exception as e:self.reply(400,{'error':str(e)})
    def do_POST(self):
        try:
            if not self.authorized():return self.reply(403,{'error':'Недействительная сессия.'})
            size=int(self.headers.get('Content-Length','0'))
            if size<0 or size>128*1024:return self.reply(413,{'error':'Слишком большой запрос.'})
            data=json.loads(self.rfile.read(size) or b'{}')
            if not isinstance(data,dict):raise ValueError('Ожидался объект запроса.')
            self.reply(200,self.server.app.mutate(urllib.parse.urlsplit(self.path).path,data))
        except Exception as e:
            diagnostics.event('desktop_api_error',route=urllib.parse.urlsplit(self.path).path,error=str(e))
            self.reply(400,{'error':str(e)})


def serve(root,port=0):
    app=Desktop(root)
    with OperationLock(app.ws.data/'desktop.lock'):
        server=ThreadingHTTPServer(('127.0.0.1',port),Handler);server.daemon_threads=True;server.app=app;app.server=server
        print(f'RPTS_URL http://127.0.0.1:{server.server_port}/#{app.token}',flush=True)
        app.updater.check()
        try:server.serve_forever(poll_interval=.3)
        finally:server.server_close()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parents[3]);parser.add_argument('--port',type=int,default=0)
    args=parser.parse_args();serve(args.root,args.port)
