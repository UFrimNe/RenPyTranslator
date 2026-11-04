"""Model-family transport rules; translation meaning stays model-independent."""
from __future__ import annotations

import re


def model_family(options) -> str:
    def get(key, default=""):
        return options.get(key, default) if isinstance(options, dict) else getattr(options, key, default)
    # The local alias rpts-local is shared by different models. Prefer its actual
    # identity; do not infer capabilities from that alias or from a stale name.
    name = str(get("model_identity") or get("model")).lower()
    if "translategemma" in name:
        return "translategemma"
    if re.search(r"hy[-_]?mt2", name):
        return "hy_mt2"
    if re.search(r"qwen3[._]5", name):
        return "qwen3_5"
    if re.search(r"(?:^|[/\s])deepseek-r1-7b(?:[:._/-]|$)|deepseek-r1-distill-(?:qwen|llama)(?:[-:/]|$)", name):
        return "deepseek_r1_distill"
    match = re.search(r"(?:^|[/\s])qwen3[-:](\d+(?:\.\d+)?)b(?:[:._/-]|$)", name)
    if match:
        return "qwen3_small" if float(match[1]) <= 4 else "qwen3"
    return "generic"


def uses_compact_instructions(options) -> bool:
    return model_family(options) == "qwen3_small"


def uses_single_text(options) -> bool:
    """Hy-MT2 must never map multiple translations onto forced JSON slots."""
    protocol = options.get("response_protocol") if isinstance(options, dict) else getattr(options, "response_protocol", "")
    return model_family(options) == "hy_mt2" or protocol == "single_text"


def normalize_translation_config(config):
    """Upgrade old/manual local configs as well as freshly generated presets."""
    if uses_single_text(config.get("provider") or {}):
        config.setdefault("provider", {}).update(response_protocol="single_text", compact_json=False, constrain_russian=False)
        config.setdefault("batch", {}).update(max_segments=1, mode_max_segments={"story": 1, "dialogue": 1, "narration": 1, "ui": 1})
    if model_family(config.get("provider") or {}) == "hy_mt2":
        config.setdefault("quality", {}).setdefault("native_free_review", False)
    return config


def uses_source_only_review(config):
    return model_family(config.get("provider") or {}) == "hy_mt2" and not config.get("quality", {}).get("native_free_review", False)


def is_r1_distill(options) -> bool:
    return model_family(options) == "deepseek_r1_distill"


def chat_messages(options, system_prompt: str, user_prompt: str) -> list[dict[str, str]]:
    family = model_family(options)
    # /no_think is a Qwen3 soft switch, not a universal chat instruction.
    # Never alter input data, which may itself contain this literal string.
    if family in {"qwen3", "qwen3_small"}:
        thinking = options.get("enable_thinking") if isinstance(options, dict) else getattr(options, "enable_thinking", None)
        if thinking is False:
            user_prompt += "\n/no_think"
    if family in {"deepseek_r1_distill", "hy_mt2", "translategemma"}:
        return [{"role": "user", "content": system_prompt + "\n\n" + user_prompt}]
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}]


def supports_thinking_switch(options):
    return model_family(options) in {"qwen3", "qwen3_small", "qwen3_5"}


def english_instructions(options):
    return model_family(options) in {"hy_mt2", "qwen3_5", "translategemma"}


def task_sampling(options, task="translation"):
    family = model_family(options)
    if family == "hy_mt2":
        return dict(temperature=0.7, top_p=0.6, top_k=20, min_p=0.0, repeat_penalty=1.05, presence_penalty=None)
    if family == "qwen3_5":
        return dict(temperature=0.7, top_p=0.8, top_k=20, min_p=0.0, repeat_penalty=None, presence_penalty=1.5)
    if family == "translategemma":
        return dict(temperature=0.0, top_p=1.0, top_k=0, min_p=0.0, repeat_penalty=None, presence_penalty=None)
    reasoning = family == "deepseek_r1_distill"
    return dict(temperature=0.6 if reasoning else 0.1 if task == "profile" else 0.7,
                top_p=0.95 if reasoning else 0.8, top_k=0 if reasoning else 20, min_p=0.0,
                repeat_penalty=None, presence_penalty=None)


# Experimental instruction mode. Google's native TranslateGemma template accepts
# language-tagged translation turns, not arbitrary JSON analysis requests. Keep this
# override explicit and use the underlying Gemma 3 turn tokens for all RPTS tasks.
TRANSLATEGEMMA_TEMPLATE = """{{ bos_token }}{% for message in messages %}{{ '<start_of_turn>' + ('model' if message['role'] == 'assistant' else 'user') + '\n' + message['content'] | trim + '<end_of_turn>\n' }}{% endfor %}{% if add_generation_prompt %}{{ '<start_of_turn>model\n' }}{% endif %}"""
