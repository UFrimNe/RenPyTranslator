"""Portable workspace, safe legacy relocation, and disk-backed project catalogue."""
from __future__ import annotations
import hashlib
import json
import os
import re
import shutil
import sqlite3
import time
from contextlib import closing
from pathlib import Path

READY = {'translated', 'translated_tm', 'approved'}
TRANSLATION_METHODS = {'local_ai', 'google_free'}

def translation_sources(path):
    if not Path(path).is_file(): return []
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True,timeout=.3)) as db:
            return [{'provider':p or 'unknown','model':m or '', 'rows':n} for p,m,n in db.execute(
                "SELECT provider,model,count(*) FROM segments WHERE status IN ('translated','translated_tm','approved') "
                "AND translation<>'' GROUP BY provider,model ORDER BY provider,model")]
    except sqlite3.Error: return []

def source_label(sources):
    labels=[]
    for item in sources:
        provider=item['provider']
        label=('Google Translate' if provider in {'google_free','google_translate'} else
               'Ручные правки' if provider=='manual' else
               item.get('model') or 'Локальный ИИ' if provider in {'openai_compatible','ollama'} else
               'Другой перевод')
        if label not in labels:labels.append(label)
    return ' + '.join(labels) if labels else ''


def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding='utf-8-sig'))
    except (OSError, ValueError):
        return {} if default is None else default

def write_json(path, data):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    os.replace(temporary, path)

def db_metadata(path):
    if not Path(path).is_file(): return {}
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.3)) as db:
            result = {}
            for key, value in db.execute('SELECT key,value FROM metadata'):
                try: result[key] = json.loads(value)
                except ValueError: result[key] = value
            return result
    except sqlite3.Error: return {}

def counts(path):
    result = {'total':0, 'ready':0, 'pending':0, 'errors':0, 'runtime':0, 'percent':0,
              'phone_total':0, 'phone_ready':0, 'ui_total':0, 'ui_ready':0}
    if not Path(path).is_file(): return result
    try:
        with closing(sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro', uri=True, timeout=.3)) as db:
            states = dict(db.execute('SELECT status,count(*) FROM segments GROUP BY status'))
            from .ui_project import UI_KINDS
            for origin,kind,status,amount in db.execute(
                    "SELECT origin,kind,status,count(*) FROM segments WHERE channel='semantic' "
                    "AND source_text<>'' GROUP BY origin,kind,status"):
                group='phone' if origin=='interactive' else 'ui' if kind in UI_KINDS else ''
                if group:
                    result[group+'_total']+=amount
                    if status in READY:result[group+'_ready']+=amount
        result.update(total=sum(states.values()),ready=sum(states.get(s,0) for s in READY),
                      pending=states.get('pending',0),errors=states.get('error',0),runtime=states.get('runtime_pending',0))
        result['percent'] = round(100*result['ready']/result['total'],1) if result['total'] else 0
    except sqlite3.Error as e: result['db_error'] = str(e)
    return result

def game_folder(value):
    p = Path(str(value).strip().strip('"')).expanduser().resolve()
    if p.is_file(): p=p.parent
    if (p/'game').is_dir(): p=p/'game'
    if not p.is_dir() or not any(x.is_file() and x.suffix.lower() in {'.rpa','.rpy','.rpyc'} for x in p.rglob('*')) and not (p/'script.rpy').exists():
        raise ValueError('Выбери папку Ren’Py-игры, её EXE или папку game с .rpa/.rpy/.rpyc.')
    return p

def file_sha256(path):
    digest=hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda:stream.read(1024*1024),b''):digest.update(block)
    return digest.hexdigest()

def project_id(game):
    game = Path(game).resolve()
    slug = re.sub(r'[^A-Za-z0-9._-]+','_',game.parent.name or game.name).strip('._-')[:40] or 'game'
    return slug+'-'+hashlib.sha1(str(game).lower().encode('utf-8','replace')).hexdigest()[:10]

class OperationLock:
    def __init__(self, path): self.path=Path(path); self.file=None;self.running=None
    def __enter__(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        self.file=self.path.open('a+b')
        try:
            if self.path.stat().st_size==0:
                self.file.write(b'0');self.file.flush()
            self.file.seek(0)
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(self.file.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
            from .update_package import RunningMutex
            self.running=RunningMutex().__enter__()
        except OSError:
            self.file.close();self.file=None
            raise RuntimeError('Уже выполняется другая операция RPTS. Дождись её завершения или закрой второе окно.')
        return self
    def __exit__(self,*args):
        if self.file:
            if os.name=='nt':
                import msvcrt
                self.file.seek(0);msvcrt.locking(self.file.fileno(),msvcrt.LK_UNLCK,1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(),fcntl.LOCK_UN)
            self.file.close()
            if self.running:self.running.__exit__(*args)

class Workspace:
    def __init__(self,root):
        self.root=Path(root).resolve();self.data=self.root/'data';self.settings=self.data/'settings'
        self.projects=self.data/'projects';self.library=self.data/'translations';self.reports=self.data/'reports'
        self.jobs=self.data/'jobs';self.runtime=self.root/'runtime';self.ai=self.runtime/'local_ai'
    def initialize(self):
        self.data.mkdir(parents=True,exist_ok=True)
        with OperationLock(self.data/'layout.lock'):
            journal=read_json(self.data/'layout_migration.json',{'moves':[],'warnings':[]})
            moves=[('settings',self.settings),('projects',self.projects),('reports',self.reports),('work',self.data/'work'),('diagnostics',self.data/'diagnostics'),('local_ai',self.ai)]
            for name,target in moves:
                old=self.root/name
                if old.exists() and not target.exists():
                    # Intent is durable before the atomic same-volume rename.
                    entry={'from':str(old),'to':str(target)}
                    if entry not in journal['moves']:journal['moves'].append(entry)
                    write_json(self.data/'layout_migration.json',journal)
                    target.parent.mkdir(parents=True,exist_ok=True);old.rename(target)
                elif old.exists() and target.exists():
                    warning=f'Сохранены обе папки: {old} и {target}. Автоматического объединения нет.'
                    if warning not in journal['warnings']:journal['warnings'].append(warning)
            for folder in (self.settings,self.projects,self.library,self.reports,self.jobs,self.ai):folder.mkdir(parents=True,exist_ok=True)
            legacy_settings=self.root/'local_ai_settings.json'
            if legacy_settings.is_file() and not (self.settings/legacy_settings.name).exists():
                shutil.copy2(legacy_settings,self.settings/legacy_settings.name)
            # Recover the older single-project layout, preserving its original copy.
            legacy_db=self.data/'work/translation_project.sqlite'
            legacy_game=db_metadata(legacy_db).get('game_dir')
            if legacy_game:
                dest=self.projects/project_id(legacy_game)
                if not dest.exists():
                    stage=self.projects/(dest.name+'.migrating')
                    if stage.exists():shutil.rmtree(stage)
                    stage.mkdir();shutil.copytree(legacy_db.parent,stage/'work')
                    if db_metadata(self.reports/'translation_project.sqlite').get('game_dir')==legacy_game:
                        shutil.copytree(self.reports,stage/'reports')
                    write_json(stage/'project.json',{'title':Path(legacy_game).parent.name,'game_dir':legacy_game})
                    stage.rename(dest)
            # Rewrite only known configuration paths, never dialogue/source text.
            def relocate(value):
                if not isinstance(value,str):return value
                for move in journal['moves']:
                    old=move['from'];new=move['to']
                    if value==old or value.startswith(old+os.sep):return new+value[len(old):]
                    rel=str(Path(old).relative_to(self.root))
                    if value==rel or value.startswith(rel+'/') or value.startswith(rel+'\\'):
                        return str(Path(new)/value[len(rel):].lstrip('/\\'))
                return value
            path_keys={'home','quality_dir','character_profiles_file','database','output_dir','backup_dir'}
            def repair(value):
                if isinstance(value,dict):return {k:relocate(v) if k in path_keys else repair(v) for k,v in value.items()}
                if isinstance(value,list):return [repair(v) for v in value]
                return value
            for p in self.settings.glob('*.json'):
                data=read_json(p);fixed=repair(data)
                if fixed!=data:
                    backup=p.with_suffix(p.suffix+'.before-layout')
                    if not backup.exists():shutil.copy2(p,backup)
                    write_json(p,fixed)
            write_json(self.data/'layout_migration.json',journal)
        return self
    def project_paths(self):
        result={}
        for base in (self.projects,self.root/'projects'):
            if base.is_dir():
                for p in sorted(base.iterdir()):
                    if p.is_dir() and not p.name.endswith('.migrating'):result.setdefault(p.name,p.resolve())
        return list(result.values())
    def projects_list(self):
        result=[]
        for p in self.project_paths():
            info=read_json(p/'project.json');db=p/'work/translation_project.sqlite'
            meta=db_metadata(db if db.exists() else p/'reports/translation_project.sqlite')
            game=str(info.get('game_dir') or meta.get('game_dir') or '')
            if not game:continue
            item={'id':p.name,'title':info.get('title') or Path(game).parent.name,'game_dir':game,'path':str(p),
                  'available':Path(game).is_dir(),'translation_method':info.get('translation_method','local_ai'),'model':info.get('model',''),'updated_at':info.get('updated_at',''),
                  'audited':db.is_file(),'installed':(Path(game)/'rpts_installed_manifest.json').is_file(),**counts(db)}
            # The actual installer filename is shared with the patch backend.
            from .patch_project import MANIFEST_NAME
            item['installed']=(Path(game)/MANIFEST_NAME).is_file()
            result.append(item)
        return result
    def project(self,identity):
        if not isinstance(identity,str) or not re.fullmatch(r'[A-Za-z0-9._-]+',identity) or identity in {'.','..'}:
            raise ValueError('Некорректный идентификатор проекта.')
        for p in self.project_paths():
            if p.name==identity:return p
        raise ValueError('Проект не найден. Добавь игру в библиотеку.')
    def set_translation_method(self,identity,method):
        if method not in TRANSLATION_METHODS:raise ValueError('Неизвестный способ перевода.')
        path=self.project(identity)/'project.json';info=read_json(path)
        info['translation_method']=method;write_json(path,info)

    def add_game(self,value):
        game=game_folder(value)
        for item in self.projects_list():
            if Path(item['game_dir']).resolve()==game:return item['id']
        identity=project_id(game);project=self.projects/identity;project.mkdir(parents=True,exist_ok=True)
        write_json(project/'project.json',{'title':game.parent.name,'game_dir':str(game),'created_at':time.time()})
        return identity
    def game(self,identity):
        project=self.project(identity);info=read_json(project/'project.json')
        game=info.get('game_dir') or db_metadata(project/'work/translation_project.sqlite').get('game_dir') or db_metadata(project/'reports/translation_project.sqlite').get('game_dir')
        if not game:raise ValueError('Путь игры неизвестен.')
        return Path(game)
    def translations(self):
        result=[]
        for p in sorted(self.library.glob('*/translation.json'),reverse=True):
            info=read_json(p)
            if info.get('id')==p.parent.name:result.append(info)
        saved={str(x.get('source_patch')) for x in result}
        for game in self.projects_list():
            patch=Path(game['path'])/'work/patch';manifest=read_json(patch/'patch_manifest.json')
            if manifest and str(patch) not in saved:
                result.append({'id':'working:'+game['id'],'project_id':game['id'],'title':game['title'],'created_at':manifest.get('created_at',''),
                    'ready':manifest.get('translated_rows',0),'total':game['total'],'language':manifest.get('renpy_language','russian'),
                    'model':game['model'],'sources':translation_sources(Path(game['path'])/'work/translation_project.sqlite'),'source_patch':str(patch),'working':True})
        return result
    def translation_path(self,identity):
        if isinstance(identity,str) and identity.startswith('working:'):return self.project(identity[8:])/'work/patch'
        if not isinstance(identity,str) or not re.fullmatch(r'[a-f0-9]{24}',identity):raise ValueError('Перевод не найден.')
        p=self.library/identity
        if not (p/'translation.json').is_file():raise ValueError('Перевод не найден.')
        return p/'patch'
    def save_translation(self,project_id,patch,model=''):
        patch=Path(patch).resolve();manifest=read_json(patch/'patch_manifest.json')
        if not manifest.get('files'):raise ValueError('В папке нет готового RPTS-патча.')
        if str(Path(manifest.get('game_dir','')).resolve())!=str(self.game(project_id).resolve()):
            raise ValueError('Патч относится к другой игре. Выбери её проект.')
        stamp=manifest.get('created_at') or str(time.time())
        identity=hashlib.sha256((project_id+stamp+json.dumps(manifest['files'],sort_keys=True)).encode()).hexdigest()[:24]
        destination=self.library/identity
        if (destination/'translation.json').is_file():return identity
        staging=self.library/(identity+'.tmp');staging.mkdir(exist_ok=True)
        try:
            for item in manifest['files']:
                rel=Path(item['path']);src=(patch/rel).resolve()
                if rel.is_absolute() or not src.is_relative_to(patch) or src.is_symlink() or src.suffix!='.rpy':raise ValueError('Небезопасный путь в патче.')
                if not src.is_file() or file_sha256(src)!=item['sha256']:raise ValueError('Файлы перевода изменены или повреждены.')
                target=staging/'patch'/rel;target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(src,target)
            shutil.copy2(patch/'patch_manifest.json',staging/'patch/patch_manifest.json')
            game=next(x for x in self.projects_list() if x['id']==project_id)
            sources=translation_sources(self.project(project_id)/'work/translation_project.sqlite') if patch==self.project(project_id)/'work/patch' else []
            write_json(staging/'translation.json',{'id':identity,'project_id':project_id,'title':game['title'],'created_at':stamp,
                'ready':manifest.get('translated_rows',0),'total':game['total'],'language':manifest.get('renpy_language','russian'),
                'model':model or source_label(sources) or game['model'],'sources':sources,'source_patch':str(patch),'working':False})
            staging.rename(destination)
        except BaseException:
            shutil.rmtree(staging,ignore_errors=True);raise
        return identity
