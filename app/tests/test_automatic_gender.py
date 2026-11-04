"""Normal translation repairs agreement itself; cached gender is not authority."""
import contextlib,copy,io,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
APP=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(APP/'src'),str(APP/'tests')]
import test_gender_review as fixtures
from test_translation_repairs import ScriptedProvider
from rpts.translation_engine import load_config,translate_project,plan
from rpts.characters import CharacterCorpus,refresh_saved_gender,analyze_character_profiles,PROFILE_VERSION
from rpts.project_db import _normalize_segment,write_project_database
from rpts.identity import resolve_scene_roles,identity_warning


class LocalProvider(ScriptedProvider):
    name='openai_compatible'
    def __init__(self,replies):
        super().__init__(replies)
        self.config.response_protocol='single_text'


class AutomaticTests(unittest.TestCase):
    setUp=fixtures.RepairTests.setUp
    tearDown=fixtures.RepairTests.tearDown
    def prepare(self):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE segments SET status='approved',provider='manual',translation='Сохранено.' WHERE status='pending' AND segment_id<>?",(self.ids[14],))
        cfg=load_config(APP/'templates/translation_config.mock.json')
        cfg['provider']['type']='openai_compatible'
        cfg['provider'].update(model_identity='hy-mt2-7b-q6',response_protocol='single_text')
        cfg['target_language_code']='ru'
        cfg['quality'].update(enabled=True,russian_localization=False,auto_profile_characters=False,
            character_profiles_file=str(self.profiles),quality_dir=str(self.root/'quality'),repair_validation_errors=False)
        cfg['translation'].update(include_statuses=['pending','error'],use_existing_translation_memory=False,refresh_legacy_ai=False)
        cfg['batch']['max_segments']=1
        return cfg
    def run_model(self,cfg,replies,**kwargs):
        provider=LocalProvider(replies)
        with patch('rpts.translation_engine.create_provider',return_value=provider),contextlib.redirect_stdout(io.StringIO()):
            result=translate_project(self.db,cfg,**kwargs)
        return result,provider
    def test_normal_continue_repairs_old_rows_alongside_pending(self):
        cfg=self.prepare();result,provider=self.run_model(cfg,['Я понял.','Я наконец понял.','Понятно.'])
        self.assertEqual(2,result['automatic_gender_repairs']);self.assertEqual(3,result['translated']);self.assertEqual(3,len(provider.calls))
        backups=list((self.root/'backups').glob('*before-gender*'));self.assertEqual(1,len(backups))
        with sqlite3.connect(self.db) as db,sqlite3.connect(backups[0]) as old:
            self.assertEqual(('Я понимала.','approved','manual'),db.execute('select translation,status,provider from segments where segment_id=?',(self.ids[16],)).fetchone())
            self.assertEqual('Я понял.',db.execute('select translation from segments where segment_id=?',(self.ids[15],)).fetchone()[0])
            self.assertEqual('Я понимала.',old.execute('select translation from segments where segment_id=?',(self.ids[12],)).fetchone()[0])
    def test_valid_text_does_not_trigger_another_generation(self):
        cfg=self.prepare();self.run_model(cfg,['Я понял.','Я наконец понял.','Понятно.'])
        result,provider=self.run_model(cfg,[])
        self.assertEqual(0,result['selected']);self.assertEqual(0,result['automatic_gender_repairs']);self.assertEqual([],provider.calls)
    def test_no_pending_rows_is_not_a_reason_to_skip_old_errors(self):
        cfg=self.prepare()
        with sqlite3.connect(self.db) as db:db.execute("UPDATE segments SET status='approved',provider='manual',translation='Понятно.' WHERE segment_id=?",(self.ids[14],))
        result,provider=self.run_model(cfg,['Я понял.','Я наконец понял.'])
        self.assertEqual(2,result['selected']);self.assertEqual(2,result['automatic_gender_repairs']);self.assertEqual(2,len(provider.calls))
    def test_dry_run_plan_includes_repairs_without_model_or_backup(self):
        cfg=self.prepare();report=plan(self.db,cfg)
        self.assertEqual(3,report['segments']);self.assertEqual(2,report['automatic_gender_repairs'])
        result,provider=self.run_model(cfg,[],dry_run=True,limit=1)
        self.assertEqual(1,result['selected']);self.assertEqual(1,result['plan']['segments']);self.assertEqual([],provider.calls)
        self.assertFalse((self.root/'backups').exists())
        self.assertFalse(self.profiles.exists())
    def test_limit_remains_a_total_limit_including_repairs(self):
        cfg=self.prepare();result,provider=self.run_model(cfg,['Я понял.'],limit=1)
        self.assertEqual(1,result['selected']);self.assertEqual(1,result['automatic_gender_repairs']);self.assertEqual(1,len(provider.calls))
        with sqlite3.connect(self.db) as db:
            self.assertEqual('pending',db.execute('select status from segments where segment_id=?',(self.ids[14],)).fetchone()[0])
    def test_ui_only_scope_does_not_retranslate_story(self):
        cfg=self.prepare();cfg['translation']['kinds']=['ui']
        result,provider=self.run_model(cfg,[])
        self.assertEqual(0,result['selected']);self.assertEqual([],provider.calls);self.assertFalse((self.root/'backups').exists())
    def test_failed_automatic_repair_keeps_old_text_and_is_retryable(self):
        cfg=self.prepare();result,provider=self.run_model(cfg,['Я понимала.','Я наконец понял.','Понятно.'])
        self.assertEqual(1,result['errors'])
        with sqlite3.connect(self.db) as db:
            self.assertEqual(('Я понимала.','error'),db.execute('select translation,status from segments where segment_id=?',(self.ids[12],)).fetchone())
        result,provider=self.run_model(cfg,['Я понял.'])
        self.assertEqual(0,result['errors']);self.assertEqual(1,len(provider.calls))
    def test_cancelled_preflight_does_not_reset_any_translation(self):
        cfg=self.prepare()
        with sqlite3.connect(self.db) as db:before=db.execute('select segment_id,translation,status from segments').fetchall()
        with patch('rpts.translation_engine.create_provider',return_value=LocalProvider([])),patch('rpts.runtime_control.check_cancelled',side_effect=KeyboardInterrupt),contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(KeyboardInterrupt):translate_project(self.db,cfg)
        with sqlite3.connect(self.db) as db:self.assertEqual(before,db.execute('select segment_id,translation,status from segments').fetchall())
        self.assertFalse((self.root/'backups').exists())
    def test_phone_metadata_controls_actual_listener_and_narrator(self):
        profiles={'a':{'gender':'male'},'b':{'gender':'male'},'mom':{'gender':'female'}}
        row={'kind':'dialogue','speaker':'a','source_text':'你回来了吗？','origin':'interactive',
            'context_json':json.dumps({'interactive':{'participants':['a','mom']}})}
        context={'before':[{'speaker':'b','text':'嗯。'}]}
        self.assertEqual('female',resolve_scene_roles(row,context,profiles)['addressee']['gender'])
        row.update(kind='narration',speaker='',source_text='我回来了。',context_json=json.dumps({'interactive':{'narration':True,'viewpoint':'mom'}}))
        context['source_viewpoint']={'candidate_speakers':['a','b']}
        roles=resolve_scene_roles(row,context,profiles)
        self.assertEqual('mom',roles['viewpoint']['speaker_id']);self.assertEqual('female',roles['viewpoint']['gender'])
        self.assertEqual('',identity_warning({**row,**roles},'Я вернулась.',profiles))
    def test_preflight_does_not_leak_its_temporary_roles_into_queue(self):
        from rpts.gender_review import find_gender_repair_rows
        from rpts.quality import discover_character_profiles
        self.prepare()
        with sqlite3.connect(self.db) as db:
            profiles=discover_character_profiles(db)
            rows=find_gender_repair_rows(db,profiles)
        self.assertTrue(rows)
        self.assertTrue(all('viewpoint' not in row and 'addressee' not in row for row in rows))


class SavedGenderTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name);self.path=self.root/'project.sqlite'
        rows=[]
        for n,text,kind,speaker,source in [(1,'Mira','character_name','','define p = Character("Mira")'),
            (10,'Mira, she smiled.','narration','',''),(11,'I knew.','dialogue','p','')]:
            rows.append(_normalize_segment({'file':'script.rpy','line':n,'text':text,'kind':kind,'speaker':speaker,'source':source,'label':'start'},origin='static',channel='semantic'))
        write_project_database(self.path,segments=rows,translation_pairs=[],dialogue_translations=[],multilingual_variants=[],runtime_targets=[],metadata={})
        with sqlite3.connect(self.path) as db:self.corpus=CharacterCorpus.read(db)
        record=next(r for r in self.corpus.rows if r['source_line']==10)
        self.profile={'gender':'female','gender_source':'ai_evidence','profile_source_hash':'keep-valid-cache',
            'gender_evidence':[{'id':'E999','segment_id':record['segment_id'],'text_origin':'source','quote':record['source_text'],'reason':'Bound subject pronoun'}]}
    def tearDown(self):self.temp.cleanup()
    def refresh(self,profile):return refresh_saved_gender('p',copy.deepcopy(profile),self.corpus)
    def test_valid_stable_citation_survives_e_number_changes_and_keeps_cache(self):
        result=self.refresh(self.profile)
        self.assertEqual('female',result['gender']);self.assertEqual('verified',result['gender_check']['status'])
        self.assertEqual('keep-valid-cache',result['profile_source_hash'])
    def test_missing_forged_or_removed_evidence_invalidates_cached_gender(self):
        for change in [{'quote':'Invented quotation'},{'segment_id':'removed-segment'},{'text_origin':'invented'}]:
            old=copy.deepcopy(self.profile);old['gender_evidence'][0].update(change)
            result=self.refresh(old);self.assertEqual('unknown',result['gender']);self.assertNotIn('profile_source_hash',result)
        old=copy.deepcopy(self.profile);old['gender_evidence']=[];self.assertEqual('unknown',self.refresh(old)['gender'])
    def test_valid_quote_cannot_prove_the_opposite_gender(self):
        old={**self.profile,'gender':'male'};self.assertEqual('unknown',self.refresh(old)['gender'])
    def test_original_subject_gender_overrides_conflicting_english_reference(self):
        from rpts.characters import discover_profiles
        record=next(r for r in self.corpus.rows if r['source_line']==10);record['reference_en']='Mira is a man.'
        old=copy.deepcopy(self.profile);old.update(gender='male')
        old['gender_evidence'][0].update(text_origin='reference_en',quote=record['reference_en'])
        with sqlite3.connect(self.path) as db:result=discover_profiles(db,{'p':old},corpus=self.corpus)
        self.assertEqual('female',result['p']['gender']);self.assertEqual('source_explicit_reference',result['p']['gender_source'])
    def test_conflicting_original_evidence_does_not_keep_a_cached_gender(self):
        from rpts.characters import discover_profiles
        index=next(i for i,r in enumerate(self.corpus.rows) if r['source_line']==11)
        self.corpus.rows[index].update(kind='narration',speaker='',source_text='Mira, he smiled.')
        self.corpus.mentions['p'].append(index)
        with sqlite3.connect(self.path) as db:result=discover_profiles(db,{'p':self.profile},corpus=self.corpus)
        self.assertEqual('unknown',result['p']['gender']);self.assertEqual('source_conflict',result['p']['gender_source'])
    def test_manual_gender_is_not_invalidated_by_missing_machine_evidence(self):
        old={**self.profile,'gender':'male','gender_locked':True,'gender_evidence':[]}
        result=self.refresh(old);self.assertEqual('male',result['gender']);self.assertEqual('keep-valid-cache',result['profile_source_hash'])
    def cached_profile(self):
        p=copy.deepcopy(self.profile);p['profile_version']=PROFILE_VERSION
        options={'sample_lines':12,'max_chars':7200,'min_confidence':0.85}
        p['profile_source_hash']=self.corpus.signature('p','hy-mt2-7b-q6',options,p)
        return p
    def test_verified_cached_profile_does_not_call_the_model_again(self):
        provider=LocalProvider([])
        with sqlite3.connect(self.path) as db,contextlib.redirect_stdout(io.StringIO()):
            result=analyze_character_profiles(db,provider,{'p':self.cached_profile()},model_identity='hy-mt2-7b-q6')
        self.assertEqual('female',result['p']['gender']);self.assertEqual([],provider.calls)
    def test_invalid_cached_evidence_triggers_one_fresh_profile(self):
        old=self.cached_profile();old['gender_evidence'][0]['quote']='Invented quotation'
        record=next(r for r in self.corpus.rows if r['source_line']==10)
        reply={'entity_type':'person','gender':{'value':'female','confidence':1.0,'reason':'Named subject she',
            'evidence':[{'id':record['id'],'text_origin':'source','reason':'Named subject she'}]},'evidence_ids':[],'relationships':[]}
        provider=LocalProvider([json.dumps(reply)])
        with sqlite3.connect(self.path) as db,contextlib.redirect_stdout(io.StringIO()):
            result=analyze_character_profiles(db,provider,{'p':old},model_identity='hy-mt2-7b-q6')
        self.assertEqual('female',result['p']['gender']);self.assertEqual(1,len(provider.calls))
        self.assertTrue(result['p']['profile_source_hash'])


if __name__=='__main__':unittest.main(verbosity=2)
