from __future__ import annotations

import re

from .tokens import scan_protected_tokens


GENDER_FORMS = {
    "был": "была", "готов": "готова", "рад": "рада", "уверен": "уверена",
    "сам": "сама", "сделал": "сделала", "сказал": "сказала", "подумал": "подумала",
    "пришёл": "пришла", "ушёл": "ушла", "смог": "смогла", "хотел": "хотела",
    "решил": "решила", "понял": "поняла", "согласен": "согласна", "должен": "должна",
    "устал": "устала", "занят": "занята", "вернулся": "вернулась", "увидел": "увидела",
    "ждал": "ждала", "знал": "знала", "забыл": "забыла", "вспомнил": "вспомнила",
    "слышал": "слышала", "пошёл": "пошла", "спал": "спала", "заснул": "заснула", "проснулся": "проснулась",
    "любил": "любила", "думал": "думала", "заметил": "заметила", "мог": "могла",
    "почувствовал": "почувствовала", "посмотрел": "посмотрела", "смотрел": "смотрела",
    "пытался": "пыталась", "остался": "осталась", "поднял": "подняла", "счастлив": "счастлива", "виноват": "виновата", "намерен": "намерена",
    "один": "одна", "отправил": "отправила", "поверил": "поверила", "потянул": "потянула",
    "договорился": "договорилась", "вдохнул": "вдохнула", "взял": "взяла", "получил": "получила",
    "написал": "написала", "ответил": "ответила", "позвонил": "позвонила", "купил": "купила",
    "попросил": "попросила", "положил": "положила", "нашёл": "нашла", "вышел": "вышла",
    "приехал": "приехала", "уехал": "уехала", "поступил": "поступила", "окончил": "окончила",
    "работал": "работала", "жил": "жила", "сидел": "сидела", "стоял": "стояла", "лежал": "лежала",
    "понимал":"понимала", "перевернулся":"перевернулась", "занимался":"занималась", "признавал":"признавала",
    "подошёл":"подошла", "отошёл":"отошла", "включил":"включила", "выключил":"выключила",
}


def visible_text(text: str) -> str:
    """Mask game syntax without changing character offsets."""
    chars = list(text)
    for token in scan_protected_tokens(text):
        chars[token.start:token.end] = " " * (token.end - token.start)
    return "".join(chars)


def speaker_view(text: str) -> str:
    """Also mask quotations: their grammatical gender need not match the speaker."""
    masked = visible_text(text)
    chars = list(masked)
    for match in re.finditer(r'''«[^»]*»|“[^”]*”|„[^“]*“|「[^」]*」|『[^』]*』|"[^"\n]*"|(?<!\w)'[^'\n]+'(?!\w)''', masked):
        chars[match.start():match.end()] = " " * (match.end() - match.start())
    return "".join(chars)


def first_person_words(text: str):
    view = speaker_view(text)
    # Parenthetical commas must not hide the subject: "я, наверное, поверила".
    view = re.sub(r",\s*(?:наверное|наверно|конечно|кажется|пожалуй|видимо|возможно|честно говоря)\s*,",
                  lambda m: " " * len(m[0]), view, flags=re.I)
    # An em/en dash (or spaced ASCII dash) can introduce another subject.
    view = re.sub(r"[—–]|(?<=\s)-(?=\s)", ";", view)
    for match in re.finditer(r"\bя\b([^.!?;:\n,]{0,180})", view, re.I):
        for word in re.finditer(r"[А-Яа-яЁё]+", match.group(1)):
            value = word.group(0).lower()
            if value in {"он", "она", "оно", "они", "ты", "вы", "мы", "что", "чтобы", "когда", "если", "которая", "который",
                         "мама", "папа", "мать", "отец", "сестра", "брат", "сын", "дочь", "жена", "муж", "бабушка", "дедушка"}:
                break
            yield match.start(1) + word.start(), match.start(1) + word.end(), value


def _productive_first_person_feminine(text: str):
    """Recognize productive past forms only directly after I + particles.

    This is a validation hint, never an automatic suffix replacement. Objects,
    other subjects, quotes and nominal predicates must not inherit speaker gender.
    """
    view = speaker_view(text)
    view = re.sub(r",\s*(?:наверное|наверно|конечно|кажется|пожалуй|видимо|возможно|честно говоря)\s*,",
                  lambda m: " " * len(m[0]), view, flags=re.I)
    modifiers = {"не", "уже", "ещё", "еще", "только", "тоже", "ведь", "же", "бы", "просто", "так",
                 "снова", "опять", "сразу", "вчера", "сегодня", "тогда", "потом", "давно", "всегда",
                 "никогда", "наконец", "совсем", "даже", "искренне", "глубоко", "случайно", "почти",
                 "сначала", "сперва", "быстро", "медленно", "осторожно", "тихо", "резко", "слегка", "незаметно"}
    nominal = {"сила", "скала", "акула", "пчела", "кукла", "метла", "смола", "игла", "стрела"}
    for match in re.finditer(r"\bя\s+([^.!?;:\n,]{1,160})", view, re.I):
        for word in re.findall(r"[а-яё]+", match[1].lower()):
            if word in modifiers:
                continue
            if word not in nominal and re.fullmatch(r"[а-яё]{2,}(?:[аеиыя]ла|нула)(?:сь)?", word):
                yield word
            break


def russian_gender_warning(gender: str, text: str) -> str:
    if gender not in {"male", "female"}:
        return ""
    wrong = set(GENDER_FORMS.values()) if gender == "male" else set(GENDER_FORMS) | {"пришел", "ушел", "пошел"}
    for _start, _end, word in first_person_words(text):
        if word in wrong:
            return f"first-person Russian gender form conflicts with character profile ({gender}): {word}"
    if gender == "male":
        for word in _productive_first_person_feminine(text):
            return f"first-person Russian gender form conflicts with character profile ({gender}): {word}"
    return ""


def repair_first_person(gender: str, text: str) -> tuple[str, list[str]]:
    if gender not in {"male", "female"}:
        return text, []
    replacements = {v: k for k, v in GENDER_FORMS.items()} if gender == "male" else dict(GENDER_FORMS)
    if gender == "female":
        replacements.update({"пришел": "пришла", "ушел": "ушла", "пошел": "пошла"})
    edits = []
    for start, end, word in first_person_words(text):
        replacement = replacements.get(word)
        if replacement:
            original = text[start:end]
            if original.isupper():
                replacement = replacement.upper()
            elif original[:1].isupper():
                replacement = replacement[:1].upper() + replacement[1:]
            edits.append((start, end, replacement, original))
    out = text
    for start, end, replacement, _original in reversed(edits):
        out = out[:start] + replacement + out[end:]
    return out, [f"{original} -> {replacement}" for _, _, replacement, original in edits]


def repair_known_script_mixes(source: str, translated: str) -> tuple[str, list[str]]:
    """Fix only the observed ST + Cyrillic студия spelling, never transliterate.

    The allowlist is whole-word and excludes source brands, URLs, mail and game
    syntax. Unknown mixtures still fail validation and require model repair.
    """
    forms = {"stуд" + ending: "студ" + ending for ending in
             ("ия", "ии", "ию", "ией", "ий", "иям", "иями", "иях")}
    view = visible_text(translated)
    exempt = [(m.start(), m.end()) for m in re.finditer(r"https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]+", translated)]
    source_words = set(re.findall(r"[A-Za-zА-Яа-яЁё]+", source.lower()))
    edits = []
    for match in re.finditer(r"(?<!\w)[A-Za-zА-Яа-яЁё]+(?!\w)", view):
        word = match[0]
        replacement = forms.get(word.lower())
        if not replacement or word.lower() in source_words or any(a <= match.start() < b for a, b in exempt):
            continue
        if word.isupper(): replacement = replacement.upper()
        elif word[:1].isupper(): replacement = replacement.capitalize()
        edits.append((match.start(), match.end(), replacement, word))
    for start, end, value, _ in reversed(edits):
        translated = translated[:start] + value + translated[end:]
    return translated, [f"script spelling: {old} -> {value}" for _, _, value, old in edits]


def russian_issues(source: str, translated: str, kind: str = "dialogue", *, include_style_warnings: bool = False) -> list[str]:
    issues = []
    visible = visible_text(translated)
    if re.search(r"</?think>|⟪RPTS?\d|\b(?:Here is (?:the )?translation|Translation:)\b", translated, re.I):
        issues.append("service text or reasoning leaked into translation")
    # Dialogue inside quotations still needs translation. Protected game
    # identifiers remain exempt from language checks.
    han = list(dict.fromkeys(re.findall(r"[\u3400-\u9fff\uf900-\ufaff\U00020000-\U0002FA1F]+", visible)))
    if han:
        issues.append("untranslated Chinese text remains outside protected markup: " + ", ".join(han[:8]) + "; переведи эти фрагменты в составе всей фразы")
    for word in re.findall(r"[A-Za-zА-Яа-яЁё]+", visible):
        if re.search(r"[A-Za-z]", word) and re.search(r"[А-Яа-яЁё]", word):
            issues.append("Mixed Latin/Cyrillic letters inside a word: " + word)
            break
    source_visible = visible_text(source)
    han_source = re.findall(r"[\u3400-\u9fff]", source_visible)
    if kind in {"dialogue", "dialogue_extend", "narration", "centered", "localized_string"} and han_source:
        # Catch observed whole-scene expansion of a tiny turn. This is a
        # conservative scope alarm, not semantic equivalence or truncation.
        words = re.findall(r"[А-Яа-яЁё]+", visible)
        source_letters = re.sub(r"[\W_]+", "", source_visible)
        interjection = re.fullmatch(r"[啊呀嗯唔哦喔噢哎诶欸呃哈唉啧哼]+", source_letters)
        if (interjection and len(han_source) <= 6 and (len(words) > 8 or len(visible) > 80)
                or re.fullmatch(r"[\u3400-\u9fff]{1,6}", source_letters)
                and len(words) > 24 and len(visible) > 140):
            issues.append("Short source expanded into unrelated speech: translate only this passage, never neighbouring turns")
    if ("Рука подняла меня" in translated and not re.search(r"手|臂|рук|\b(?:hand|arm)\b", source_visible, re.I)):
        issues.append("An unrelated example from an old instruction leaked into the output; translate only the source")
    # A Cyrillic name does not make an otherwise English sentence Russian.
    # Keep real titles/quoted English already present in the original exempt.
    for phrase in re.findall(r"[A-Za-z]+(?:['’][A-Za-z]+)?(?:[ \t]+[A-Za-z]+(?:['’][A-Za-z]+)?){2,}", visible):
        if (re.search(r"\b(?:you|your|I'm|don't|doesn't|is|are|was|were|should|please|stop)\b", phrase, re.I)
                and phrase.lower() not in source_visible.lower()):
            issues.append("Untranslated English sentence remains in Russian output: " + phrase)
            break
    if (kind != "character_name" and re.search(r"[\u3400-\u9fff]", source_visible)
            and re.search(r"[A-Za-z]", visible) and not re.search(r"[А-Яа-яЁё]", visible)
            and visible.strip().lower() not in source_visible.lower()):
        issues.append("Chinese source was replaced with Latin text instead of Russian")
    if kind in {"dialogue", "dialogue_extend", "narration", "centered", "localized_string", "ui", "marked_string", "notification"}:
        source_visible = visible_text(source)
        linguistic_source = re.sub(r"https?://\S+|[\w.+-]+@[\w.-]+\.[A-Za-z]+", "", source_visible)
        if len(re.findall(r"[A-Za-z]+", linguistic_source)) >= 4 and not re.search(r"[А-Яа-яЁё]", visible) and re.search(r"[A-Za-z]", visible):
            issues.append("line contains no Russian text")
        if include_style_warnings and len(re.findall(r"[А-Яа-яЁё]", visible)) >= 8 and source.strip() != translated.strip():
            words = re.findall(r"[А-Яа-яЁё]+", visible.lower())
            for size in (3, 4):
                if any(words[i:i + size] == words[i + size:i + 2 * size] for i in range(len(words) - 2 * size + 1)):
                    # Repeated source speech is intentional; a report/repair is only
                    # triggered when the source has no corresponding repeated run.
                    source_words = re.findall(r"\w+", source_visible.lower())
                    if not any(source_words[j:j + size] == source_words[j + size:j + 2 * size] for j in range(len(source_words) - 2 * size + 1)):
                        issues.append("unexpected repeated phrase in translation")
                    break
    if kind in {"dialogue", "dialogue_extend", "narration", "centered", "localized_string"}:
        prose = speaker_view(translated).strip()
        if re.search(r"\b(?:мне|тебе|ему|ей|нам|вам|им)\s+было\s+(?:(?:совсем|очень|совершенно|не)\s+){0,3}нравилось\b", prose, re.I):
            issues.append("Russian grammar: incompatible было + нравилось; rewrite the complete sentence naturally")
        if re.search(r"\bс кем\s+(?:[а-яё-]+\s+){0,4}(?:ни|не) с кем\b", prose, re.I):
            issues.append("Russian grammar: duplicated incompatible с кем / ни с кем construction")
        if re.search(r"\b(?:какой(?:-то)?|этот|такой|любой|каждый)\s+(?:студия|компания|работа|жизнь|память)\b", prose, re.I):
            issues.append("Russian grammar: masculine determiner before a feminine nominative noun; fix agreement")
        if re.search(r"\b(?:какой(?:-то)?|этот|такой|любой|каждый)\s+место\b", prose, re.I):
            issues.append("Russian grammar: место is neuter; fix determiner agreement in the full sentence")
        if re.search(r"\b[а-яё]+(?:вшийся|ющийся|ущийся)\s+лицо\b", prose, re.I):
            issues.append("Russian grammar: masculine participle with neuter лицо; fix agreement")
        # A source ending in an ellipsis/dash may deliberately trail off.
        complete_source = bool(re.search(r"(?<!\.)[。！？!?](?:[\"”»])?$|(?<!\.)\.(?!\.)(?:[\"”»])?$", source_visible.strip()))
        tail = visible.strip().rstrip('"”»')
        if complete_source and (tail.endswith((",", ":", ";")) or re.search(r"\b(?:наконец-то|потому что|так как|для того чтобы|после того как)\s*$", tail, re.I)):
            issues.append("Translation is an unfinished fragment although the source sentence is complete; restore the missing action/clause")
    return issues


def coverage_warning(source: str, translated: str, reference: str = "") -> str:
    """A conservative omission signal, not a semantic quality score."""
    src = visible_text(source)
    ref = visible_text(reference)
    dst = visible_text(translated)
    han = len(re.findall(r"[\u3400-\u9fff]", src))
    ref_words = len(re.findall(r"[A-Za-z]+", ref))
    ru_words = len(re.findall(r"[А-Яа-яЁё]+", dst))
    if han >= 20 and ref_words >= 15 and ru_words < ref_words * 0.45:
        return "Possible omitted meaning: Russian is less than half the reference length; translate every action and clause, not a summary"
    return ""


def source_language_hints(source: str) -> list[str]:
    # Short lexical disambiguation only when the expression occurs in this line.
    # These are meanings, not ready-made translations of a game's dialogue.
    cues = {
        "发小": "发小 — друг детства; совместная учёба из этого не следует.",
        "火锅": "火锅 — хого, блюдо с общим котелком; не просто горячая еда.",
        "绿皮火车": "绿皮火车 — старый обычный/медленный поезд; слово 车 здесь относится к поезду.",
        "复试": "复试 — повторный этап испытания; в поиске работы это повторное собеседование.",
        "十几个": "十几个 — больше десяти, десять с лишним; не десятки и не ровно десять.",
        "任何形式": "任何形式 — любая форма; не оставляй эти иероглифы в русском предложении.",
        "直男审美": "直男审美 — стереотип о непритязательном вкусе гетеросексуальных мужчин; не буквальный «прямой вкус».",
        "下班收工": "下班收工 — рабочий день закончен; разговорная реплика после работы.",
        "互相关注": "互相关注 — взаимная подписка в социальной сети.",
        "虎头虎脑": "虎头虎脑 описывает крепкий, простодушный облик; не придумывай полноту или другой вид животного.",
        "狼吞虎咽": "狼吞虎咽 — жадно уплетать еду; это идиома, не указание на вид персонажа.",
        "五花肉": "五花肉 — свиная грудинка с прослойками мяса и жира, а не отбивная.",
        "猛兽": "猛兽 — хищный/свирепый зверь; в кавычках и дружеской сцене возможна шутливая гипербола. Не добавляй презрительную «тварь» без опоры на тон исходника.",
        "嘴馋": "嘴馋 — хочется поесть чего-нибудь вкусного; это не обязательно тяга к сладкому. Учитывай, какую еду обсуждают.",
        "肉麻": "肉麻 в разговоре о чувствах — приторная нежность/смущающие нежности. Выбери живую реакцию по отношению собеседников, не отвлечённую книжную оценку сентиментальности.",
        "滚滚滚": "Повтор 滚 в дружеской перепалке может быть шутливым отмахиванием, а не приказом физически уйти. Сопоставь с соседними репликами; при настоящей ссоре сохрани резкость.",
        "发呆": "发呆 — отвлечься, задуматься, смотреть в одну точку; не производственный простой или безделье.",
        "闲得发闷": "闲得发闷 — скучать от безделья. Это состояние говорящего, не характеристика другого человека.",
        "待机维修": "待机维修 — образ режима ожидания и ремонта/восстановления. Сохрани обе части самоироничной метафоры, не своди всё к ожиданию.",
    }
    hints = [hint for cue, hint in cues.items() if cue in source]
    if source.count('提醒')>=2:
        hints.append('Повтор 提醒 может быть устройством шутки: напоминаю тебе, чтобы ты напомнил мне. Сохрани повтор и роли, не своди фразу к обычной просьбе напомнить.')
    if re.search(r'(?:^|[，,。！!？?\s“「«\"])哥(?:带|给|来|陪)',source):
        hints.append('哥 здесь может быть шутливым самообозначением старшего приятеля. Проверь отношения и того, кто действует; не создавай буквальное родство и не добавляй «братик» автоматически.')
    if "优化" in source and re.search(r"被|公司|工作室|工作|裁员", source):
        hints.append("优化 в контексте работы и увольнения — эвфемизм сокращения штата, а не улучшение сотрудника.")
    return hints


def localization_rules(mode: str = "story", *, compact: bool = False) -> list[str]:
    profanity = ("Мат допустим, когда он естественно передаёт брань, грубость или резкую эмоцию исходной реплики в её контексте. "
                 "Не цензурируй уместный мат звёздочками и не смягчай его автоматически. Сохраняй силу, адресата и характер ругани: "
                 "не усиливай лёгкую досаду до оскорбления, не добавляй мат в нейтральную речь только из-за грубого профиля персонажа.")
    if mode == "ui":
        return ["Интерфейс: привычные русские названия действий. Полностью переводи пояснения и уведомления; не делай пересказ. Без разговорных добавлений. Части layout_context образуют одну строку: согласуй их, но не добавляй теги."]
    if compact:
        return [
            "АДАПТАЦИЯ: живой русский диалог и связное повествование. Перестраивай порядок слов, идиомы передавай по смыслу. Сохрани все действия, отрицания, числа, время, отношения, эмоции и намёки. Не добавляй факты, шутки и сравнения.",
            "Сохраняй ты/вы по ситуации. Мама/папа/哥/姐 могут обозначать самого говорящего. Не переноси дословно китайское построение или сухой английский. Проверяй падежи, род и сочетания слов.",
            profanity,
        ]
    return [
        "АДАПТАЦИЯ: пиши так, как люди говорят и думают по-русски. Перестраивай фразу, а не подставляй слова по очереди. Можно менять порядок слов, опускать лишние местоимения и делить длинное предложение внутри одной строки.",
        "Сохрани весь смысл: действия и их участников, отрицание, уверенность, время, числа, отношения, эмоции и намёки. Не добавляй шутки, сравнения, причины или детали. Каждая строка передаёт весь свой оригинал, а не краткий пересказ. Не разъясняй намеренную недосказанность.",
        "Короткий ответ связан с вопросом. Внутренний голос согласуй с viewpoint, включая фразы без я. Соблюдай ты/вы по текущему разговору. Сохраняй культурные детали, блюда и реалии. Нормальные безличные и нейтральные фразы разрешены.",
        "Китайские 哥/姐 и мама/папа могут быть самообозначением. Определи по контексту, кто кого зовёт или угощает. 喂 по телефону обычно «Алло», 嗯 — «Угу», «Да», «Хм» по смыслу; не механическое «Ум».",
        profanity,
        "Перед ответом проверь: фраза естественна, смысл полон, падежи и согласование верны, лишних деталей нет. Выводи только готовый текст.",
    ]
