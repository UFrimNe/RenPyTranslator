"""Inspect existing automatic translations and back up before scoped retranslation."""
from __future__ import annotations
from contextlib import closing
from datetime import datetime,timezone
from pathlib import Path
import sqlite3


def automatic_review_enabled(config,provider_name=None):
    provider_name=provider_name or (config.get('provider') or {}).get('type')
    return (bool((config.get('quality') or {}).get('enabled')) and
        str(config.get('target_language_code') or '').lower() in {'ru','russian'} and
        provider_name in {'openai_compatible','ollama'} and
        (config.get('translation') or {}).get('repair_scope')!='gender')


def include_automatic_repairs(db,selected,profiles,config,limit=None,*,context_data=None,provider_name=None):
    if not automatic_review_enabled(config,provider_name):return selected,[]
    repairs=find_gender_repair_rows(db,profiles,config.get('translation'),context_data=context_data)
    merged={r['segment_id']:r for r in selected}
    for row in repairs:merged.setdefault(row['segment_id'],row)
    rows=sorted(merged.values(),key=lambda r:(r['source_file'],r['source_line'],r['segment_id']))
    if limit is not None:rows=rows[:max(0,int(limit))]
    included={r['segment_id'] for r in rows}
    return rows,[{'segment_id':r['segment_id'],'reason':r['_auto_gender_reason']} for r in repairs if r['segment_id'] in included]


def find_gender_repair_rows(db,profiles,selection=None,*,context_data=None):
    """Internal normal-translation preflight; respects scope, never manual rows."""
    from .quality import gender_warning,profile_for_speaker
    from .translation_engine import _all_context_rows,_context_for
    from .identity import resolve_scene_roles,identity_warning
    selection=selection or {}
    if 'semantic' not in selection.get('channels',['semantic']):return []
    by,index=context_data or _all_context_rows(db)
    kinds=set(selection.get('kinds') or []);origins=set(selection.get('origins') or []);ids=set(selection.get('segment_ids') or [])
    result=[]
    cur=db.execute("SELECT * FROM segments WHERE channel='semantic' AND translation<>'' AND status IN ('translated','translated_tm','error') AND provider IN ('openai_compatible','ollama','google_free','google_translate') ORDER BY source_file,source_line,segment_id")
    columns=[c[0] for c in cur.description]
    for values in cur.fetchall():
        row=dict(zip(columns,values))
        if row['kind'] not in {'dialogue','dialogue_extend','narration','centered'}:continue
        if kinds and row['kind'] not in kinds and not (selection.get('include_interactive') and row['origin']=='interactive'):continue
        if origins and row['origin'] not in origins or ids and row['segment_id'] not in ids:continue
        node={**row,**resolve_scene_roles(row,_context_for(row,by,index,4,3),profiles)}
        reason=gender_warning(profile_for_speaker(profiles,row['speaker']),row['translation']) if row['kind'] in {'dialogue','dialogue_extend'} else ''
        reason=reason or identity_warning(node,row['translation'],profiles)
        if reason and 'gender' in reason.lower():
            row['_auto_gender_reason']=reason;result.append(row)
    return result


def scan_gender_repairs(database,profiles_path):
    from .quality import load_character_profiles,discover_character_profiles,gender_warning,profile_for_speaker
    from .translation_engine import _all_context_rows,_context_for
    from .identity import resolve_scene_roles,identity_warning
    path=Path(database).resolve()
    with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)) as db:
        db.row_factory=sqlite3.Row
        profiles=discover_character_profiles(db,load_character_profiles(profiles_path))
        by,index=_all_context_rows(db)
        repairs=[]
        for raw in db.execute("SELECT * FROM segments WHERE channel='semantic' AND translation<>'' AND status IN ('translated','translated_tm','error') AND provider IN ('openai_compatible','ollama','google_free','google_translate') ORDER BY source_file,source_line,segment_id"):
            row=dict(raw)
            if row['kind'] not in {'dialogue','dialogue_extend','narration','centered'}:continue
            row.update(resolve_scene_roles(row,_context_for(row,by,index,4,3),profiles))
            reason=gender_warning(profile_for_speaker(profiles,row['speaker']),row['translation']) if row['kind'] in {'dialogue','dialogue_extend'} else ''
            reason=reason or identity_warning(row,row['translation'],profiles)
            if reason and ('gender' in reason.lower()):
                repairs.append({'segment_id':row['segment_id'],'source_file':row['source_file'],
                    'source_line':row['source_line'],'source_text':row['source_text'],
                    'translation':row['translation'],'reason':reason,'viewpoint':row.get('viewpoint')})
    return {'profiles':profiles,'repairs':repairs,
        'known_genders':sum(p.get('gender') in {'male','female'} for p in profiles.values()),
        'unknown_genders':[sid for sid,p in profiles.items() if p.get('gender') not in {'male','female'}]}


def backup_before_gender_repair(database):
    path=Path(database);folder=path.parent/'backups';folder.mkdir(exist_ok=True)
    target=folder/(path.stem+'.before-gender-'+datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S-%f')+'.sqlite')
    with closing(sqlite3.connect(path)) as source,closing(sqlite3.connect(target)) as dest:
        source.backup(dest)
    return target


def select_gender_repairs(config,review):
    selection=config.setdefault('translation',{})
    selection.update(channels=['semantic'],include_statuses=['translated','translated_tm','error'],
        kinds=[],origins=[],include_interactive=True,refresh_legacy_ai=False,
        repair_scope='gender',
        segment_ids=[r['segment_id'] for r in review['repairs']])
    config.setdefault('quality',{})['auto_profile_characters']=False
    return config
