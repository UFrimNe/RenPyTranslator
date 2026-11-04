"""Source-bound gender and fixed-name viewpoint, without a biography/model call."""
from __future__ import annotations
import re
from collections import defaultdict
from .russian import speaker_view, russian_gender_warning
from .scene_situation import index_scopes, same_scope

ROLES = {'哥哥':'male','哥':'male','弟弟':'male','爸爸':'male','爸':'male','父亲':'male',
         '叔叔':'male','儿子':'male','姐姐':'female','姐':'female','妹妹':'female',
         '妈妈':'female','妈':'female','母亲':'female','阿姨':'female','女儿':'female'}
ROLE_PATTERN = '|'.join(sorted(ROLES,key=len,reverse=True))
SAY = {'dialogue','dialogue_extend'}
STORY = {'narration','centered'}


def source_gender_facts(corpus):
    """Bind explicit roles to named people or a unique local listener.

    Never assign the listener's role to the speaker. Names alone, personality,
    shared images, possessive pronouns and imagined/negated roles prove nothing.
    """
    by_file=defaultdict(list)
    for row in corpus.rows:by_file[row['source_file']].append(row)
    index_scopes(by_file)
    findings=defaultdict(list)
    def add(sid,gender,row,reason):
        findings[sid].append((gender,{'id':row['id'],'text_origin':'source',
            'quote':row['source_text'],'reason':reason,'segment_id':row['segment_id'],
            'source_file':row['source_file'],'source_line':row['source_line']}))
    for rows in by_file.values():
        for i,row in enumerate(rows):
            if row['kind'] not in SAY|STORY:continue
            text=speaker_view(row['source_text'])
            local=[r for r in rows[max(0,i-3):i+4] if same_scope(r,row) and r.get('speaker') and r['kind'] in SAY]
            # An apposition explicitly names the gendered relative: 弟弟，周源.
            if not re.search(r'[?？]|不是|并非|如果|假如|假装',text):
                for sid,identity in corpus.identities.items():
                    for name in identity.get('aliases',[]):
                        if not re.fullmatch(r'[\u3400-\u9fff]{2,5}',name):continue
                        pattern=r'('+ROLE_PATTERN+r')[，,：:\s]+'+re.escape(name)+r'(?=$|[。！!，,；;\s])'
                        match=re.search(pattern,text)
                        if match:add(sid,ROLES[match[1]],row,'Explicit gendered role followed by the declared full name')
            if row['kind'] not in SAY:continue
            # Only a punctuation-delimited vocative, not "my brother..." or a
            # question ABOUT a brother. The small exchange must have one listener.
            vocative=re.match(r'^\s*(?:[啊哎欸喂哦…\s]{0,4})('+ROLE_PATTERN+r')(?:[，,！!。？?…]|$)',text)
            others={r['speaker'] for r in local}-{row.get('speaker')}
            if vocative and len(others)==1:
                add(next(iter(others)),ROLES[vocative[1]],row,'Direct kinship vocative in a unique two-speaker exchange')
            # A gendered surname reference (周叔叔) must match ONE declared
            # participant nearby; a remote namesake is not imported into a scene.
            for match in re.finditer(r'([\u3400-\u9fff])('+'|'.join(['叔叔','阿姨'])+r')',text):
                if match.start() and text[match.start()-1] in '非不':continue
                candidates={r['speaker'] for r in local if r['speaker']!=row.get('speaker') and
                    re.fullmatch(r'[\u3400-\u9fff]{2,5}',corpus.identities.get(r['speaker'],{}).get('display_name','')) and
                    corpus.identities[r['speaker']]['display_name'].startswith(match[1])}
                if len(candidates)==1:add(next(iter(candidates)),ROLES[match[2]],row,'Gendered surname role uniquely matches a local declared person')
    result={}
    for sid,records in findings.items():
        genders={g for g,_ in records}
        result[sid]={'gender':next(iter(genders)) if len(genders)==1 else 'unknown',
                     'evidence':[e for _,e in records[:3]],'conflict':len(genders)>1}
    return result


def has_narrator_i(text):
    # Possessive "my mother" alone is not an action attributed to the narrator.
    return bool(re.search(r'(?:^|[，,。！？!?；;…])\s*(?:但(?:是)?|而|可(?:是)?)?我(?!的|们|妈|爸|哥|姐|弟|妹|家)|\bI\b',speaker_view(text)))


def index_source_viewpoints(by_file):
    """Record local narrator candidates without inventing a speaker identity.

    Explicit I narration is required. A later resolver can use a shared known
    gender only when EVERY local candidate has that gender. Different/unknown
    genders leave the viewpoint unresolved. Quotes and branches never inherit it.
    """
    for rows in by_file.values():
        for i,row in enumerate(rows):
            row.pop('_source_viewpoint',None)
            if row['kind'] not in STORY or not has_narrator_i(row['source_text']):continue
            local=[r for r in rows[max(0,i-4):i+5] if same_scope(r,row) and r['kind'] in SAY and r.get('speaker')]
            candidates=sorted({r['speaker'] for r in local})
            if candidates:
                row['_source_viewpoint']={'candidate_speakers':candidates,
                    'basis':'local first-person source passage; speaker identity unresolved',
                    'evidence_ids':[r['segment_id'] for r in local]}


def named_subject_gender_warning(row,translated,profiles):
    """Narrow named-subject check; report errors for model repair, never suffix-edit."""
    source=speaker_view(row.get('source_text',''))
    for sid,p in profiles.items():
        if p.get('gender') not in {'male','female'}:continue
        names=[p.get('display_name',''),*(p.get('aliases') or [])]
        if not any(n and re.match(r'^\s*'+re.escape(n)+r'(?:似乎|也|已经|微笑|笑|懂|明白|知道|说|走|拿|帮)',source) for n in names):continue
        target=p.get('russian_name') or ''
        match=re.match(r'^\s*'+re.escape(target)+r'(?!\w)',translated,re.I) if target else None
        if not match:continue
        tail=translated[match.end():]
        # Dative name + impersonal predicate can legitimately agree with an
        # object: "Чжоу Цю была нужна помощь". Do not gender such constructions.
        if re.match(r'\s+(?:был[ао]?|нужн[ао]?|пришла|осталась)\b',tail,re.I):continue
        warning=russian_gender_warning(p['gender'],'Я'+tail)
        if warning:return f'Named subject gender conflicts ({sid}, {p["gender"]}): '+warning
    return ''
