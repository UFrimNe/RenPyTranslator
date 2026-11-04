from db_helpers import connection as db_connection
"""Partial launch/install and static phone/UI regression, no model or game execution."""
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(APP/'src'),str(APP/'tools'),str(APP/'tests')]
from test_desktop import fixture,Desktop,counts,build_patch,install_patch,MANIFEST_NAME
from rpts.game_audit import audit_game
from rpts.workspace import read_json,write_json
from desktop_worker import select_translation_scope

PHONE_SOURCE='''define hero = Character("云溪")
define mom = Character("妈妈")
init python:
    contacts = {"yunxi": {"name": "云溪"}, "mother": {"name": "妈妈"}}
    def phone_chat_msg(speaker, text):
        return {"speaker": speaker, "text": text}
screen messages_panel():
    text "未读消息"
    textbutton "返回" action Return()
label start:
    hero "我马上回来。"
    call phone_chat(caller="mother", game_time="19:30", messages=[
        phone_chat_msg("caller", "你到哪儿了？"),
        phone_chat_msg("player", "我快到家了。"),
    ])
    return
'''


class LaunchTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.ws,self.pid,self.game,self.db=fixture(self.base)
        for name in ['src','tools','templates','web']:
            shutil.copytree(APP/name,self.ws.root/'app'/name,ignore=shutil.ignore_patterns('__pycache__'))
        self.source=(self.game/'script.rpy').read_bytes()
        self.marker=self.base/'launched.json'

    def tearDown(self):self.temp.cleanup()

    def worker(self,install_current,*,fail_install=False,cancel=False):
        task={'action':'launch','project_id':self.pid,'params':{'install_current':install_current}}
        # Real build, snapshot and install; only external game launch is replaced.
        code='''import json,sys
from pathlib import Path
from unittest.mock import patch
import desktop_worker as worker
from rpts.patch_project import MANIFEST_NAME
def launch(game):
    manifest=Path(game)/MANIFEST_NAME
    Path(sys.argv[2]).write_text(json.dumps({'game':str(game),'installed':manifest.exists(),'manifest':json.loads(manifest.read_text()) if manifest.exists() else None}),encoding='utf-8')
    return str(Path(game).parent/'Game.exe')
with patch('rpts.patch_project.launch_game',launch):
    if sys.argv[3]=='fail':
        with patch('rpts.patch_project.install_patch',side_effect=OSError('test installation failed')):
            result=worker.run(json.loads(sys.argv[1]))
    else:result=worker.run(json.loads(sys.argv[1]))
print('RESULT '+json.dumps(result,ensure_ascii=False))
'''
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(self.ws.root/'app/tools'),str(self.ws.root/'app/src')]),'PYTHONUTF8':'1'}
        env.pop('RPTS_PROJECT_ID',None);env.pop('RPTS_CANCEL_FILE',None)
        if cancel:
            flag=self.base/'cancel';flag.touch();env['RPTS_CANCEL_FILE']=str(flag)
        return subprocess.run([sys.executable,'-c',code,json.dumps(task),str(self.marker),'fail' if fail_install else 'ok'],
                              cwd=self.ws.root,env=env,capture_output=True,text=True,encoding="utf-8",timeout=30)

    def partial(self):
        with db_connection(self.db) as db:
            db.execute("UPDATE segments SET translation='Свежая правка, [name].' WHERE source_text LIKE 'Hello%'")
            db.execute("UPDATE segments SET status='pending',translation='' WHERE source_text='Let us go.'")

    def test_partial_installs_latest_database_not_stale_working_patch(self):
        build_patch(self.db,self.db.parent/'patch');self.partial()
        result=self.worker(True);self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertTrue(read_json(self.marker)['installed'])
        installed=(self.game/'tl/russian/rpts_generated.rpy').read_text()
        self.assertIn('Свежая правка, [name].',installed);self.assertNotIn('Привет, [name].',installed)
        self.assertNotIn('Пойдём.',installed)
        self.assertEqual(1,counts(self.db)['pending']);self.assertEqual(self.source,(self.game/'script.rpy').read_bytes())
        self.assertEqual(1,len(self.ws.translations()))
        payload=json.loads(result.stdout.split('RESULT ')[1]);self.assertTrue(payload['partial']);self.assertEqual(1,payload['translated_rows'])

    def test_launch_only_preserves_installed_patch_and_does_not_build(self):
        build_patch(self.db,self.db.parent/'patch');install_patch(self.db.parent/'patch',self.game)
        before={p.relative_to(self.game).as_posix():p.read_bytes() for p in self.game.rglob('*') if p.is_file()}
        self.partial();self.db.unlink()  # launch must not require/recreate a DB
        result=self.worker(False);self.assertEqual(0,result.returncode,result.stderr)
        self.assertFalse(self.db.exists());self.assertEqual([],list(self.ws.library.iterdir()))
        self.assertEqual(before,{p.relative_to(self.game).as_posix():p.read_bytes() for p in self.game.rglob('*') if p.is_file()})

    def test_install_failure_does_not_launch(self):
        result=self.worker(True,fail_install=True);self.assertNotEqual(0,result.returncode)
        self.assertIn('test installation failed',result.stderr);self.assertFalse(self.marker.exists())

    def test_empty_working_translation_does_not_replace_installed_patch(self):
        build_patch(self.db,self.db.parent/'patch');install_patch(self.db.parent/'patch',self.game)
        before=(self.game/MANIFEST_NAME).read_bytes()
        with db_connection(self.db) as db:db.execute("UPDATE segments SET translation='',status='pending'")
        result=self.worker(True);self.assertNotEqual(0,result.returncode)
        self.assertFalse(self.marker.exists());self.assertEqual(before,(self.game/MANIFEST_NAME).read_bytes())

    def test_cancel_before_install_keeps_game_and_database(self):
        self.partial();before=self.db.read_bytes();result=self.worker(True,cancel=True)
        self.assertNotEqual(0,result.returncode);self.assertFalse(self.marker.exists())
        self.assertFalse((self.game/MANIFEST_NAME).exists());self.assertEqual(before,self.db.read_bytes())

    def test_invalid_install_flag_rejected(self):
        result=self.worker('false');self.assertNotEqual(0,result.returncode)
        self.assertFalse(self.marker.exists());self.assertFalse((self.game/MANIFEST_NAME).exists())

    def test_stopping_job_blocks_new_launch(self):
        with patch.object(Desktop,'_hardware',lambda s:None):app=Desktop(self.ws.root)
        app.job={'id':'stopping-test','status':'stopping'}
        with self.assertRaises(ValueError):app.start('launch',self.pid,{'install_current':True})

    def test_working_database_without_report_recovers_static_phone_on_install(self):
        (self.game/'phone.rpy').write_text(PHONE_SOURCE,encoding='utf-8')
        self.assertFalse((self.db.parent.parent/'reports').exists())
        result=self.worker(True);self.assertEqual(0,result.returncode,result.stderr)
        self.assertEqual(2,counts(self.db)['phone_total'])
        self.assertEqual(2,counts(self.db)['phone_ready']+counts(self.db)['pending'])
        report=self.db.parent.parent/'reports/interactive_messages.json';self.assertTrue(report.exists())
        self.assertEqual(2,len(read_json(report)['messages']))
        self.assertFalse((self.game/'rpts_ui_capture.rpy').exists())


class StaticUITests(unittest.TestCase):
    def test_normal_translation_selects_static_phone_and_ui_without_running_game(self):
        from rpts.translation_engine import _segment_rows,load_config,translate_project
        from rpts.providers import MockProvider
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);game=root/'Emberbound/game';game.mkdir(parents=True)
            script=game/'script.rpy';script.write_text(PHONE_SOURCE,encoding='utf-8')
            reports=root/'reports'
            with contextlib.redirect_stdout(io.StringIO()):audit_game(game,reports_dir=reports,progress=False)
            path=reports/'translation_project.sqlite'
            cfgpath=root/'config.json';write_json(cfgpath,{'quality':{'enabled':False},'translation':{'use_existing_translation_memory':False,
                'kinds':['narration'],'origins':['static'],'channels':['runtime'],'include_statuses':['error']}})
            cfg=select_translation_scope(load_config(cfgpath))
            with contextlib.closing(sqlite3.connect(path)) as db:rows=_segment_rows(db,cfg)
            sources={r['source_text'] for r in rows}
            self.assertTrue({'我马上回来。','你到哪儿了？','我快到家了。','未读消息','返回'}.issubset(sources),sources)
            phone=[r for r in rows if r['origin']=='interactive']
            self.assertEqual(['mom','hero'],[r['speaker'] for r in phone])
            self.assertEqual([0,1],[json.loads(r['context_json'])['interactive']['index'] for r in phone])
            ui_cfg=select_translation_scope(load_config(cfgpath),ui_only=True)
            with contextlib.closing(sqlite3.connect(path)) as db:ui_rows=_segment_rows(db,ui_cfg)
            self.assertIn('我快到家了。',{r['source_text'] for r in ui_rows})
            self.assertNotIn('我马上回来。',{r['source_text'] for r in ui_rows})
            seen=[];original=MockProvider.translate
            def observe(provider,**kwargs):
                seen.extend(kwargs['items']);return original(provider,**kwargs)
            with patch.object(MockProvider,'translate',observe),contextlib.redirect_stdout(io.StringIO()):
                result=translate_project(path,cfg,allow_mock=True)
            self.assertEqual(0,result['errors']);self.assertEqual(len(rows),len(seen))
            self.assertEqual(len(rows),counts(path)['ready'])
            # Scripted approved outputs validate the build adapter, not model quality.
            translations={'我马上回来。':'Я скоро вернусь.','你到哪儿了？':'Ты где сейчас?',
                          '我快到家了。':'Я уже почти дома.','未读消息':'Непрочитанные сообщения','返回':'Назад'}
            with db_connection(path) as db:
                for source,target in translations.items():
                    db.execute("UPDATE segments SET translation=?,status='approved',provider='manual' WHERE source_text=?",(target,source))
            manifest=build_patch(path,root/'patch')
            self.assertEqual(2,manifest['interactive_context_translations']);self.assertEqual(0,manifest['interactive_untranslated'])
            install_patch(root/'patch',game)
            coverage=read_json(root/'patch/interactive_coverage.json')
            self.assertEqual(['mom','hero'],[r['speaker'] for r in coverage])
            self.assertTrue(all(r['status']=='context_adapter' for r in coverage))
            self.assertEqual(PHONE_SOURCE,script.read_text());self.assertFalse((game/'rpts_ui_capture.rpy').exists())
            self.assertGreaterEqual(manifest['runtime_ui_pairs'],2)
            counters=counts(path);self.assertEqual(2,counters['phone_total']);self.assertEqual(2,counters['phone_ready'])


if __name__=='__main__':unittest.main(verbosity=2)
