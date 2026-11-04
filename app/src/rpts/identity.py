"""Source-bound identity facts and agreement checks, independent of AI biography."""
from __future__ import annotations
import re
import json
from .russian import GENDER_FORMS, russian_gender_warning, speaker_view, visible_text

# Literal role/species labels are not aliases for subsequently revealed names.
LABELS = {"老妈": "Мама", "妈妈": "Мама", "广播": "Радио", "白熊": "Белый медведь",
          "灰狼兽人": "Серый волк", "灰狼": "Серый волк", "赤狐": "Рыжий лис",
          "老板娘": "Хозяйка", "老板": "Хозяин"}
SPECIES = {
    "bear": (r"熊|\bbears?\b", r"\bмедвед\w*|\bмедвеж\w*"),
    "wolf": (r"狼|\bwol(?:f|ves)\b", r"\bволк\w*|\bволч\w*"),
    "fox": (r"狐|\bfox(?:es)?\b", r"\bлис(?:а|ы|у|е|ой|ом|ов|ам|ами|ах)?\b|\bлисиц\w*|\bлись\w*"),
}

def source_species(text):
    return {key for key, (source, _) in SPECIES.items() if re.search(source, visible_text(text), re.I)}

def literal_name(text):
    plain = re.sub(r"[\u200b-\u200f\ufeff]", "", text).strip()
    if plain and not re.sub(r"[?？. …]", "", plain):
        return text
    return LABELS.get(text, "")

def apply_identity_facts(speaker, profile, identity, corpus, locks):
    name = str(identity.get("display_name") or "")
    kinds = source_species(name)
    if len(kinds) == 1:
        profile["species"] = next(iter(kinds))
    canonical = literal_name(name)
    if canonical and "russian_name" not in locks:
        profile["russian_name"] = canonical
        profile["russian_name_source"] = "source_literal"
    variables = identity.get("interpolation_variables") or []
    profile["interpolation_variables"] = variables
    is_player = any(re.fullmatch(r"(?:player|mc|protagonist)(?:_?name)?", var, re.I) for var in variables)
    if not is_player:
        return
    profile["entity_type"] = "player"
    # Narrator attribution is resolved scene by scene, never for all narration.
    if "gender" in locks:
        return
    candidates = []
    for i, row in enumerate(corpus.rows):
        if not row.get("speaker") or row["speaker"] == speaker or row["kind"] not in {"dialogue", "dialogue_extend"}:
            continue
        text = row["source_text"]
        cue = re.search(r"(?:^|[，,。！!？?—])\s*(儿子|女儿)\s*[，,！!。]", text)
        if not cue:
            continue
        turns = [r for r in corpus.rows[max(0, i-3):i+4] if r["source_file"] == row["source_file"] and r["label"] == row["label"] and r.get("speaker")]
        if {r["speaker"] for r in turns} != {row["speaker"], speaker}:
            continue
        candidates.append(("male" if cue[1] == "儿子" else "female", row))
    if len({gender for gender, _ in candidates}) == 1:
        gender, record = candidates[0]
        profile.update(gender=gender, gender_source="source_explicit_reference", gender_confidence=1.0,
            gender_reason="Direct kinship vocative addressed to the player in a two-speaker exchange",
            gender_evidence=[{"id": record["id"], "text_origin": "source", "quote": record["source_text"],
                              "reason": "Vocative son/daughter; adjacent reply belongs to this player"}])

def resolve_scene_roles(row, context, profiles):
    result = {}
    speaker = row.get("speaker") or ""
    nearby = [*(context.get("before") or []), row, *(context.get("after") or [])]
    players = [(s, p) for s, p in profiles.items() if p.get("entity_type") == "player" and p.get("interpolation_variables")]
    # Explicit source/reference first-person narration plus a local player turn.
    if row.get("kind") in {"narration", "centered"}:
        source = row.get("source_text") or row.get("text") or ""
        visible = re.sub(r'“[^”]*”|「[^」]*」|"[^"\n]*"', '', source)
        from .source_agreement import has_narrator_i
        established=context.get('source_viewpoint') or {}
        local_players = [(s, p) for s, p in players if p.get("narrator_viewpoint") is not False and any(r.get("speaker") == s for r in nearby)]
        scene_players = [(s, p) for s, p in players if s in context.get("scene_speakers", []) and p.get("narrator_viewpoint") is not False]
        candidates = local_players if re.search(r"我|\bI\b", visible) else []
        if len(candidates) != 1 and context.get("scene_first_person") and re.search(r"\w", visible):
            candidates = scene_players
        if len(candidates) == 1 and not result.get('viewpoint'):
            s, p = candidates[0]
            result["viewpoint"] = {"speaker_id": s, "gender": p.get("gender", "unknown"),
                "basis": "first-person scene with one declared player; quoted speech excluded"}
        if not result.get('viewpoint') and has_narrator_i(source):
            candidates=established.get('candidate_speakers',[])
            genders={profiles.get(s,{}).get('gender','unknown') for s in candidates}
            excluded=any(profiles.get(s,{}).get('narrator_viewpoint') is False for s in candidates)
            # Names/speaker IDs remain unresolved. Never call the narrator the
            # last character who happened to speak, or assume an unknown is male.
            explicit_role=re.search(r'我是\s*(?:一个|个)?(?:男|女)|\bI(?: am|\x27m) (?:a )?(?:man|woman|boy|girl)\b',visible,re.I)
            if candidates and not excluded and not explicit_role and len(genders)==1 and 'unknown' not in genders:
                result['viewpoint']={**established,'gender':next(iter(genders)),
                    'basis':'unquoted first-person passage; all local narrator candidates share this established gender; identity unresolved'}
    if speaker and row.get("kind") in {"dialogue", "dialogue_extend"}:
        source = row.get("source_text") or row.get("text") or ""
        participants = {r.get("speaker") for r in nearby if r.get("speaker")}
        explicit = []
        for s, p in profiles.items():
            if s == speaker: continue
            names = ["["+v+"]" for v in p.get("interpolation_variables", [])] + list(p.get("aliases") or []) + [p.get("display_name") or ""]
            # An explicit vocative can address a bystander who has no nearby
            # turn (e.g. calling the barbecue vendor). A standalone name
            # question may instead mention an absent friend; it is not a cue.
            if any(name and re.match(r"^[啊哎欸嘿喂呀哦，,！!….\s]{0,8}" + re.escape(name) + r"\s*[，,！!]\s*[^，,！!？?。.\s…]", source) for name in names):
                explicit.append(s)
        # A continuing speech turn in a two-speaker exchange still addresses the other speaker.
        others = participants - {speaker}
        target = explicit[0] if len(explicit) == 1 else None if explicit else next(iter(others)) if len(others) == 1 else None
        if target in profiles:
            result["addressee"] = {"speaker_id": target, "gender": profiles[target].get("gender", "unknown")}
    if row.get('origin')=='interactive':
        try:
            metadata=json.loads(row.get('context_json') or '{}')
            message=metadata.get('interactive',{}) if isinstance(metadata,dict) else {}
        except (ValueError,TypeError):message={}
        if isinstance(message,dict):
            viewpoint=message.get('viewpoint')
            if message.get('narration') and isinstance(viewpoint,str) and viewpoint in profiles:
                result['viewpoint']={'speaker_id':viewpoint,'gender':profiles[viewpoint].get('gender','unknown'),'basis':'phone player note'}
            participants=message.get('participants')
            if isinstance(participants,list) and all(isinstance(s,str) for s in participants):
                others=set(participants)-{speaker}
                if speaker and len(others)==1 and next(iter(others)) in profiles:
                    target=next(iter(others))
                    result['addressee']={'speaker_id':target,'gender':profiles[target].get('gender','unknown')}
    return result

def identity_warning(row, translated, profiles):
    from .source_agreement import named_subject_gender_warning
    named_warning=named_subject_gender_warning(row,translated,profiles)
    if named_warning:return named_warning
    source = row.get("source_text") or ""
    kinds = source_species(source)
    mentioned = {key for key, (_, target) in SPECIES.items() if re.search(target, visible_text(translated), re.I)}
    if len(kinds) == 1 and mentioned - kinds:
        return "Species changed: source=" + ",".join(sorted(kinds)) + "; translation=" + ",".join(sorted(mentioned))
    if row.get("kind") == "character_name":
        expected = literal_name(source)
        if expected and translated != expected:
            return "Displayed identity must be preserved: " + expected
    if row.get("viewpoint"):
        warning = russian_gender_warning(row["viewpoint"].get("gender"), translated)
        if warning: return "Narrator: " + warning
        warning = implicit_narrator_gender_warning(row, translated, profiles)
        if warning: return warning
    gender = (row.get("addressee") or {}).get("gender")
    if gender in {"male", "female"}:
        wrong = set(GENDER_FORMS.values()) if gender == "male" else set(GENDER_FORMS) | {"пришел", "ушел", "пошел"}
        view = speaker_view(translated)
        view = re.sub(r"[—–]|(?<=\s)-(?=\s)", ";", view)
        for match in re.finditer(r"\bты\b([^.!?;:\n,]{0,120})", view, re.I):
            for word in re.findall(r"[А-Яа-яЁё]+", match[1].lower()):
                if word in {"он", "она", "оно", "они", "я", "вы", "мы", "что", "когда", "если",
                            "мама", "папа", "мать", "отец", "сестра", "брат", "сын", "дочь", "жена", "муж", "бабушка", "дедушка"}: break
                if word in wrong: return f"Addressee gender conflicts ({gender}): ты ... {word}"
        # Omitted "ты" is safe to attribute only in a narrow thank-you clause
        # followed by this listener's known name placeholder. Do not infer the
        # subject of every gendered verb from the current addressee.
        target = profiles.get((row.get("addressee") or {}).get("speaker_id"), {})
        for var in target.get("interpolation_variables", []):
            pattern = (r"\b(?:спасибо|благодарю)(?:\s+тебя)?\s*,?\s*что\s+"
                       r"(?:(?:ты|всё-таки|все-таки|наконец|не)\s+){0,3}([А-Яа-яЁё]+)\s*,?\s*\["
                       + re.escape(var) + r"\](?:[.!?,]|$)")
            for match in re.finditer(pattern, translated, re.I):
                if match[1].lower() in wrong:
                    # Quoted speech can address someone else.
                    if not view[match.start():match.start() + 7].strip():
                        continue
                    return f"Addressee gender conflicts ({gender}): спасибо, что {match[1]}"
    for profile in profiles.values():
        if profile.get("gender") not in {"male", "female"}: continue
        bad = "моя|дорогая|любимая" if profile["gender"] == "male" else "мой|дорогой|любимый"
        for var in profile.get("interpolation_variables", []):
            if re.search(r"\b(?:"+bad+r")\s+\["+re.escape(var)+r"\]", translated, re.I):
                return "Agreement conflicts with player placeholder gender: " + var
    return ""


def implicit_narrator_gender_warning(row, translated, profiles):
    """Validate omitted I only when the source actually attributes the action.

    A scene viewpoint alone is insufficient: the narrator also describes other
    people. Never mechanically change suffixes or gender quoted speech.
    """
    gender = (row.get("viewpoint") or {}).get("gender")
    if gender not in {"male", "female"}:
        return ""
    source = speaker_view(row.get("source_text", ""))
    reference = speaker_view(row.get("reference_en", ""))
    explicit_i = bool(re.search(r"(?:^|[。！？.!?，,；;…])\s*我(?!的|妈|爸|哥|姐|弟|妹|家)|\bI\b", source) or re.search(r"\bI\b", reference))
    if not explicit_i or re.search(r"[他她它]|\b(?:he|she|they|her|his)\b", source + " " + reference, re.I):
        return ""
    viewpoint = (row.get("viewpoint") or {}).get("speaker_id")
    for sid, profile in profiles.items():
        name = profile.get("display_name") or ""
        if sid != viewpoint and len(name) >= 2 and name in source:
            return ""
    past = {m: f for m, f in GENDER_FORMS.items() if m.endswith(("л", "лся")) or m in {"мог", "смог"}}
    wrong = set(past.values()) if gender == "male" else set(past) | {"пришел", "ушел", "пошел"}
    view = speaker_view(translated)
    if re.search(r"\b(?:он|она|они|мама|папа|мать|отец|девушка|женщина|мужчина)\b", view, re.I):
        return ""
    # Only verbs at a clause opening, with a small list of subject-free modifiers.
    # A following noun/adjective is not treated as the narrator's predicate.
    prefix = r"(?:после\s+того,?\s*как|в\s+итоге|всё\s+же|все\s+же|но|однако|снова|опять|потом|затем|уже|ещё|еще|наконец|не|и)"
    for match in re.finditer(r"(?:^|[.!?;,…])\s*[.…—-]*\s*(?:" + prefix + r"\s+)*([а-яё]+)", view, re.I):
        word = match[1].lower()
        # Without explicit "я", unrestricted -ла also matches nouns: "Правила
        # стали понятны". Unknown forms are safe here only when reflexive.
        productive_female = gender == "male" and bool(re.fullmatch(r"[а-яё]{2,}(?:[аеиыя]ла|нула)сь", word))
        if word in {"сила", "скала", "акула", "пчела", "кукла", "метла", "смола", "игла", "стрела"}:
            continue
        # Russian can put a non-personal subject AFTER these predicates:
        # "Осталась надежда", "Пришла пора", "Была возможность". Source I
        # alone does not prove that this verb belongs to the narrator. Let the
        # source-aware editor resolve it; don't force a masculine suffix.
        if word in {"был", "была", "остался", "осталась", "пришёл", "пришел", "пришла",
                    "мог", "могла", "смог", "смогла", "стоял", "стояла", "лежал", "лежала", "сидел", "сидела"}:
            tail = re.split(r"[.!?;,]", view[match.end():], maxsplit=1)[0]
            if re.search(r"[а-яё]", tail, re.I):
                continue
        if word in wrong or productive_female:
            return f"Narrator: omitted-I Russian gender conflicts ({gender}): {word}; согласуй действие героя даже без местоимения я"
    return ""
