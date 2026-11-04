"""Source-backed voice context: no inferred personality and no extra model call."""
from __future__ import annotations

import re
from .tokens import strip_protected_for_context

PROMPT_REVISION = "11.26"


def style_direction(mode: str, *, english: bool = True, compact: bool = False) -> str:
    if mode == "ui":
        return ("Style: clear, idiomatic interface text; concise controls, complete explanations. No conversational embellishments."
                if english else "Стиль: привычный русский интерфейс; короткие кнопки, полные пояснения, без разговорных добавлений.")
    if english:
        return (
            "Target style: idiomatic Russian fiction. Dialogue should sound spoken; inner thoughts should sound personal, not like a report. "
            "Read the exchange to distinguish teasing from hostility, reassurance from instruction, embarrassment from indifference. "
            "Choose words, particles and sentence rhythm that carry that intent at the SAME emotional intensity. "
            "Reshape syntax and idioms into natural Russian while preserving facts, implications and intent. "
            "Do not turn intimate or casual speech into ceremonial politeness, or flatten a joke into an explanation. "
            "Preserve metaphors, subtext, hesitation and unfinished thoughts. Neutral lines stay neutral. "
            "Voice samples are this speaker's earlier SOURCE lines, not extra translation targets or facts to import. "
            "The current situation overrides habitual voice. No added feelings, stage directions, jokes or fashionable slang."
            " Affection, shyness and a soft voice never change grammatical gender. Unknown gender is not female; prefer natural neutral wording when evidence is absent."
        )
    if compact:
        return ("Живая речь: выбери слова, частицы и ритм по намерению реплики в сцене. Подкол не превращай в ссору, заботу — в инструктаж. "
                "Сохрани силу эмоции, подтекст и недосказанность; нейтральное не украшай. Нежность не меняет род. Неизвестный род не означает женский; по возможности используй естественную нейтральную конструкцию. voice_samples — прежняя речь того же героя, только ориентир манеры, не факты для перевода.")
    return ("ЭМОЦИЯ И ГОЛОС: сначала пойми намерение реплики по разговору: подкол, забота, смущение, раздражение или спокойный ответ. "
            "Подбери русские слова, частицы и ритм с той же силой чувства. Свободно перестраивай синтаксис и идиомы под естественный русский, сохраняя факты, подтекст и намерение. Близкое общение не превращай в церемонную вежливость, "
            "шутку — в объяснение, внутреннюю мысль — в отчёт. Сохрани метафоры, подтекст, паузы и недоговорённость. "
            "voice_samples — прежние исходные реплики этого же героя: только пример манеры, не дополнительные строки для перевода. "
            "Текущая ситуация важнее привычной манеры. Нежность, смущение и мягкая речь не меняют род. Неизвестный род не означает женский; по возможности используй естественную нейтральную конструкцию. Не добавляй эмоции, ремарки, шутки или модный сленг; нейтральный текст остаётся нейтральным.")


def source_voice_samples(row, context, by_file, index, *, max_chars=320):
    """Up to two past utterances from this speaker AND scene, never another branch.

    Current neighbours already supply voice when they contain two usable own
    lines. Samples fill only that gap. Long lines are skipped, never cut mid-word.
    Unattributed catalogue strings and narration cannot establish a speaker.
    """
    speaker = row.get("speaker")
    if not speaker or row.get("kind") not in {"dialogue", "dialogue_extend"}:
        return []
    pos = index.get(row["segment_id"])
    if not pos or not row.get("label"):
        return []
    neighbours = [*context.get("before", []), *context.get("after", [])]
    own = [r for r in neighbours if r.get("speaker") == speaker and len(re.findall(r"\w", r.get("text", ""))) >= 6]
    if len(own) >= 2:
        return []
    excluded = {r.get("id") for r in neighbours}
    seen = {row.get("source_text", ""), *(r.get("text", "") for r in own)}
    result, used = [], 0
    filename, offset = pos
    scope=by_file[filename][offset].get('_context_scope')
    for candidate in reversed(by_file.get(filename, [])[max(0, offset - 48):offset]):
        if scope is not None and candidate.get('_context_scope')!=scope:
            break
        if candidate.get("label") != row.get("label"):
            break  # Do not cross a branch and then re-enter a label.
        if (candidate.get("speaker") != speaker or candidate.get("kind") not in {"dialogue", "dialogue_extend"}
                or candidate.get("origin") != row.get("origin") or candidate["segment_id"] in excluded):
            continue
        text = strip_protected_for_context(candidate["source_text"])
        if text in seen or len(re.findall(r"\w", text)) < 6 or len(text) > min(180, max_chars - used):
            continue
        result.append({"id": candidate["segment_id"], "speaker": speaker, "text": text})
        seen.add(text); used += len(text)
        if len(result) + len(own) >= 2:
            break
    return list(reversed(result))
