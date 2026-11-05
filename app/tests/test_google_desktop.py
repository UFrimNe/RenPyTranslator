"""Google UI and pipeline contract: real SQLite/patch worker, mocked external transport only."""
from db_helpers import connection
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from test_desktop import fixture,Desktop,read_json,write_json,APP
from rpts.providers import GoogleFreeTranslateProvider,ProviderConfig,ProviderError,ProviderRateLimitError
from rpts.translation_engine import _call_provider_with_retry
from rpts.workspace import source_label,translation_sources

class TransportTests(unittest.TestCase):
    def provider(self):
        return GoogleFreeTranslateProvider(ProviderConfig(type='google_free',request_delay_seconds=0,max_retries=2,retry_delay_seconds=0))
    def test_normal_failure_uses_fallback_but_rate_limit_does_not(self):
        p=self.provider()
        with patch.object(p,'_request_text_rpc',side_effect=ProviderError('timeout')),patch.object(p,'_request_text_gtx',return_value='Привет') as fallback:
            self.assertEqual('Привет',p._request_text('Hello','auto','ru'));fallback.assert_called_once()
        for method in ['_request_text_rpc','_request_text_gtx']:
            with patch.object(p,'_request_text_rpc',side_effect=ProviderError('timeout') if method.endswith('gtx') else ProviderRateLimitError('429')),patch.object(p,'_request_text_gtx',side_effect=ProviderRateLimitError('429')) as fallback:
                with self.assertRaises(ProviderRateLimitError):p._request_text('Hello','auto','ru')
                self.assertEqual(1 if method.endswith('gtx') else 0,fallback.call_count)
    def test_changed_marker_splits_without_merging_or_reordering(self):
        p=self.provider();calls=[]
        def text(value,*_):
            calls.append(value)
            if len(calls)==1:return 'Broken marker'
            return value.replace('Hello','Привет').replace('Bye','Пока')
        with patch.object(p,'_request_text',side_effect=text):
            got=p.translate(system_prompt='',user_prompt='',items=[{'id':'a','text':'Hello'},{'id':'b','text':'Bye'}])
        self.assertEqual([{'id':'a','text':'Привет'},{'id':'b','text':'Пока'}],got)
        self.assertEqual(3,len(calls))
    def test_cancelled_request_does_not_send_or_switch_endpoint(self):
        with tempfile.TemporaryDirectory() as t:
            flag=Path(t)/'cancel';flag.touch();p=self.provider()
            with patch.dict(os.environ,{'RPTS_CANCEL_FILE':str(flag)}),patch.object(p,'_request_text_rpc') as primary,patch.object(p,'_request_text_gtx') as fallback:
                with self.assertRaises(KeyboardInterrupt):p._request_text('Hello','auto','ru')
                primary.assert_not_called();fallback.assert_not_called()

    def test_timeout_retries_and_captcha_stops_without_retry(self):
        p=self.provider()
        with patch.object(p,'translate',side_effect=[ProviderError('timeout'),[{'id':'a','text':'Да'}]]) as call:
            self.assertEqual('Да',_call_provider_with_retry(p,system_prompt='',user_prompt='',items=[])[0]['text']);self.assertEqual(2,call.call_count)
        with patch.object(p,'translate',side_effect=ProviderRateLimitError('CAPTCHA')) as call:
            with self.assertRaises(ProviderRateLimitError):_call_provider_with_retry(p,system_prompt='',user_prompt='',items=[])
            self.assertEqual(1,call.call_count)

class GoogleWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.ws,self.pid,self.game,self.db=fixture(self.base,ready=False)
        for name in ['src','tools','templates','web']:
            shutil.copytree(APP/name,self.ws.root/'app'/name,ignore=shutil.ignore_patterns('__pycache__'))
        self.ws.set_translation_method(self.pid,'google_free')
        config=read_json(APP/'templates/translation_config.google_free.example.json')
        config['batch']['max_segments']=2;config['provider']['request_delay_seconds']=0
        write_json(self.ws.settings/'translation_config.google_free.json',config)
    def tearDown(self):self.temp.cleanup()
    def worker(self,action='translate',limit=False):
        # A separate process exercises exactly the same worker/DB/patch path as CEF.
        code='''import json,sys
from unittest.mock import patch
import desktop_worker as w
from rpts.providers import ProviderRateLimitError
calls=[]
def request(self,text,source,target):
    calls.append(text)
    if sys.argv[2]=='limit' and len(calls)>1:raise ProviderRateLimitError('HTTP 429 test')
    return text.replace('Hello','Привет').replace('Let us go.','Пойдём.')
with patch('rpts.providers.GoogleFreeTranslateProvider._request_text',request),patch('rpts.local_ai.install_engine',side_effect=AssertionError('AI download')),patch('rpts.local_ai.start_server',side_effect=AssertionError('AI server')):
    result=w.run(json.loads(sys.argv[1]))
print('RESULT '+json.dumps(result,ensure_ascii=False))
'''
        env={**os.environ,'PYTHONPATH':os.pathsep.join([str(self.ws.root/'app/tools'),str(self.ws.root/'app/src')]),'PYTHONUTF8':'1'}
        env.pop('RPTS_CANCEL_FILE',None);env.pop('RPTS_PROJECT_ID',None)
        task={'action':action,'project_id':self.pid,'params':{'install':False}}
        r=subprocess.run([sys.executable,'-c',code,json.dumps(task),'limit' if limit else 'normal'],cwd=self.ws.root,env=env,capture_output=True,text=True,encoding='utf-8',timeout=30)
        self.assertEqual(0,r.returncode,r.stdout+r.stderr)
        return json.loads(r.stdout.split('RESULT ')[1])
    def test_google_without_model_preserves_approved_and_labels_mixed_version(self):
        with connection(self.db) as db:
            db.execute("UPDATE segments SET status='approved',translation='Моя правка, [name].',provider='manual' WHERE source_text LIKE 'Hello%'")
        self.worker()
        with connection(self.db) as db:
            rows=db.execute('SELECT translation,provider FROM segments ORDER BY source_line').fetchall()
        self.assertEqual(('Моя правка, [name].','manual'),rows[0]);self.assertEqual(('Пойдём.','google_free'),rows[1])
        saved=self.ws.translations()[0];self.assertEqual({'google_free','manual'},{x['provider'] for x in saved['sources']})
        self.assertIn('Google Translate',saved['model']);self.assertIn('Ручные правки',saved['model'])
        self.assertFalse((self.ws.settings/'local_ai_settings.json').exists())
    def test_google_ui_scope_translates_ui_but_not_dialogue(self):
        with connection(self.db) as db:db.execute("UPDATE segments SET kind='ui' WHERE source_text='Let us go.'")
        self.worker('translate_ui')
        with connection(self.db) as db:rows=db.execute('SELECT status FROM segments ORDER BY source_line').fetchall()
        self.assertEqual([('pending',),('translated',)],rows)
    def test_rate_limit_preserves_batch_and_resume_finishes(self):
        config=read_json(self.ws.settings/'translation_config.google_free.json');config['batch'].update(max_segments=2,mode_max_segments={'story':1})
        write_json(self.ws.settings/'translation_config.google_free.json',config)
        result=self.worker(limit=True);self.assertTrue(result['rate_limited'])
        from rpts.workspace import counts
        self.assertEqual(1,counts(self.db)['ready']);self.assertEqual(1,counts(self.db)['pending'])
        self.worker();self.assertEqual(2,counts(self.db)['ready'])
    def test_method_persistence_validation_and_lock(self):
        with patch.object(Desktop,'_hardware',lambda _:None):desktop=Desktop(self.ws.root)
        desktop.mutate('/api/translation-method',{'project_id':self.pid,'method':'local_ai'})
        self.assertEqual('local_ai',self.ws.projects_list()[0]['translation_method'])
        with self.assertRaises(ValueError):desktop.mutate('/api/translation-method',{'project_id':self.pid,'method':'bad'})
        desktop.job={'status':'running'}
        with self.assertRaises(ValueError):desktop.mutate('/api/translation-method',{'project_id':self.pid,'method':'google_free'})
        self.assertEqual('local_ai',self.ws.projects_list()[0]['translation_method'])
