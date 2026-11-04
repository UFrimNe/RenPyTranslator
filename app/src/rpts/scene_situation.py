"""Bounded source evidence for scene relationships and time, never an AI biography.

Cue matching selects quotations, not facts. Negations, questions and memories
remain quoted; file names, label numbers and job order do not imply chronology.
"""
from __future__ import annotations
import json
import re
from .tokens import strip_protected_for_context

STORY = {'dialogue', 'dialogue_extend', 'narration', 'centered'}
RELATION = re.compile(
    r'发小|青梅竹马|朋友|陌生|认识|初次|第一次见|好久不见|很久.{0,8}(?:联系|见面)|'
    r'熟悉|熟络|亲近|亲密|疏远|信任|恋人|男朋友|女朋友|喜欢你|爱你|分手|和好|'
    r'\b(?:friend\w*|stranger\w*|first met|first meeting|grew up|known each other|'
    r'long time no see|haven.t (?:spoken|seen)|trust|dating|lovers?|broke up|made up)\b|'
    r'друз|друг детства|незнаком|познаком|давно не (?:видел|общал)|довер|сблиз|расстал', re.I)
TONE = re.compile(
    r'开玩笑|玩笑|调侃|打趣|逗|坏笑|笑着|笑了|笑意|温柔|轻声|柔声|安慰|关心|担心|'
    r'害羞|脸红|尴尬|犹豫|不耐烦|冷冷|生气|委屈|沉默|肉麻|'
    r'\b(?:teas\w*|jok\w*|smirk\w*|gentl\w*|softly|comfort\w*|reassur\w*|'
    r'blush\w*|awkward\w*|hesitat\w*|angry|coldly|tender\w*)\b|'
    r'подкол|шут|усмех|нежн|тихо|смущ|ласк|злил|утеш|неловк', re.I)
TIME = re.compile(
    r'昨天|明天|今天|第二天|次日|翌日|当晚|那天|那时|小时候|初中|高中|多年|这些年|'
    r'(?:几|一|两|三|十|\d+)[天年个月小时分钟]+(?:前|后)|回忆|记忆|回到现实|'
    r'\b(?:yesterday|tomorrow|today|years? (?:ago|later)|days? (?:ago|later)|'
    r'next (?:day|morning)|that (?:day|night)|childhood|back then|remember\w*|memory|flashback)\b|'
    r'вчера|завтра|сегодня|(?:лет|дней|года) (?:назад|спустя)|на следующий|в детстве|воспомин', re.I)
# Only explicit narrative transitions split scope, never dialogue plans.
TRANSITION = re.compile(
    r'^(?:[—…\s.]*)?(?:(?:第二天|次日|翌日|几天后|多年后|数年后|那天晚上)(?=[，,。\s…]|一早|清晨)|'
    r'还得说回|说回.{0,8}前|回到.{0,8}(?:年前|天前)|回到现实|回过神来|'
    r'(?:the )?next (?:day|morning)|(?:\w+ )?(?:years?|days?) later\b|'
    r'back in the present\b|back to (?:that|the) (?:day|night)\b|'
    r'на следующий (?:день|утро)|несколько (?:дней|лет) спустя|вернёмся.{0,12}назад)', re.I)
QUOTED = re.compile(r'^\s*[“「«"].*[”」»"]\s*[。.！!？?…]*\s*$', re.S)


def cue_types(text):
    return [name for name, pattern in (('relationship', RELATION), ('time', TIME), ('tone', TONE)) if pattern.search(text)]


def metadata(row):
    try:
        value = json.loads(row.get('context_json') or '{}')
        return value if isinstance(value, dict) else {}
    except (ValueError, TypeError):
        return {}


def index_scopes(by_file):
    """Annotate contiguous scopes; unknown legacy branches stay explicit."""
    for rows in by_file.values():
        previous = None
        scope = ''
        start = 0
        for offset, row in enumerate(rows):
            branch = metadata(row).get('context_scope')
            known = isinstance(branch, list) and all(isinstance(x, str) for x in branch)
            key = (row.get('label'), row.get('origin'), tuple(branch) if known else None)
            transition = row.get('kind') in {'narration', 'centered'} and TRANSITION.search(row.get('source_text', ''))
            if key != previous or transition or not row.get('label') or row.get('kind') not in STORY:
                scope = row['segment_id']
                start = offset
            row['_context_scope'] = scope
            row['_scope_start'] = start
            row['_branch_known'] = known or row.get('origin') == 'interactive'
            row['_cue_types'] = cue_types(row.get('source_text', ''))
            previous = key


def same_scope(a, b):
    if a.get('_context_scope') is not None and b.get('_context_scope') is not None:
        return a['_context_scope'] == b['_context_scope']
    return a.get('label') == b.get('label') and a.get('origin') == b.get('origin')


def situation_for(row, context, by_file, index, profiles=None, *, max_chars=640):
    """Select up to four complete earlier quotes, without duplicating neighbours."""
    pos = index.get(row['segment_id'])
    if not pos or not row.get('label') or row.get('kind') not in STORY or max_chars <= 0:
        return {}
    filename, offset = pos
    rows = by_file[filename]
    current = rows[offset]
    profiles = profiles or {}
    local = [*context.get('before', []), *context.get('after', [])]
    excluded = {r.get('id') for r in local} | {row['segment_id']}
    participants = {row.get('speaker'), (row.get('viewpoint') or {}).get('speaker_id'),
                    (row.get('addressee') or {}).get('speaker_id')} - {None, ''}
    names = {v for sid in participants for v in [profiles.get(sid, {}).get('display_name'), profiles.get(sid, {}).get('english_name')]
             if isinstance(v, str) and v and '[' not in v}
    # New audits carry branch metadata; old labels warrant only a short search.
    reach = 96 if current.get('_branch_known') else 24
    candidates = []
    for i in range(max(current.get('_scope_start', offset), offset-reach), offset):
        candidate = rows[i]
        types = candidate.get('_cue_types') or cue_types(candidate['source_text'])
        if not types or candidate['segment_id'] in excluded or not same_scope(current, candidate):
            continue
        if candidate.get('kind') not in STORY:
            continue
        text = strip_protected_for_context(candidate['source_text'])
        if len(text) > min(260, max_chars):
            continue
        priority = 90 if 'relationship' in types else 60 if 'time' in types else 30
        priority += 20 if any(name in text for name in names) else 0
        priority += 10 if candidate.get('speaker') in participants else 0
        candidates.append((priority-(offset-i)/8, i, types, text))
    chosen = []
    used = 0
    type_counts = {}
    for _, i, types, text in sorted(candidates, reverse=True):
        if used+len(text) > max_chars or all(type_counts.get(t, 0) >= 2 for t in types):
            continue
        chosen.append((i, types, text))
        used += len(text)
        for t in types:
            type_counts[t] = type_counts.get(t, 0)+1
        if len(chosen) == 4:
            break
    evidence = []
    for i, types, text in sorted(chosen):
        candidate = rows[i]
        evidence.append({'id': candidate['segment_id'], 'source_line': candidate['source_line'],
                         'speaker': candidate.get('speaker') or '', 'kind': candidate['kind'],
                         'cues': types, 'text': text})
    local_cues = [{'id': r.get('id', ''), 'cues': cue_types(r.get('text', '')),
                   'position': 'before' if r in context.get('before', []) else 'after'}
                  for r in local if cue_types(r.get('text', ''))]
    target_cues = cue_types(row['source_text'])
    speech = 'quoted_speech' if row.get('kind') in {'narration', 'centered'} and QUOTED.match(row['source_text']) else ''
    clock = metadata(row).get('interactive', {}).get('game_time') if row.get('origin') == 'interactive' else None
    if not (evidence or local_cues or target_cues or speech or clock):
        return {}
    result = {'scope': {'file': filename, 'label': row['label'], 'start_id': current['_context_scope'],
                        'branch_verified': bool(current.get('_branch_known'))}, 'evidence': evidence}
    if participants:
        result['participants'] = sorted(participants)
    if local_cues:
        result['nearby_cues'] = local_cues
    if target_cues:
        result['target_cues'] = target_cues
    if speech:
        result['speech_layer'] = speech
    if clock:
        result['displayed_time'] = str(clock)
    return result


def situation_direction(*, english=True, compact=False):
    if english:
        return ('Situation: situation_context contains SOURCE cues for this passage, not a permanent relationship profile. '
                'Judge how THESE people relate at THIS moment: unfamiliarity, friendship, renewed contact, growing warmth, distance. '
                'Keep affection implicit when it is implicit; warmth is not proof of romance, teasing is not hatred. '
                'A later reply/confession does not make an earlier turn intimate. Evidence may be a question, denial or memory; read its full wording. '
                'Separate present action, remembered speech and plans: yesterday/tomorrow belong to the speaker’s time, not the computer date. '
                'Do not assume chronology from label numbers or file order. quoted_speech inside narration is spoken language in its own time. '
                'Never add endearments, a sibling relationship, ты/вы changes or romantic status without source support. '
                'Preserve a joke’s mechanism (including intentional repetition), not just its practical meaning.')
    return ('СИТУАЦИЯ: situation_context — цитаты исходника для этой сцены, а не постоянный статус отношений. '
            'Выбирай лексику и дистанцию между конкретными собеседниками именно сейчас: знакомство, дружба, возобновление общения, сближение или отчуждение. '
            'Тепло не доказывает роман, подкол — вражду. Не переноси позднее признание на раннюю реплику. Цитата может быть отрицанием, вопросом или воспоминанием. '
            'Различай текущую сцену, прошлое и планы: «вчера/завтра» отсчитываются от момента речи. Порядок файлов и номера label не задают хронологию. '
            'quoted_speech — живая цитируемая речь, а не голос рассказчика. Не выдумывай ласковые обращения, родство, романтику или смену ты/вы. '
            'Сохраняй устройство шутки, в том числе намеренные повторы.')
