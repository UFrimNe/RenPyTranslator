"""Source scope, temporal/relationship evidence and model request contracts."""
import copy
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(APP/'src'),str(APP/'tests')]
from rpts.scene_situation import index_scopes,situation_for
from rpts.scene_voice import source_voice_samples
from rpts.translation_engine import (_context_for,_prompt_items,_system_prompt,
    _split_provider_batch,_cache_key,_batch_rows,load_config,translate_project,_repair_single_translation)
from rpts.native_translation import user_prompt,isolate_item
from rpts.project_db import _normalize_segment,write_project_database
from rpts.parser import RenPySourceParser
from rpts.ast_adapter import extract_ast
from rpts.local_ai import MODEL_SPECS
from rpts.providers import MockProvider
from rpts.workspace import write_json
from rpts.russian_evaluation import evaluation_context
from test_translation_repairs import ScriptedProvider


def row(n,text,*,speaker='a',label='scene',kind='dialogue',branch=(),origin='static'):
    return _normalize_segment({'text':text,'file':'script.rpy','line':n,'kind':kind,
        'speaker':speaker,'label':label,'context_scope':branch},origin=origin,channel='semantic')


def prepare(rows):
    by={'script.rpy':rows};idx={r['segment_id']:('script.rpy',i) for i,r in enumerate(rows)}
    index_scopes(by);return by,idx


class SituationTests(unittest.TestCase):
    def test_friendship_anchor_survives_short_neighbour_window(self):
        rows=[row(1,'就是我的发小——晟阳。',speaker='',kind='narration')]
        rows += [row(i,'看看这条河吧。',speaker='p') for i in range(2,14)]
        rows += [row(14,'就是担心我忘了，所以才提醒你记得提醒我嘛。')]
        by,idx=prepare(rows);target=rows[-1];target['addressee']={'speaker_id':'p'}
        ctx=_context_for(target,by,idx,4,3)
        self.assertNotIn(rows[0]['segment_id'],[x['id'] for x in ctx['before']])
        situation=situation_for(target,ctx,by,idx,{'a':{'display_name':'晟阳'}})
        self.assertEqual(rows[0]['segment_id'],situation['evidence'][0]['id'])
        self.assertEqual(rows[0]['source_text'],situation['evidence'][0]['text'])
        ctx['situation_context']=situation
        items,_=_prompt_items([target],{target['segment_id']:ctx},shared_scene_context=True)
        prompt=user_prompt(items[0]);self.assertIn('就是我的发小——晟阳。',prompt)
        self.assertIn('Повтор 提醒',prompt)
        self.assertEqual(target['source_text'],prompt.rsplit('[Source Text]\n',1)[1])

    def test_future_romance_is_not_used_as_earlier_relationship(self):
        rows=[row(1,'初次见面，请问怎么称呼？')]+[row(i,'嗯。') for i in range(2,9)]+[row(9,'我爱你。')]
        by,idx=prepare(rows);ctx=_context_for(rows[0],by,idx,4,3)
        result=situation_for(rows[0],ctx,by,idx)
        self.assertEqual([],result['evidence']);self.assertNotIn('我爱你',json.dumps(result,ensure_ascii=False))

    def test_quoted_denial_stays_a_quote_not_a_friend_status(self):
        rows=[row(1,'我们不是朋友。')]+[row(i,'我知道。') for i in range(2,9)]
        by,idx=prepare(rows);result=situation_for(rows[-1],{},by,idx)
        self.assertEqual('我们不是朋友。',result['evidence'][0]['text'])
        self.assertNotIn('relationship_status',result)

    def test_explicit_time_jump_resets_context_and_voice(self):
        rows=[row(1,'我们是好朋友。'),row(2,'别担心，我一直在这里。'),
              row(3,'第二天，一切都变了。',speaker='',kind='narration'),row(4,'你好。')]
        by,idx=prepare(rows);ctx=_context_for(rows[-1],by,idx,4,3)
        self.assertEqual([rows[2]['segment_id']],[r['id'] for r in ctx['before']])
        self.assertNotIn('我们是好朋友',json.dumps(situation_for(rows[-1],ctx,by,idx),ensure_ascii=False))
        self.assertEqual([],source_voice_samples(rows[-1],ctx,by,idx))
        groups=list(_batch_rows(rows,20,10000,scene_aware=True));self.assertEqual([2,2],[len(g) for g in groups])

    def test_plan_for_tomorrow_does_not_advance_time(self):
        rows=[row(1,'明天见，老朋友。'),row(2,'好啊。')];by,idx=prepare(rows)
        self.assertEqual(rows[0]['_context_scope'],rows[1]['_context_scope'])
        self.assertEqual('明天见，老朋友。',situation_for(rows[1],{},by,idx)['evidence'][0]['text'])

    def test_different_scene_and_unordered_catalogue_have_no_social_memory(self):
        rows=[row(1,'我们是朋友。',label='earlier'),row(2,'你好。',label='later')];by,idx=prepare(rows)
        self.assertEqual({},situation_for(rows[1],{},by,idx))
        item=row(3,'我爱你。',kind='localized_string',label='');by,idx=prepare([item])
        self.assertEqual({},situation_for(item,{},by,idx))

    def test_unknown_legacy_branches_limit_reach(self):
        rows=[row(1,'我们是朋友。',branch=None)]+[row(i,'好吧。',branch=None) for i in range(2,40)]
        by,idx=prepare(rows);self.assertEqual({},situation_for(rows[-1],{},by,idx))
        rows[-2]['source_text']='我们很久没联系了。';by,idx=prepare(rows)
        result=situation_for(rows[-1],{},by,idx)
        self.assertFalse(result['scope']['branch_verified']);self.assertEqual(1,len(result['evidence']))

    def test_phone_time_is_literal_and_scenes_do_not_mix(self):
        a=row(1,'晚安。',origin='interactive',label='interactive:one')
        a['context_json']=json.dumps({'interactive':{'game_time':'三年前 / 23:40'}})
        b=row(1,'我们是朋友。',origin='interactive',label='interactive:two');by,idx=prepare([b,a])
        value=situation_for(a,{},by,idx);self.assertEqual('三年前 / 23:40',value['displayed_time'])
        self.assertEqual([],value['evidence']);self.assertTrue(value['scope']['branch_verified'])

    def test_quoted_childhood_speech_preserves_its_layer(self):
        rows=[row(1,'他的声音从记忆里传来。',kind='narration',speaker=''),
              row(2,'“[player_name]，走！哥带你去买冰棍！”',kind='narration',speaker=''),
              row(3,'那都是初中时的事了。',kind='narration',speaker='')]
        by,idx=prepare(rows);ctx=_context_for(rows[1],by,idx,4,3);ctx['situation_context']=situation_for(rows[1],ctx,by,idx)
        item=_prompt_items([rows[1]],{rows[1]['segment_id']:ctx})[0][0]
        self.assertEqual('quoted_speech',item['situation_context']['speech_layer'])
        self.assertIn('quoted speech inside narration',user_prompt(item));self.assertIn('родство',user_prompt(item))

    def test_evidence_budget_uses_whole_quotes_and_no_extra_for_tiny_context(self):
        rows=[row(i,'我们是朋友。'+('真的。'*i),speaker='a') for i in range(1,10)]
        by,idx=prepare(rows);result=situation_for(rows[-1],{},by,idx,max_chars=80)
        self.assertLessEqual(sum(len(r['text']) for r in result['evidence']),80)
        self.assertLessEqual(len(result['evidence']),4)
        self.assertTrue(all(r['text'] in {x['source_text'] for x in rows} for r in result['evidence']))
        self.assertEqual({},situation_for(rows[-1],{},by,idx,max_chars=0))


class BranchScopeTests(unittest.TestCase):
    def test_source_if_else_and_menu_branches_are_separate_without_id_changes(self):
        source='''label start:
    if close:
        a "我们是朋友。"
    else:
        a "我们是陌生人。"
    a "走吧。"
    menu:
        "留下":
            a "我相信你。"
        "离开":
            a "再见。"
'''
        candidates=[c for c in RenPySourceParser().parse(source) if c.kind=='dialogue']
        self.assertNotEqual(candidates[0].context_scope,candidates[1].context_scope)
        self.assertEqual((),candidates[2].context_scope)
        self.assertNotEqual(candidates[3].context_scope,candidates[4].context_scope)
        rows=[]
        for c in candidates:
            value={**c.to_dict(),'file':'script.rpy'}
            normalized=_normalize_segment(value,origin='static',channel='semantic');rows.append(normalized)
            value.pop('context_scope');self.assertEqual(normalized['segment_id'],_normalize_segment(value,origin='static',channel='semantic')['segment_id'])
        by,idx=prepare(rows)
        self.assertEqual([], _context_for(rows[1],by,idx,4,3)['before'])
        self.assertNotIn('我们是朋友',json.dumps(situation_for(rows[1],{},by,idx),ensure_ascii=False))

    def test_compiled_ast_records_branch_scopes(self):
        def node(kind,**kw):
            result=type(kind,(),{})();result.__dict__.update(kw);return result
        def say(line,text):return node('Say',linenumber=line,who='a',what=text,filename='script.rpy')
        ast=[node('Label',name='start',linenumber=1,block=[
            node('If',linenumber=2,entries=[('close',[say(3,'我们是朋友。')]),('True',[say(5,'初次见面。')])]),
            say(6,'走吧。'),node('Menu',linenumber=7,items=[('Yes',None,[say(8,'好啊。')]),('No',None,[say(9,'再见。')])])])]
        result=[x for x in extract_ast(ast) if x.kind=='dialogue']
        self.assertNotEqual(result[0].context_scope,result[1].context_scope);self.assertEqual((),result[2].context_scope)
        self.assertNotEqual(result[3].context_scope,result[4].context_scope)


class PromptIntegrationTests(unittest.TestCase):
    def test_evaluation_uses_the_same_quoted_speech_context(self):
        cases=json.loads((APP/'templates/russian_evaluation_cases.json').read_text(encoding='utf-8'))
        case=next(c for c in cases if '哥带你去买冰棍' in c['source'])
        target=row(2,case['source'],kind='narration',speaker='')
        context=evaluation_context(target,case,{}, {'provider':{'context_limit_tokens':6144}})
        item=_prompt_items([target],{target['segment_id']:context})[0][0]
        self.assertEqual('quoted_speech',item['situation_context']['speech_layer'])
        self.assertIn('quoted speech inside narration',user_prompt(item))
        self.assertEqual(item['text'],user_prompt(item).rsplit('[Source Text]\n',1)[1])

    def test_every_model_receives_time_relationship_rules_without_changing_format(self):
        for spec in MODEL_SPECS.values():
            cfg={'provider':{'model_identity':spec.key,'compact_json':True,'response_protocol':'anchored_keys'},'quality':{'russian_localization':True}}
            text=_system_prompt(cfg,'dialogue');self.assertIn('situation_context',text,spec.key)
            self.assertNotIn('situation_context',_system_prompt(cfg,'ui'))
            self.assertTrue('JSON' in text or 'Return ONLY' in text)

    def test_split_isolation_and_repairs_keep_target_situation(self):
        rows=[row(1,'你好。'),row(2,'走吧。')]
        situations=[{'scope':{'label':'scene','start_id':str(i)},'evidence':[]} for i in range(2)]
        contexts={r['segment_id']:{'situation_context':s} for r,s in zip(rows,situations)}
        items,tokens=_prompt_items(rows,contexts,shared_scene_context=True)
        isolated=isolate_item(items,1);self.assertEqual(situations[1],isolated['situation_context'])
        left,right=_split_provider_batch(None,system_prompt='',items=items,reason=ValueError('fixture'))
        self.assertEqual(situations[1],right[0]['situation_context'])
        provider=ScriptedProvider(['Пойдём.'])
        cfg={'provider':{'model_identity':'hy-mt2-7b-q6'},'quality':{'russian_localization':True,'repair_attempts':1}}
        _repair_single_translation(provider,config=cfg,row=rows[1],prompt_item=isolated,tokens=tokens[rows[1]['segment_id']],profiles={},reason='fixture error')
        self.assertEqual(situations[1],provider.calls[0]['items'][0]['situation_context'])

    def test_cache_distinguishes_context_without_resetting_approved_text(self):
        r=row(1,'你好。');cfg={'provider':{},'quality':{}}
        a=_cache_key(r,{'situation_context':{'evidence':['friends']}},cfg,'mock','test')[0]
        b=_cache_key(r,{'situation_context':{'evidence':['strangers']}},cfg,'mock','test')[0]
        self.assertNotEqual(a,b)

    def test_real_queue_attaches_evidence_with_no_extra_provider_call(self):
        with tempfile.TemporaryDirectory() as temp:
            root=Path(temp);dbpath=root/'project.sqlite';cfgpath=root/'config.json'
            rows=[row(1,'我们是好朋友。',speaker='',kind='narration')]+[row(i,'看这条河。') for i in range(2,12)]+[row(12,'你还记得吗？')]
            write_project_database(dbpath,segments=rows,translation_pairs=[],dialogue_translations=[],multilingual_variants=[],runtime_targets=[],metadata={})
            write_json(cfgpath,{'quality':{'enabled':True,'russian_localization':False,'character_profiles_file':str(root/'profiles.json'),'quality_dir':str(root/'quality')},
                'batch':{'max_segments':1},'context':{'previous':4,'next':3},'translation':{'use_existing_translation_memory':False}})
            cfg=load_config(cfgpath);calls=[];original=MockProvider.translate
            def observe(provider,**kw):calls.extend(copy.deepcopy(kw['items']));return original(provider,**kw)
            with patch.object(MockProvider,'translate',observe),contextlib.redirect_stdout(io.StringIO()):
                result=translate_project(dbpath,cfg,allow_mock=True)
            self.assertEqual(0,result['errors']);self.assertEqual(len(rows),len(calls))
            self.assertEqual(rows[0]['segment_id'],calls[-1]['situation_context']['evidence'][0]['id'])


if __name__=='__main__':unittest.main(verbosity=2)
