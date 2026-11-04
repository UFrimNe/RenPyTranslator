"""Offline regressions derived from the 2026-10-28 Emberbound report.

Scripted replies check code paths, not the literary quality of a live model.
"""
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))
from rpts import native_translation
from rpts.identity import identity_warning
from rpts.providers import BaseProvider, ProviderConfig, OpenAICompatibleProvider, _single_text_repair_grammar
from rpts.russian import russian_gender_warning, repair_first_person, repair_known_script_mixes, russian_issues
from rpts.scene_voice import source_voice_samples
from rpts.tokens import protect, validate_preservation
from rpts.translation_engine import (_prompt_items, _system_prompt, _repair_single_translation,
    _restore_provider_text, _validate_quality_translation, _accepted_revisions)
from rpts.model_policy import model_family
from rpts.local_ai import make_translation_config, MODEL_SPECS, HardwareInfo


CONFIG = {"target_language": "Russian", "provider": {"model_identity": "hy-mt2-7b-q6"},
          "quality": {"enabled": True, "russian_localization": True, "repair_attempts": 2},
          "validation": {"reject_empty": True, "max_length_ratio": 8, "min_source_chars_for_ratio": 20}}


class ScriptedProvider(BaseProvider):
    def __init__(self, replies):
        super().__init__(ProviderConfig(model="rpts-local", model_identity="hy-mt2-7b-q6", base_url="http://127.0.0.1:9999/v1"))
        self.replies, self.calls = list(replies), []
    def translate(self, *, system_prompt, user_prompt, items):
        self.calls.append({"system": system_prompt, "user": user_prompt, "items": copy.deepcopy(items)})
        return [{"id": item["id"], "text": self.replies.pop(0)} for item in items]


class GenderScopeTests(unittest.TestCase):
    def test_reported_mother_clause_is_not_hero(self):
        for dash in ("—", "–", "-"):
            text = f"Я к тебе пришёл {dash} мама сказала, что нужно прийти лично."
            self.assertEqual("", russian_gender_warning("male", text))
            self.assertEqual((text, []), repair_first_person("male", text))
    def test_known_male_still_rejects_female_self_reference(self):
        for text in ("Я пришла.", "Я, наверное, поверила.", "Я уже вернулась.", "Я совсем растерялась."):
            self.assertTrue(russian_gender_warning("male", text), text)
    def test_quotes_and_mother_do_not_inherit_gender(self):
        for text in ('Я слышал, как мама сказала: «Я устала».', 'Мама сказала, что я уже готов.', 'Я — мама сказала — уже пришёл.'):
            self.assertEqual("", russian_gender_warning("male", text))
    def test_female_and_unknown_profiles(self):
        self.assertTrue(russian_gender_warning("female", "Я пришёл."))
        self.assertEqual("", russian_gender_warning("female", "Я пришла — папа был дома."))
        self.assertEqual("", russian_gender_warning("unknown", "Я пришла."))
    def test_addressee_scope(self):
        row = {"source_text":"", "kind":"dialogue", "addressee":{"speaker_id":"hero", "gender":"male"}}
        self.assertEqual("", identity_warning(row, "Ты пришёл — мама была рада.", {}))
        self.assertIn("Addressee", identity_warning(row, "Ты пришла.", {}))


class ScriptMixTests(unittest.TestCase):
    def test_reported_studio(self):
        for spelling, expected in (("STУДИЯ", "СТУДИЯ"), ("stудии", "студии"), ("Stудия", "Студия")):
            fixed, changes = _restore_provider_text("FLUFFORE STUDIO / 绒石开发组", spelling + " FLUFFORE", [])
            self.assertEqual(expected + " FLUFFORE", fixed)
            self.assertTrue(changes)
            self.assertEqual([], russian_issues("FLUFFORE STUDIO", fixed, "ui"))
    def test_no_general_transliteration(self):
        for text in ("Fluффore", "Cтудия", "STУДИЯ2", "https://example.test/STУДИЯ", "[STУДИЯ]", "{font=STУДИЯ.ttf}Текст{/font}"):
            self.assertEqual((text, []), repair_known_script_mixes("Brand", text))
        self.assertEqual(("STУДИЯ", []), repair_known_script_mixes("STУДИЯ", "STУДИЯ"))
        self.assertTrue(russian_issues("Brand", "Fluффore", "ui"))
    def test_neuter_place(self):
        self.assertTrue(russian_issues("那个地方", "Этот место хорошее."))
        self.assertEqual([], russian_issues("那个地方", "Это место хорошее."))


class PromptContextTests(unittest.TestCase):
    def rows(self):
        return [{"segment_id":str(i), "source_file":"script.rpy", "source_line":i, "kind":"dialogue",
                 "source_text":text, "speaker":speaker, "label":"scene", "origin":"static"}
                for i, (speaker, text) in enumerate([("a", "你终于回来了！"), ("b", "怎么了，发生什么事？"),
                  ("a", "别担心，我在这里。"), ("b", "好的，我知道了。"), ("a", "还好吗？")])]
    def test_only_same_scene_speaker_past_source_samples(self):
        rows=self.rows();index={r['segment_id']:('script.rpy',i) for i,r in enumerate(rows)}
        samples=source_voice_samples(rows[-1],{}, {'script.rpy':rows}, index)
        self.assertEqual(['0','2'],[r['id'] for r in samples])
        rows[2]['label']='other_branch'
        self.assertEqual([],source_voice_samples(rows[-1],{}, {'script.rpy':rows}, index))
    def test_neighbours_and_catalogue_do_not_duplicate_samples(self):
        rows=self.rows();index={r['segment_id']:('script.rpy',i) for i,r in enumerate(rows)}
        context={'before':[{'id':r['segment_id'],'speaker':r['speaker'],'text':r['source_text']} for r in rows[:-1]]}
        self.assertEqual([],source_voice_samples(rows[-1],context,{'script.rpy':rows},index))
        rows[-1]['kind']='localized_string'
        self.assertEqual([],source_voice_samples(rows[-1],{}, {'script.rpy':rows},index))
    def test_small_context_has_no_extra_samples(self):
        rows=self.rows();index={r['segment_id']:('script.rpy',i) for i,r in enumerate(rows)}
        self.assertEqual([],source_voice_samples(rows[-1],{}, {'script.rpy':rows},index,max_chars=0))
    def test_phone_request_has_source_hints_and_read_only_voice(self):
        row={'segment_id':'target','source_text':'滚滚滚，别这么肉麻。','speaker':'p','kind':'dialogue',
             'origin':'interactive','label':'phone'}
        sample={'id':'past','speaker':'p','text':'别担心，我在这里。'}
        items,_=_prompt_items([row],{'target':{'before':[],'after':[],'voice_samples':[sample]}},profiles={'p':{'gender':'male'}})
        prompt=native_translation.user_prompt(items[0])
        self.assertIn('messaging, not physically arriving',prompt)
        self.assertIn('肉麻',str(items[0]['source_hints']))
        self.assertIn('voice only; do not translate',prompt)
        self.assertEqual(row['source_text'],prompt.rsplit('[Source Text]\n',1)[1])
        self.assertEqual(sample,items[0]['voice_samples'][0])
    def test_every_catalogue_model_keeps_response_contract_and_tone_rules(self):
        for spec in MODEL_SPECS.values():
            cfg={'provider':{'model_identity':spec.key,'compact_json':True,'response_protocol':'anchored_keys'},
                 'quality':{'russian_localization':True},'target_language':'Russian'}
            prompt=_system_prompt(cfg,'dialogue')
            self.assertTrue('эмоци' in prompt.lower() or 'emotional' in prompt, spec.key)
            self.assertTrue('voice_samples' in prompt or 'Voice samples' in prompt, spec.key)
            self.assertIn('ONLY' if model_family(cfg['provider'])=='hy_mt2' else 'JSON',prompt)
    def test_ui_prompt_is_not_emotional_dialogue(self):
        self.assertNotIn('teasing from hostility',_system_prompt(CONFIG,'ui'))
    def test_style_update_does_not_force_successful_11_14_revision_to_reset(self):
        self.assertIn('11.14',_accepted_revisions(CONFIG))


class MarkupRepairTests(unittest.TestCase):
    def setUp(self):
        self.row={'segment_id':'ui','source_text':'{color=#fff}你好{/color}，欢迎回来。','speaker':'','kind':'ui','label':''}
        self.items,self.tokens=_prompt_items([self.row],{})
    def test_complete_sentence_is_repaired_before_fragment_fallback(self):
        model=ScriptedProvider(['⟪RPT0000⟫Привет⟪RPT0001⟫, с возвращением.'])
        text=_repair_single_translation(model,config=CONFIG,row=self.row,prompt_item=self.items[0],tokens=self.tokens['ui'],profiles={},reason='Missing protected marker')
        self.assertEqual('{color=#fff}Привет{/color}, с возвращением.',text)
        self.assertEqual(1,len(model.calls))
        self.assertEqual(self.items[0]['text'],model.calls[0]['items'][0]['text'])
    def test_fallback_never_exposes_raw_tags_in_context(self):
        model=ScriptedProvider(['Привет.', 'Здравствуйте.', 'Привет', ', с возвращением.'])
        text=_repair_single_translation(model,config=CONFIG,row=self.row,prompt_item=self.items[0],tokens=self.tokens['ui'],profiles={},reason='Missing protected marker')
        self.assertTrue(validate_preservation(self.row['source_text'],text)[0])
        for call in model.calls[2:]:
            self.assertNotIn('{color=',call['user'])
            self.assertNotIn('{/color}',call['user'])
    def test_failed_formatting_is_not_silently_stripped_and_accepted(self):
        model=ScriptedProvider(['Привет.', 'Здравствуйте.', '{color=#bad}Привет{/color}', ', с возвращением.',
                                '{color=#bad}Привет{/color}', '{color=#bad}Привет{/color}'])
        with self.assertRaises(ValueError):
            _repair_single_translation(model,config=CONFIG,row=self.row,prompt_item=self.items[0],tokens=self.tokens['ui'],profiles={},reason='Missing protected marker')
    def test_missing_and_cross_row_markers_remain_errors(self):
        with self.assertRaises(ValueError):_restore_provider_text(self.row['source_text'],'Привет',self.tokens['ui'])
        with self.assertRaises(ValueError):_restore_provider_text(self.row['source_text'],'⟪RPTS01_0000⟫Привет',{'scope':'00','tokens':self.tokens['ui']})
    def test_repair_grammar_is_scoped_to_local_native_repair(self):
        cfg=ProviderConfig(model='rpts-local',model_identity='hy-mt2-7b-q6',base_url='http://127.0.0.1:9999/v1')
        item=dict(self.items[0],repair_attempt=1)
        grammar=_single_text_repair_grammar(cfg,item)
        self.assertEqual(1,grammar.count('⟪RPT0000⟫'))
        self.assertEqual(1,grammar.count('⟪RPT0001⟫'))
        self.assertIsNone(_single_text_repair_grammar(cfg,self.items[0]))
        cfg.base_url='https://api.example.test/v1'
        self.assertIsNone(_single_text_repair_grammar(cfg,item))
    def test_actual_provider_request_has_plain_text_grammar_not_json(self):
        cfg=ProviderConfig(model='rpts-local',model_identity='hy-mt2-7b-q6',base_url='http://127.0.0.1:9999/v1')
        item=dict(self.items[0],repair_attempt=1)
        answer='⟪RPT0000⟫Привет⟪RPT0001⟫, с возвращением.'
        with patch('rpts.providers._http_json',return_value={'choices':[{'finish_reason':'stop','message':{'content':answer}}]}) as http:
            result=OpenAICompatibleProvider(cfg).translate(system_prompt=_system_prompt(CONFIG,'ui'),user_prompt=native_translation.user_prompt(item),items=[item])
        body=http.call_args.args[1]
        self.assertIn('grammar',body);self.assertNotIn('response_format',body)
        self.assertEqual(['user'],[m['role'] for m in body['messages']])
        self.assertEqual([{'id':'ui','text':answer}],result)


if __name__ == '__main__':unittest.main(verbosity=2)
