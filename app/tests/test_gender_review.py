"""Gender evidence, fixed-name prose and scoped repair safety."""
import contextlib,hashlib,io,json,os,shutil,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
APP=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(APP/'src'),str(APP/'tools'),str(APP/'tests')]
from rpts.source_agreement import source_gender_facts,index_source_viewpoints,has_narrator_i,named_subject_gender_warning
from rpts.scene_situation import index_scopes
from rpts.identity import resolve_scene_roles,identity_warning
from rpts.characters import _declared_role_gender,discover_profiles
from rpts.russian import russian_gender_warning
from rpts.gender_review import scan_gender_repairs,backup_before_gender_repair,select_gender_repairs
from rpts.translation_engine import _segment_rows,ensure_engine_schema,load_config,translate_project
from rpts.project_db import _normalize_segment,write_project_database
from desktop_worker import select_translation_scope
from test_translation_repairs import ScriptedProvider


def row(n,text,speaker='',kind='dialogue',branch=()):
    return {'id':f'E{n}','segment_id':str(n),'source_file':'script.rpy','source_line':n,'source_text':text,
        'speaker':speaker,'kind':kind,'label':'start','origin':'static','context_json':json.dumps({'context_scope':branch})}


def facts(rows,names):
    identities={s:{'display_name':n,'aliases':[n]} for s,n in names.items()}
    return source_gender_facts(SimpleNamespace(rows=rows,identities=identities))


class EvidenceTests(unittest.TestCase):
    def test_vocative_genders_listener_not_speaker(self):
        f=facts([row(1,'哥哥，你回来了？','younger'),row(2,'嗯。','older')],{'younger':'子明','older':'子安'})
        self.assertEqual('male',f['older']['gender']);self.assertNotIn('younger',f)
    def test_named_apposition_and_local_surname(self):
        f=facts([row(1,'这是你的弟弟，周源。','a'),row(2,'嗯。','q'),row(3,'这是周叔叔。','a'),row(4,'你好。','q')],{'a':'肖勇','z':'周源','q':'周瞿'})
        self.assertEqual('male',f['z']['gender']);self.assertEqual('male',f['q']['gender'])
    def test_uncertain_names_negation_quotes_and_ambiguous_listener(self):
        for text in ['不是你的妹妹，周源。','“哥哥，你来了。”','哥带你去。','周源的妹妹来了。']:
            self.assertEqual({},facts([row(1,text,'a'),row(2,'嗯。','b')],{'a':'肖勇','b':'周源'}),text)
        self.assertEqual({},facts([row(1,'哥哥，你来了？','a'),row(2,'嗯。','b'),row(3,'哦。','c')],{'a':'肖勇','b':'周源','c':'周瞿'}))
    def test_vocative_cannot_cross_branch(self):
        self.assertEqual({},facts([row(1,'哥哥，你来了？','a',branch=['one']),row(2,'嗯。','b',branch=['two'])],{'a':'肖勇','b':'周源'}))
    def test_declared_man_is_gendered_but_name_is_not(self):
        self.assertEqual('male',_declared_role_gender({'display_name':'男人'}))
        self.assertEqual('unknown',_declared_role_gender({'display_name':'李昊'}))


class ViewpointTests(unittest.TestCase):
    def context(self,rows):
        by={'script.rpy':rows};index_scopes(by);index_source_viewpoints(by)
        return {'source_viewpoint':rows[1].get('_source_viewpoint')}
    def test_same_known_gender_does_not_invent_narrator_identity(self):
        rows=[row(1,'嗯。','a'),row(2,'我其实明白他的意思。',kind='narration'),row(3,'好。','b')]
        ctx=self.context(rows);v=resolve_scene_roles(rows[1],ctx,{'a':{'gender':'male'},'b':{'gender':'male'}})['viewpoint']
        self.assertEqual('male',v['gender']);self.assertNotIn('speaker_id',v)
        self.assertIn('gender',identity_warning({**rows[1],'viewpoint':v},'Я на самом деле понимала его.',{}))
    def test_mixed_or_unknown_candidates_leave_viewpoint_unresolved(self):
        rows=[row(1,'嗯。','a'),row(2,'我明白了。',kind='narration'),row(3,'好。','b')];ctx=self.context(rows)
        for gender in ['female','unknown']:
            self.assertNotIn('viewpoint',resolve_scene_roles(rows[1],ctx,{'a':{'gender':'male'},'b':{'gender':gender}}))
    def test_quotes_objects_and_third_person_are_not_narrator_actions(self):
        for text in ['“我明白了。”','他看着我。','我的妈妈笑了。','周瞿微笑着。']:
            self.assertFalse(has_narrator_i(text),text)
        rows=[row(1,'嗯。','a'),row(2,'我是个女孩。',kind='narration'),row(3,'好。','a')]
        self.assertNotIn('viewpoint',resolve_scene_roles(rows[1],self.context(rows),{'a':{'gender':'male'}}))
    def test_named_subject_and_first_person_forms(self):
        p={'q':{'display_name':'周瞿','russian_name':'Чжоу Цю','gender':'male'}}
        target={'source_text':'周瞿似乎也懂了的样子，微笑着帮我扛起行李箱。'}
        self.assertIn('Named subject',named_subject_gender_warning(target,'Чжоу Цю, кажется, тоже всё поняла — она улыбнулась.',p))
        self.assertEqual('',named_subject_gender_warning(target,'Чжоу Цю, кажется, тоже всё понял.',p))
        self.assertEqual('',named_subject_gender_warning({'source_text':'周瞿需要帮助。'},'Чжоу Цю была нужна помощь.',p))
        self.assertEqual('',russian_gender_warning('male','Извини, я сначала забыл.'))
        self.assertTrue(russian_gender_warning('male','Я быстро подошла к нему.'))


class RepairTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.db=self.root/'project.sqlite';self.profiles=self.root/'profiles.json'
        data=[(1,'子安','', 'character_name','define a = Character("子安")'),
              (2,'子明','', 'character_name','define b = Character("子明")'),
              (10,'这是我的弟弟，子明。','a','dialogue',''),(11,'哥哥，你回来了？','b','dialogue',''),
              (12,'我知道。','a','dialogue',''),(13,'我终于明白了。','','narration',''),
              (14,'知道了。','a','dialogue',''),(15,'我知道。','b','dialogue',''),(16,'我知道。','a','dialogue','')]
        rows=[]
        for n,text,speaker,kind,source in data:
            r=_normalize_segment({'text':text,'kind':kind,'file':'script.rpy','line':n,'speaker':speaker,
                'label':'start','source':source,'context_scope':()},origin='static',channel='semantic')
            if n in {12,13,15,16}:r.update(status='translated',translation='Я понимала.' if n!=15 else 'Я понял.',provider='openai_compatible',model='rpts-local')
            if n==16:r.update(status='approved',provider='manual')
            rows.append(r)
        write_project_database(self.db,segments=rows,translation_pairs=[],dialogue_translations=[],multilingual_variants=[],runtime_targets=[],metadata={})
        self.ids={n:r['segment_id'] for (n,*_),r in zip(data,rows)}
    def tearDown(self):self.temp.cleanup()
    def test_scan_is_read_only_and_skips_manual_valid_and_pending(self):
        before=hashlib.sha256(self.db.read_bytes()).hexdigest();review=scan_gender_repairs(self.db,self.profiles)
        self.assertEqual({self.ids[12],self.ids[13]},{r['segment_id'] for r in review['repairs']})
        self.assertEqual(before,hashlib.sha256(self.db.read_bytes()).hexdigest())
        self.assertEqual('male',review['profiles']['a']['gender'])
    def test_manual_gender_is_preserved(self):
        with sqlite3.connect(self.db) as db:profiles=discover_profiles(db,{'a':{'gender':'female','gender_source':'manual','gender_locked':True}})
        self.assertEqual('female',profiles['a']['gender'])
    def test_backup_is_complete_and_all_handles_close(self):
        backup=backup_before_gender_repair(self.db)
        with sqlite3.connect(backup) as b,sqlite3.connect(self.db) as d:
            self.assertEqual(d.execute('select segment_id,translation,status from segments').fetchall(),b.execute('select segment_id,translation,status from segments').fetchall())
        renamed=backup.with_suffix('.bak');backup.rename(renamed);renamed.rename(backup)
    def test_scoped_selection_and_normal_translation_clear_stale_ids(self):
        review=scan_gender_repairs(self.db,self.profiles);cfg=load_config(APP/'templates/translation_config.mock.json');select_gender_repairs(cfg,review)
        with sqlite3.connect(self.db) as db:
            ensure_engine_schema(db);ids={r['segment_id'] for r in _segment_rows(db,cfg)}
        self.assertEqual({self.ids[12],self.ids[13]},ids)
        select_translation_scope(cfg);self.assertEqual([],cfg['translation']['segment_ids'])
        self.assertEqual(['pending','error'],cfg['translation']['include_statuses'])
    def test_real_queue_changes_only_selected_automatic_rows(self):
        review=scan_gender_repairs(self.db,self.profiles)
        self.profiles.write_text(json.dumps({'characters':review['profiles']},ensure_ascii=False))
        cfg=load_config(APP/'templates/translation_config.mock.json');cfg['quality'].update(enabled=True,russian_localization=False,
            character_profiles_file=str(self.profiles),quality_dir=str(self.root/'quality'),auto_profile_characters=False)
        cfg['translation']['use_existing_translation_memory']=False
        select_gender_repairs(cfg,review);cfg['batch']['max_segments']=1
        provider=ScriptedProvider(['Я понял.','Я наконец понял.'])
        with patch('rpts.translation_engine.create_provider',return_value=provider),contextlib.redirect_stdout(io.StringIO()):result=translate_project(self.db,cfg)
        self.assertEqual(0,result['errors']);self.assertEqual(2,result['translated']);self.assertEqual(2,len(provider.calls))
        with sqlite3.connect(self.db) as db:
            self.assertEqual(('Я понимала.','approved','manual'),db.execute('select translation,status,provider from segments where segment_id=?',(self.ids[16],)).fetchone())
            self.assertEqual(('Я понял.','translated'),db.execute('select translation,status from segments where segment_id=?',(self.ids[15],)).fetchone())
            self.assertEqual('pending',db.execute('select status from segments where segment_id=?',(self.ids[14],)).fetchone()[0])
    def test_failed_model_reply_keeps_the_previous_text(self):
        review=scan_gender_repairs(self.db,self.profiles);review['repairs']=review['repairs'][:1]
        self.profiles.write_text(json.dumps({'characters':review['profiles']},ensure_ascii=False))
        cfg=load_config(APP/'templates/translation_config.mock.json');cfg['quality'].update(enabled=True,russian_localization=False,
            character_profiles_file=str(self.profiles),quality_dir=str(self.root/'quality'),auto_profile_characters=False,repair_validation_errors=False)
        cfg['translation']['use_existing_translation_memory']=False
        select_gender_repairs(cfg,review);provider=ScriptedProvider(['Я понимала.'])
        with patch('rpts.translation_engine.create_provider',return_value=provider),contextlib.redirect_stdout(io.StringIO()):result=translate_project(self.db,cfg)
        self.assertEqual(1,result['errors'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(('Я понимала.','error'),db.execute('select translation,status from segments where segment_id=?',(review['repairs'][0]['segment_id'],)).fetchone())
    def test_no_separate_gender_repair_action_or_button(self):
        from rpts.desktop import ACTIONS
        self.assertNotIn('repair_gender',ACTIONS)
        for path in [APP/'web/views.js',APP/'web/app.js',APP/'tools/desktop_worker.py']:
            self.assertNotIn('repair_gender',path.read_text(encoding='utf-8'))


if __name__=='__main__':unittest.main(verbosity=2)
