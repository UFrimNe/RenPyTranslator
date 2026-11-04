"""Offline integration regression tests. No model download, no real game changes."""
import contextlib
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request
import urllib.error
import zipfile
APP=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(APP/'src'))
from rpts.workspace import Workspace,OperationLock,read_json,write_json,counts
from rpts.project_db import _normalize_segment,write_project_database
from rpts.desktop import Desktop,Handler,ThreadingHTTPServer
from rpts.patch_project import build_patch,install_patch,uninstall_patch,MANIFEST_NAME

def fixture(root,name='Пример игры',ready=True):
    game=root/name/'game';game.mkdir(parents=True,exist_ok=True)
    (game/'script.rpy').write_text('define a = Character("Alex")\nlabel start:\n    a "Hello [name]."\n    a "Let us go."\n    return\n',encoding='utf-8')
    ws=Workspace(root/'translator').initialize();pid=ws.add_game(game);project=ws.project(pid)
    db=project/'work/translation_project.sqlite';db.parent.mkdir(parents=True)
    rows=[]
    for n,text in enumerate(['Hello [name].','Let us go.']):
        row=_normalize_segment({'text':text,'kind':'dialogue','file':'script.rpy','line':n+3,'speaker':'a','label':'start','translation_id':f'start_abc{n}','say_code':'a'},origin='static',channel='semantic',status='approved' if ready else 'pending')
        if ready:row.update(translation=['Привет, [name].','Пойдём.'][n],provider='manual')
        rows.append(row)
    write_project_database(db,segments=rows,translation_pairs=[],dialogue_translations=[],multilingual_variants=[],runtime_targets=[],metadata={'game_dir':str(game)})
    return ws,pid,game,db

class BackendTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.ws,self.pid,self.game,self.db=fixture(self.base)
        with patch.object(Desktop,'_hardware',lambda s:None):self.desktop=Desktop(self.ws.root)
    def tearDown(self):self.temp.cleanup()
    def test_add_game_deduplicates_exe_and_game_folder(self):
        exe=self.game.parent/'Game.exe';exe.write_bytes(b'fixture')
        self.assertEqual(self.pid,self.ws.add_game(exe));self.assertEqual(1,len(self.ws.projects_list()))
    def test_old_audit_only_project_remains_usable(self):
        project=self.ws.project(self.pid);(project/'project.json').unlink()
        report=project/'reports/translation_project.sqlite';report.parent.mkdir();self.db.rename(report)
        self.assertEqual(self.game,self.ws.game(self.pid))
        self.assertEqual(1,len(self.ws.projects_list()))
        self.assertFalse(self.ws.projects_list()[0]['audited'])
    def test_reject_non_game_folder(self):
        other=self.base/'other';other.mkdir()
        with self.assertRaises(ValueError):self.ws.add_game(other)
    def test_counts_and_no_database_handles_left_open(self):
        self.assertEqual(counts(self.db)['ready'],2)
        self.assertEqual(counts(self.db)['percent'],100)
        moved=self.db.with_suffix('.bak');self.db.rename(moved);moved.rename(self.db)
    def test_project_path_traversal(self):
        for value in ['..','../../etc','/tmp','x/y']:
            with self.assertRaises(ValueError):self.ws.project(value)
    def test_operation_lock_excludes_second_writer(self):
        lock=self.ws.data/'operation.lock'
        with OperationLock(lock):
            with self.assertRaises(RuntimeError):
                with OperationLock(lock):pass
        with OperationLock(lock):pass
    def test_snapshot_install_uninstall_preserves_source(self):
        original=(self.game/'script.rpy').read_bytes();out=self.db.parent/'patch'
        build_patch(self.db,out);identity=self.ws.save_translation(self.pid,out,'test model')
        saved=self.ws.translation_path(identity);self.assertTrue(saved.is_dir())
        self.assertEqual(identity,self.ws.save_translation(self.pid,out,'test model'))
        install_patch(saved,self.game)
        self.assertTrue(self.ws.projects_list()[0]['installed'])
        self.assertIn('Привет, [name].',next(saved.rglob('rpts_generated.rpy')).read_text())
        uninstall_patch(self.game)
        self.assertFalse((self.game/MANIFEST_NAME).exists());self.assertEqual(original,(self.game/'script.rpy').read_bytes())
    def test_snapshot_rejects_corruption(self):
        out=self.db.parent/'patch';m=build_patch(self.db,out)
        (out/m['files'][0]['path']).write_text('corrupt')
        with self.assertRaises(ValueError):self.ws.save_translation(self.pid,out)
        self.assertEqual([],list(self.ws.library.iterdir()))
    def test_snapshot_rejects_wrong_game(self):
        out=self.db.parent/'patch';m=build_patch(self.db,out);m['game_dir']=str(self.base/'other')
        write_json(out/'patch_manifest.json',m)
        with self.assertRaises(ValueError):self.ws.save_translation(self.pid,out)
    def test_snapshot_rejects_path_traversal(self):
        out=self.db.parent/'patch';m=build_patch(self.db,out);m['files'][0]['path']='../../escape.rpy';write_json(out/'patch_manifest.json',m)
        with self.assertRaises(ValueError):self.ws.save_translation(self.pid,out)
    def test_editor_preserves_tokens_and_detects_stale_edits(self):
        row=self.desktop.rows(self.pid)['rows'][0]
        request={'project_id':self.pid,'id':row['segment_id'],'updated_at':row['updated_at'],'translation':'Здравствуй, [name].'}
        self.desktop.mutate('/api/save-row',request)
        self.assertEqual(self.desktop.rows(self.pid)['rows'][0]['status'],'approved')
        with self.assertRaises(ValueError):self.desktop.mutate('/api/save-row',request)
        request.update(updated_at=self.desktop.rows(self.pid)['rows'][0]['updated_at'],translation='Здравствуй.')
        with self.assertRaises(ValueError):self.desktop.mutate('/api/save-row',request)
        self.assertEqual(1,len(list((self.db.parent/'backups').glob('*.sqlite'))))
    def test_editor_does_not_create_missing_database(self):
        self.db.unlink()
        with self.assertRaises(ValueError):self.desktop.mutate('/api/save-row',{'project_id':self.pid})
        self.assertFalse(self.db.exists())
    def test_invalid_settings_do_not_partially_save(self):
        from rpts.local_ai import MODEL_SPECS
        before=read_json(self.ws.settings/'local_ai_settings.json')
        with self.assertRaises(ValueError):self.desktop.mutate('/api/settings',{'model':next(iter(MODEL_SPECS)),'backend':'cpu','glossary':{'a':[]}})
        self.assertEqual(before,read_json(self.ws.settings/'local_ai_settings.json'))
    def test_busy_blocks_edits_but_allows_folder_open(self):
        self.desktop.job={'id':'test','status':'running'}
        with self.assertRaises(ValueError):self.desktop.mutate('/api/select',{'id':self.pid})
        with OperationLock(self.ws.data/'operation.lock'),patch('rpts.desktop.subprocess.Popen'),patch('rpts.desktop.os.startfile',create=True):
            self.assertTrue(self.desktop.mutate('/api/open',{'target':'projects'})['ok'])
    def test_cancel_saves_flag_and_keeps_translations(self):
        folder=self.ws.jobs/'test';folder.mkdir();self.desktop.job={'id':'test','status':'running'}
        self.desktop.cancel();self.assertTrue((folder/'cancel').exists());self.assertEqual(counts(self.db)['ready'],2)
    def test_interrupted_job_recovery(self):
        write_json(self.ws.jobs/'unfinished/job.json',{'id':'unfinished','status':'running'})
        with patch.object(Desktop,'_hardware',lambda s:None):app=Desktop(self.ws.root)
        self.assertEqual('interrupted',app.history[0]['status'])
    def test_incomplete_model_does_not_appear_ready(self):
        from rpts.local_ai import MODEL_SPECS,installation_status
        spec=next(iter(MODEL_SPECS.values()));home=self.ws.ai
        (home/'models').mkdir(exist_ok=True);(home/'models'/spec.filename).write_bytes(b'GGUF incomplete')
        engine=home/'engine/cpu';engine.mkdir(parents=True);(engine/'llama-server.exe').write_bytes(b'fixture')
        status=installation_status({'home':str(home),'model_key':spec.key,'backend':'cpu'})
        self.assertFalse(status['model_present']);self.assertFalse(status['ready']);self.assertTrue(status['engine_present'])
    def test_invalid_job_is_not_started(self):
        with self.assertRaises(ValueError):self.desktop.start('delete_anything',self.pid)
    def test_worker_audit_reads_source_game(self):
        for name in ['src','tools','templates','web']:
            source=APP/name
            if source.exists():shutil.copytree(source,self.ws.root/'app'/name,ignore=shutil.ignore_patterns('__pycache__'))
        # Raw-source fixture: dependency downloading is outside this test.
        fixture_menu=self.ws.root/'app/tools/rpts_menu.py'
        fixture_menu.write_text(fixture_menu.read_text(encoding='utf-8').replace('    if not unrpyc_ready:', '    if False:  # Offline fixture: skip optional dependency installer'),encoding='utf-8')
        self.db.unlink();self.desktop.start('audit',self.pid)
        end=time.monotonic()+40
        while self.desktop.busy() and time.monotonic()<end:time.sleep(.05)
        self.assertEqual('completed',self.desktop.job['status'],self.desktop.job)
        self.assertGreaterEqual(counts(self.db)['total'],2)
    def test_full_worker_build_and_report(self):
        for name in ['src','tools','templates','web']:
            source=APP/name
            if source.exists():shutil.copytree(source,self.ws.root/'app'/name,ignore=shutil.ignore_patterns('__pycache__'))
        for action in ['build','report']:
            self.desktop.start(action,self.pid)
            end=time.monotonic()+40
            while self.desktop.busy() and time.monotonic()<end:time.sleep(.05)
            self.assertFalse(self.desktop.busy(),self.desktop.job)
            self.assertEqual('completed',self.desktop.job['status'],self.desktop.job)
        self.assertEqual(1,len(self.ws.translations()))
        with zipfile.ZipFile(self.ws.reports/'RenPyAuditReport.zip') as z:
            self.assertIn('work/translation_project.sqlite',z.namelist())
            self.assertTrue(any(n.startswith('diagnostics/desktop_jobs/') for n in z.namelist()))
            self.assertIn('diagnostics/frontend/app.js',z.namelist())

class MigrationTests(unittest.TestCase):
    def test_legacy_folders_configuration_and_idempotence(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'local_ai').mkdir();(root/'local_ai/model.gguf').write_bytes(b'model')
            write_json(root/'settings/local_ai_settings.json',{'home':'local_ai','notes':'local_ai should remain in prose'})
            (root/'projects/test/work').mkdir(parents=True);(root/'projects/test/work/keep').write_text('keep')
            ws=Workspace(root).initialize();Workspace(root).initialize()
            self.assertEqual('keep',(ws.projects/'test/work/keep').read_text())
            cfg=read_json(ws.settings/'local_ai_settings.json');self.assertEqual(str(ws.ai),cfg['home']);self.assertEqual('local_ai should remain in prose',cfg['notes'])
            self.assertTrue((ws.ai/'model.gguf').exists());self.assertTrue((ws.settings/'local_ai_settings.json.before-layout').exists())
    def test_conflicting_folders_are_kept(self):
        with tempfile.TemporaryDirectory() as t:
            root=Path(t);(root/'projects/same').mkdir(parents=True);(root/'data/projects/same').mkdir(parents=True)
            ws=Workspace(root).initialize();self.assertEqual([ws.projects/'same'],ws.project_paths())
            self.assertTrue((root/'projects/same').exists());self.assertTrue(read_json(ws.data/'layout_migration.json')['warnings'])
    def test_single_project_layout_is_recovered_without_deleting_original(self):
        with tempfile.TemporaryDirectory() as t:
            ws,pid,game,db=fixture(Path(t));shutil.copytree(db.parent,ws.data/'work');shutil.rmtree(ws.projects/pid)
            ws.initialize();self.assertEqual(2,ws.projects_list()[0]['ready']);self.assertTrue((ws.data/'work/translation_project.sqlite').exists())

class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();ws,pid,game,db=fixture(Path(self.tmp.name));self.pid=pid
        with patch.object(Desktop,'_hardware',lambda s:None):self.app=Desktop(ws.root)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler);self.server.app=self.app;self.app.server=self.server
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
        self.url=f'http://127.0.0.1:{self.server.server_port}'
        shutil.copytree(APP/'web',ws.root/'app/web')
    def tearDown(self):self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def call(self,path,headers=None,data=None):
        req=urllib.request.Request(self.url+path,headers=headers or {},data=json.dumps(data).encode() if data is not None else None)
        try:
            with urllib.request.urlopen(req) as r:return r.status,json.load(r)
        except urllib.error.HTTPError as e:return e.code,json.load(e)
    def test_launcher_serves_views_and_authenticates_update_service(self):
        with urllib.request.urlopen(self.url+'/views.js') as response:
            self.assertEqual(200,response.status)
            self.assertIn('script-src',response.headers['Content-Security-Policy'])
        self.assertTrue(hasattr(self.app,'updater'))
        headers={'Authorization':'Bearer '+self.app.token}
        self.assertEqual(404,self.call('/api/update/handoff',headers)[0])
        self.assertEqual(403,self.call('/api/update/check',{}, {})[0])
        with patch.object(self.app.updater,'check',return_value={'status':'checking'}) as check:
            self.assertEqual(200,self.call('/api/update/check',headers,{})[0])
            check.assert_called_once()
    def test_native_picker_and_icon_scripts_are_local_static_assets(self):
        for path in ['/native-picker.js','/icons.js','/selects.js']:
            with urllib.request.urlopen(self.url+path) as response:
                self.assertEqual(200,response.status)
                self.assertIn('script-src',response.headers['Content-Security-Policy'])
        self.assertEqual(404,self.call('/icons/../../runtime/python/python.exe')[0])
    def test_authentication_and_origin(self):
        self.assertEqual(403,self.call('/api/state')[0])
        headers={'Authorization':'Bearer '+self.app.token}
        self.assertEqual(200,self.call('/api/state',headers)[0])
        headers['Origin']='https://external.invalid';self.assertEqual(403,self.call('/api/select',headers,{'id':self.pid})[0])
    def test_static_path_traversal_denied(self):
        self.assertEqual(404,self.call('/../runtime/python/python.exe')[0])
    def test_rows_and_mutation_validation(self):
        headers={'Authorization':'Bearer '+self.app.token}
        status,data=self.call('/api/rows?project_id='+self.pid,headers)
        self.assertEqual(200,status);self.assertEqual(2,data['total'])
        self.assertEqual(400,self.call('/api/job',headers,{'action':'bad'})[0])

class TranslationTests(unittest.TestCase):
    def test_cooperative_cancel_keeps_committed_batch(self):
        from rpts.translation_engine import translate_project,load_config
        from rpts.providers import MockProvider
        with tempfile.TemporaryDirectory() as t:
            ws,pid,game,db=fixture(Path(t),ready=False);cancel=Path(t)/'cancel'
            config_path=Path(t)/'config.json';write_json(config_path,{'batch':{'max_segments':1},'quality':{'enabled':False},'translation':{'use_existing_translation_memory':False}})
            cfg=load_config(config_path);original=MockProvider.translate
            def stop_after_one(self,**kw):
                result=original(self,**kw);cancel.touch();return result
            with patch.dict(os.environ,{'RPTS_CANCEL_FILE':str(cancel)}),patch.object(MockProvider,'translate',stop_after_one),contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(KeyboardInterrupt):translate_project(db,cfg,allow_mock=True)
            self.assertEqual(1,counts(db)['ready']);self.assertEqual(1,counts(db)['pending'])

class PromptContractTests(unittest.TestCase):
    def test_every_model_config_has_actual_identity_and_safe_hy_mapping(self):
        from rpts.local_ai import MODEL_SPECS,make_translation_config,detect_hardware
        from rpts.model_policy import model_family,uses_single_text,uses_source_only_review
        hw=detect_hardware()
        self.assertEqual(9,len(MODEL_SPECS))
        for spec in MODEL_SPECS.values():
            with self.subTest(model=spec.key):
                cfg=make_translation_config(spec,hw=hw,backend='cpu')
                self.assertTrue(cfg['provider']['model_identity'])
                if model_family(cfg['provider'])=='hy_mt2':
                    self.assertTrue(uses_single_text(cfg['provider']))
                    self.assertEqual(1,cfg['batch']['max_segments'])
                    self.assertTrue(uses_source_only_review(cfg))
    def test_single_target_keeps_neighbours_read_only_and_gender(self):
        from rpts.native_translation import isolate_item,user_prompt
        original=[{'id':'one','text':'First turn','speaker':'mom','kind':'dialogue'},
                  {'id':'two','text':'Target turn','speaker':'hero','kind':'dialogue','character':{'gender':'male'}},
                  {'id':'three','text':'Next turn','speaker':'mom','kind':'dialogue'}]
        before=json.dumps(original);item=isolate_item(original,1);prompt=user_prompt(item)
        self.assertEqual('Target turn',prompt.split('[Source Text]')[-1].strip())
        self.assertIn('Earlier turns, not part of the target',prompt)
        self.assertIn('Later turns, not part of the target',prompt)
        self.assertIn('gender=male',prompt);self.assertEqual(before,json.dumps(original))
    def test_repair_prompt_excludes_rejected_draft(self):
        from rpts.native_translation import user_prompt
        prompt=user_prompt({'text':'Original line','draft':'Rejected neighbour merge','repair_attempt':True})
        self.assertNotIn('Rejected neighbour merge',prompt)

if __name__=='__main__':unittest.main(verbosity=2)
