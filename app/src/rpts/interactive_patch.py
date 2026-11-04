"""Optional runtime adapter for the statically recognized Emberbound phone."""
from __future__ import annotations
import json
import textwrap


def phone_mapping(rows, inventory):
    accepted = {r["segment_id"]: r for r in rows if r.get("origin") == "interactive" and r.get("status") in {"translated", "translated_tm", "approved"} and r.get("translation") and r.get("provider") != "mock"}
    mapping = {}
    ambiguous = set()
    coverage = []
    for message in inventory.get("messages", []):
        if message.get("debug"):
            continue
        row = accepted.get(message["segment_id"])
        adapter = message.get("adapter") == "emberbound_phone"
        key = message["signature"] + ":" + str(message["index"])
        if row and adapter:
            record = [message["text"], row["translation"]]
            if key in mapping and mapping[key] != record:
                ambiguous.add(key)
            mapping[key] = record
        coverage.append(dict(runtime_key=key, segment_id=message["segment_id"], file=message["file"], line=message["line"],
            speaker=message["speaker"], source_text=message["text"],
            status="context_adapter" if row and adapter else "string_lookup" if row else "untranslated"))
    for key in ambiguous:
        mapping.pop(key, None)
    for item in coverage:
        if item["runtime_key"] in ambiguous:
            item["status"] = "ambiguous_runtime_context"
    return mapping, coverage


def phone_bootstrap(mapping, language):
    if not mapping:
        return ""
    body = '''
import hashlib as _rpts_phone_hashlib
import json as _rpts_phone_json
_rpts_phone_map = __MAP__
_rpts_phone_language = __LANG__

class _RPTSPhoneText(str):
    def __new__(cls, source, key=""):
        value = str.__new__(cls, source)
        value.rpts_key = key
        return value
    def __getnewargs__(self):
        return (str(self), getattr(self, "rpts_key", ""))

def _rpts_phone_signature(kind, game_time, messages):
    rows = [[str(m.get("speaker", "")), str(m.get("text", ""))] for m in messages or []]
    return _rpts_phone_hashlib.sha256(_rpts_phone_json.dumps([kind, game_time, rows], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()

def _rpts_phone_seen_key(kind, title, game_time, messages):
    # Preserve the exact original source strings and game's persistent seen key.
    result = _rpts_phone_original_seen(kind, title, game_time, messages)
    signature = _rpts_phone_signature(kind, game_time, messages)
    for index, message in enumerate(messages or []):
        source = message.get("text", "")
        if not isinstance(source, str):
            continue
        key = signature + ":" + str(index)
        record = _rpts_phone_map.get(key)
        if record and record[0] == source:
            message["text"] = _RPTSPhoneText(str(source), key)
        elif isinstance(source, _RPTSPhoneText):
            message["text"] = str(source)
    return result

def _rpts_phone_localized_text(text):
    if isinstance(text, _RPTSPhoneText) and preferences.language == _rpts_phone_language:
        record = _rpts_phone_map.get(getattr(text, "rpts_key", ""))
        if record and record[0] == text:
            return record[1]
    return _rpts_phone_original_text(text)

if callable(globals().get("phone_ui_seen_key")) and callable(globals().get("phone_ui_localized_text")):
    if phone_ui_seen_key is not _rpts_phone_seen_key:
        _rpts_phone_original_seen = phone_ui_seen_key
        _rpts_phone_original_text = phone_ui_localized_text
        phone_ui_seen_key = _rpts_phone_seen_key
        phone_ui_localized_text = _rpts_phone_localized_text
# Localize baked Chinese text only for the verified original image. The game
# artwork stays in its archive; the patch adds a text displayable at render time.
class _RPTSPhoneAssets(str):
    def __add__(self, suffix):
        path = str.__add__(self, suffix)
        if suffix == "incoming_call_background.png" and preferences.language == _rpts_phone_language and _rpts_phone_artwork_verified:
            return Composite((608, 1222), (0, 0), path,
                (230, 965), Solid("#f1e6dd", xsize=160, ysize=54),
                (166, 978), Text("Проведите, чтобы ответить", font="DejaVuSans.ttf", size=19, color="#b49b84", xsize=300, text_align=0.5))
        return path

_rpts_phone_artwork_verified = False
if isinstance(globals().get("PHONE_CALL_UI"), str) and callable(globals().get("phone_ui_seen_key")):
    try:
        with renpy.file(str(PHONE_CALL_UI) + "incoming_call_background.png") as _rpts_phone_asset:
            _rpts_phone_artwork_verified = _rpts_phone_hashlib.sha256(_rpts_phone_asset.read()).hexdigest() == "42dcac573b48fe3ac9521e339127c1dd3fe00206d8b30254834e54ac3ca37c23"
        if _rpts_phone_artwork_verified:
            PHONE_CALL_UI = _RPTSPhoneAssets(str(PHONE_CALL_UI))
        renpy.log("RPTS phone artwork adapter: " + str(_rpts_phone_artwork_verified))
    except Exception as _rpts_phone_error:
        renpy.log("RPTS phone artwork adapter skipped: " + str(_rpts_phone_error))
'''.replace("__MAP__", repr(mapping)).replace("__LANG__", repr(language))
    return "\ninit 999 python:\n" + textwrap.indent(body.strip(), "    ") + "\n"
