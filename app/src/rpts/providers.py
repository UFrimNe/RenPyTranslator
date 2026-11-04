from __future__ import annotations

import json
import hashlib
import html
import os
import re
import urllib.error
import urllib.request
import urllib.parse
import time
from dataclasses import dataclass
from typing import Any

from .model_policy import chat_messages, is_r1_distill, supports_thinking_switch, uses_single_text
from . import diagnostics


class ProviderError(RuntimeError):
    pass


class ProviderRateLimitError(ProviderError):
    """Provider explicitly asked us to stop sending requests for now."""
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}: {detail}")


class ProviderCancelledError(ProviderError):
    """A pending retry was cancelled after another request failed."""


class ProviderContextLimitError(ProviderError):
    """One source segment cannot fit after removing auxiliary context."""
    pass


class ProviderBatchIntegrityError(ProviderError):
    """A batched response cannot be mapped back to input ids safely."""
    pass


class ProviderResponseFormatError(ProviderError):
    """Provider returned output that cannot be parsed as the required structure."""
    pass


class ProviderOutputTruncatedError(ProviderResponseFormatError):
    """Generation reached its output limit, rather than malformed JSON."""
    pass


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def _extract_json_payload(text: str) -> dict[str, Any]:
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.I)
        text = re.sub(r"\s*```$", "", text)
    try:
        value = json.loads(text, object_pairs_hook=_unique_object)
        if isinstance(value, dict):
            return value
    except Exception:
        pass
    # Some reasoning servers return a separate reasoning_content field, others
    # put <think> in content, sometimes with its opening tag already prefilled.
    # Discard that envelope only AFTER attempting real JSON, so a valid string
    # literal is never edited. Never accept a JSON draft inside unfinished thought.
    if "</think>" in text:
        text = text.rsplit("</think>", 1)[1].strip()
    elif text.startswith("<think>"):
        raise ProviderResponseFormatError("Reasoning ended without a final JSON answer")
    # Conservative fallback for models that add one sentence around the JSON.
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start:end + 1], object_pairs_hook=_unique_object)
            if isinstance(value, dict):
                return value
        except Exception as e:
            raise ProviderResponseFormatError(f"Provider returned invalid JSON: {e}") from None
    raise ProviderResponseFormatError("Provider did not return a JSON object")


def _http_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> dict[str, Any]:
    raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/json; charset=utf-8")
    req.add_header("Accept", "application/json")
    for key, value in headers.items():
        if value:
            req.add_header(key, value)
    trace = diagnostics.http_start(url, body, headers, timeout)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            response_bytes = resp.read()
            diagnostics.http_end(trace, response=response_bytes.decode("utf-8", errors="replace"), status=getattr(resp, "status", None))
            payload = response_bytes.decode("utf-8")
    except urllib.error.HTTPError as e:
        full_detail = e.read().decode("utf-8", errors="replace")
        diagnostics.http_end(trace, response=full_detail, error=str(e), status=e.code)
        detail = full_detail[:2000]
        if e.code == 429:
            retry_after = e.headers.get("Retry-After", "") if e.headers else ""
            extra = f" Retry-After={retry_after}" if retry_after else ""
            raise ProviderRateLimitError(f"HTTP 429:{extra} {detail}") from None
        raise ProviderHTTPError(e.code, detail) from None
    except UnicodeDecodeError:
        raise ProviderResponseFormatError("Provider HTTP response is not valid UTF-8") from None
    except Exception as e:
        diagnostics.http_end(trace, error=f"{type(e).__name__}: {e}")
        raise ProviderError(f"Provider request failed: {type(e).__name__}: {e}") from None
    try:
        value = json.loads(payload)
    except Exception as e:
        raise ProviderError(f"Provider returned non-JSON HTTP response: {e}: {payload[:1000]}") from None
    if not isinstance(value, dict):
        raise ProviderError("Provider HTTP response was not a JSON object")
    return value


@dataclass
class ProviderConfig:
    type: str = "mock"
    model: str = "mock"
    base_url: str = ""
    endpoint: str = ""
    api_key_env: str = "RPTS_API_KEY"
    timeout_seconds: float = 120.0
    temperature: float = 0.2
    max_retries: int = 2
    retry_delay_seconds: float = 2.0
    source_language_code: str = "auto"
    target_language_code: str = "ru"
    request_delay_seconds: float = 0.0
    structured_json: bool = False
    top_p: float = 0.9
    max_output_tokens: int = 0
    output_token_factor: float = 0.0
    output_token_min: int = 0
    compact_json: bool = False
    response_protocol: str = "source_echo"
    context_limit_tokens: int = 0
    token_preflight: bool = False
    model_identity: str = ""
    enable_thinking: bool | None = None
    cache_prompt: bool | None = None
    top_k: int | None = None
    min_p: float | None = None
    repeat_penalty: float | None = None
    presence_penalty: float | None = None
    constrain_russian: bool = False

    @classmethod
    def from_dict(cls, value: dict[str, Any] | None):
        value = value or {}
        return cls(
            type=str(value.get("type") or "mock"),
            model=str(value.get("model") or "mock"),
            base_url=str(value.get("base_url") or ""),
            endpoint=str(value.get("endpoint") or ""),
            api_key_env=str(value.get("api_key_env") or "RPTS_API_KEY"),
            timeout_seconds=float(value.get("timeout_seconds") or 120.0),
            temperature=float(value.get("temperature") if value.get("temperature") is not None else 0.2),
            max_retries=max(0, int(value.get("max_retries") if value.get("max_retries") is not None else 2)),
            retry_delay_seconds=max(0.0, float(value.get("retry_delay_seconds") if value.get("retry_delay_seconds") is not None else 2.0)),
            source_language_code=str(value.get("source_language_code") or "auto"),
            target_language_code=str(value.get("target_language_code") or "ru"),
            request_delay_seconds=max(0.0, float(value.get("request_delay_seconds") if value.get("request_delay_seconds") is not None else 0.0)),
            structured_json=bool(value.get("structured_json", False)),
            top_p=max(0.0, min(1.0, float(value.get("top_p") if value.get("top_p") is not None else 0.9))),
            max_output_tokens=max(0, int(value.get("max_output_tokens") if value.get("max_output_tokens") is not None else 0)),
            output_token_factor=max(0.0, float(value.get("output_token_factor") if value.get("output_token_factor") is not None else 0.0)),
            output_token_min=max(0, int(value.get("output_token_min") if value.get("output_token_min") is not None else 0)),
            compact_json=bool(value.get("compact_json", False)),
            response_protocol="single_text" if uses_single_text(value) else str(value.get("response_protocol") or "source_echo"),
            context_limit_tokens=max(0, int(value.get("context_limit_tokens") or 0)),
            token_preflight=bool(value.get("token_preflight", False)),
            model_identity=str(value.get("model_identity") or ""),
            enable_thinking=value.get("enable_thinking") if isinstance(value.get("enable_thinking"), bool) else None,
            cache_prompt=value.get("cache_prompt") if isinstance(value.get("cache_prompt"), bool) else None,
            top_k=max(0, int(value["top_k"])) if value.get("top_k") is not None else None,
            min_p=max(0.0, min(1.0, float(value["min_p"]))) if value.get("min_p") is not None else None,
            repeat_penalty=float(value["repeat_penalty"]) if value.get("repeat_penalty") is not None else None,
            presence_penalty=float(value["presence_penalty"]) if value.get("presence_penalty") is not None else None,
            constrain_russian=bool(value.get("constrain_russian", False)),
        )


def _single_text_payload(content: str) -> str:
    text = content.strip("\r\n")
    if text.startswith("<think>"):
        if "</think>" not in text:
            raise ProviderResponseFormatError("Reasoning ended without a final translation")
        text = text.split("</think>", 1)[1].strip()
    if not text.strip() or text.lstrip().startswith(("```", '{"t":', '{"translations":')):
        raise ProviderResponseFormatError("Expected one complete plain-text translation")
    if re.search(r"(?m)^(?:END SOURCE|END BACKGROUND|SOURCE \(translate|BACKGROUND \(do not translate|\[Source Text\]|\[Background Information)", text):
        raise ProviderResponseFormatError("Translation contains prompt scaffolding")
    return text


class BaseProvider:
    name = "base"

    def __init__(self, config: ProviderConfig):
        self.config = config

    def translate(self, *, system_prompt: str, user_prompt: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        raise NotImplementedError

    def output_budget(self, items: list[dict[str, Any]]) -> int:
        if self.config.max_output_tokens <= 0:
            return 0
        if self.config.output_token_factor > 0:
            source_chars = sum(len(str(item.get("text") or "")) for item in items)
            anchored = uses_single_text(self.config) or self.config.compact_json and self.config.response_protocol == "anchored_keys"
            framing = 12 if uses_single_text(self.config) else 28 if anchored else 40 if self.config.compact_json else 64
            dynamic = int(source_chars * (self.config.output_token_factor + (0.0 if anchored else 1.0)) + framing * max(1, len(items)))
            draft_chars = sum(len(str(item.get("draft") or "")) for item in items)
            if draft_chars:
                dynamic = max(dynamic, int(draft_chars * 0.75 + framing * max(1, len(items))))
            if is_r1_distill(self.config):
                dynamic += 1024  # Native R1 reasoning shares max_tokens with its final answer.
            return min(self.config.max_output_tokens, max(self.config.output_token_min or 1, dynamic))
        return self.config.max_output_tokens


class MockProvider(BaseProvider):
    name = "mock"

    def translate(self, *, system_prompt: str, user_prompt: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        target = "MOCK"
        m = re.search(r"TARGET_LANGUAGE:\s*(.+)", system_prompt)
        if m:
            target = m.group(1).strip()
        return [{"id": str(x["id"]), "text": f"⟦{target}⟧ {x['text']}"} for x in items]


def _single_text_repair_grammar(config, item):
    """llama.cpp-only repair constraint: each original marker exactly in order.

    Language and wording remain unrestricted. Remote OpenAI-compatible APIs are
    not assumed to accept llama.cpp's grammar extension.
    """
    if not item.get("repair_attempt") or urllib.parse.urlparse(config.base_url).hostname not in {"127.0.0.1", "localhost", "::1"}:
        return None
    if config.model != "rpts-local":
        return None
    markers = re.findall(r"⟪RPTS?\d+(?:_\d+)?⟫", str(item.get("text", "")))
    if not markers or len(markers) != len(set(markers)):
        return None
    return 'root ::= text ' + ' text '.join(json.dumps(m, ensure_ascii=False) for m in markers) + ' text\ntext ::= [^⟪⟫{}]*\n'


class OpenAICompatibleProvider(BaseProvider):
    name = "openai_compatible"

    def _headers(self) -> dict[str, str]:
        api_key = os.environ.get(self.config.api_key_env, "") if self.config.api_key_env else ""
        return {"Authorization": f"Bearer {api_key}" if api_key else ""}

    def tokenize_count(self, text: str) -> int | None:
        if not self.config.token_preflight:
            return None
        base = self.config.base_url.rstrip("/")
        root = base[:-3] if base.endswith("/v1") else base
        try:
            data = _http_json(root + "/tokenize", {"content": text}, self._headers(), min(30.0, self.config.timeout_seconds))
            tokens = data.get("tokens")
            return len(tokens) if isinstance(tokens, list) else None
        except ProviderRateLimitError:
            raise
        except ProviderHTTPError as exc:
            if exc.status_code in {401, 403}:
                raise
            return None
        except Exception:
            return None

    def translate(self, *, system_prompt: str, user_prompt: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        if uses_single_text(self.config):
            if len(items) != 1:
                raise ProviderBatchIntegrityError("Single-text translation requires exactly one target")
            text = self._generate_json(system_prompt=system_prompt, user_prompt=user_prompt,
                schema=None, budget=self.output_budget(items), raw_text=True,
                grammar=_single_text_repair_grammar(self.config, items[0]))
            return [{"id": str(items[0]["id"]), "text": text}]
        payload = self._generate_json(system_prompt=system_prompt, user_prompt=user_prompt,
            schema=_translation_schema(items, compact=self.config.compact_json, protocol=self.config.response_protocol) if self.config.structured_json else None,
            budget=self.output_budget(items),
            grammar=_russian_translation_grammar(items) if self.config.constrain_russian and not is_r1_distill(self.config) and self.config.enable_thinking is not True and self.config.compact_json and self.config.response_protocol == "anchored_keys" else None)
        return _translations_for_items(payload, items, compact=self.config.compact_json, protocol=self.config.response_protocol)

    def generate_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        return self._generate_json(system_prompt=system_prompt, user_prompt=user_prompt, schema=schema, budget=self.config.max_output_tokens)

    def _generate_json(self, *, system_prompt: str, user_prompt: str, schema: dict | None, budget: int, grammar: str | None = None, raw_text: bool = False) -> dict | str:
        base = self.config.base_url.rstrip("/")
        if not base:
            raise ProviderError("provider.base_url is required for openai_compatible")
        endpoint = self.config.endpoint or "/chat/completions"
        url = base + (endpoint if endpoint.startswith("/") else "/" + endpoint)
        headers = self._headers()
        body = {
            "model": self.config.model,
            "stream": False,
            "temperature": self.config.temperature,
            "top_p": self.config.top_p,
            "messages": chat_messages(self.config, system_prompt, user_prompt),
        }
        if self.config.enable_thinking is not None and supports_thinking_switch(self.config):
            body["chat_template_kwargs"] = {"enable_thinking": self.config.enable_thinking}
        if self.config.cache_prompt is not None:
            body["cache_prompt"] = self.config.cache_prompt
        for name in ("top_k", "min_p", "repeat_penalty", "presence_penalty"):
            if getattr(self.config, name) is not None:
                body[name] = getattr(self.config, name)
        if budget > 0:
            body["max_tokens"] = budget

        if grammar is not None:
            body["grammar"] = grammar
        elif schema is not None:
            body["response_format"] = {"type": "json_object", "schema": schema}
        data = _http_json(url, body, headers, self.config.timeout_seconds)
        try:
            choice = data["choices"][0]
            finish_reason = str(choice.get("finish_reason") or "")
            if finish_reason in {"length", "max_tokens"}:
                raise ProviderOutputTruncatedError(f"Provider output truncated: finish_reason={finish_reason}")
            content = choice["message"]["content"]
        except ProviderResponseFormatError:
            raise
        except Exception:
            raise ProviderError(f"Unexpected OpenAI-compatible response shape: {json.dumps(data, ensure_ascii=False)[:1500]}") from None
        if not isinstance(content, str):
            raise ProviderResponseFormatError("Provider message.content must be a string")
        return _single_text_payload(content) if raw_text else _extract_json_payload(content)


def _http_form_json_any(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    """POST application/x-www-form-urlencoded and return arbitrary JSON.

    Used only by the unofficial consumer Google Translate endpoint, whose JSON
    shape is not the same as Google Cloud Translation and can be either a dict
    (dj=1) or a nested list on fallback responses.
    """
    raw = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded; charset=utf-8")
    req.add_header("Accept", "application/json,text/plain,*/*")
    req.add_header("User-Agent", "Mozilla/5.0 RPTS/7.3")
    for key, value in headers.items():
        if value:
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail_full = e.read().decode("utf-8", errors="replace")
        detail = detail_full[:1200]
        retry_after = ""
        try:
            retry_after = e.headers.get("Retry-After", "")
        except Exception:
            pass
        extra = f" Retry-After={retry_after}" if retry_after else ""
        low = detail_full.lower()
        if e.code == 429 or (e.code == 403 and ("captcha" in low or "recaptcha" in low)):
            raise ProviderRateLimitError(
                f"HTTP {e.code}:{extra} Google Translate временно ограничил бесплатные запросы/CAPTCHA. "
                "Прогресс уже сохранён; не повторяй запросы сразу, запусти продолжение позже."
            ) from None
        raise ProviderError(f"HTTP {e.code}:{extra} {detail}") from None
    except Exception as e:
        raise ProviderError(f"Free Google Translate request failed: {type(e).__name__}: {e}") from None
    try:
        return json.loads(payload)
    except Exception as e:
        raise ProviderError(f"Free Google Translate returned non-JSON data: {e}: {payload[:800]}") from None




def _http_form_text(url: str, body: dict[str, Any], params: dict[str, Any], headers: dict[str, str], timeout: float) -> str:
    """POST form data and return raw text for Google Translate's browser RPC."""
    if params:
        query = urllib.parse.urlencode(params)
        url = url + ("&" if "?" in url else "?") + query
    raw = urllib.parse.urlencode(body).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST")
    req.add_header("Content-Type", "application/x-www-form-urlencoded;charset=UTF-8")
    req.add_header("Accept", "*/*")
    req.add_header("Origin", "https://translate.google.com")
    req.add_header("Referer", "https://translate.google.com/")
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36")
    for key, value in headers.items():
        if value:
            req.add_header(key, value)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        detail_full = e.read().decode("utf-8", errors="replace")
        retry_after = ""
        try:
            retry_after = e.headers.get("Retry-After", "")
        except Exception:
            pass
        extra = f" Retry-After={retry_after}" if retry_after else ""
        low = detail_full.lower()
        if e.code == 429 or (e.code == 403 and ("captcha" in low or "recaptcha" in low)):
            raise ProviderRateLimitError(
                f"HTTP {e.code}:{extra} Google Translate временно ограничил бесплатные запросы/CAPTCHA. "
                "Прогресс уже сохранён; не повторяй запросы сразу, запусти продолжение позже."
            ) from None
        raise ProviderError(f"HTTP {e.code}:{extra} {detail_full[:1200]}") from None
    except Exception as e:
        raise ProviderError(f"Free Google Translate browser RPC failed: {type(e).__name__}: {e}") from None


def _google_rpc_payload(raw: str) -> Any:
    """Extract the inner MkEWBc JSON payload from Google's batchexecute framing."""
    raw = raw or ""
    token_found = False
    buf = ""
    opens = closes = 0
    for line in raw.splitlines():
        if not token_found:
            if '"MkEWBc"' not in line:
                continue
            token_found = True
        in_string = False
        escaped = False
        for ch in line:
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
            else:
                if ch == '"':
                    in_string = True
                elif ch == '[':
                    opens += 1
                elif ch == ']':
                    closes += 1
        buf += line
        if opens and opens == closes:
            break
    if not buf:
        raise ProviderError(f"Google browser RPC response did not contain MkEWBc: {raw[:800]}")
    try:
        outer = json.loads(buf)
    except Exception as e:
        raise ProviderError(f"Google browser RPC returned malformed framing: {e}: {buf[:800]}") from None

    def find(node: Any):
        if isinstance(node, list):
            if len(node) >= 3 and node[1] == "MkEWBc" and isinstance(node[2], str):
                try:
                    return json.loads(node[2])
                except Exception as e:
                    raise ProviderError(f"Google browser RPC inner JSON is invalid: {e}") from None
            for child in node:
                found = find(child)
                if found is not None:
                    return found
        return None

    found = find(outer)
    if found is None:
        raise ProviderError(f"Google browser RPC framing had no MkEWBc payload: {buf[:800]}")
    return found


def _google_rpc_text(raw: str) -> str:
    parsed = _google_rpc_payload(raw)
    try:
        row = parsed[1][0][0]
        parts = row[5]
        separator = " " if row[3] else ""
        out = [str(part[0]) for part in parts if isinstance(part, list) and part and part[0] is not None]
        if out:
            return separator.join(out)
    except Exception:
        pass
    raise ProviderError(f"Unexpected Google browser RPC translation shape: {json.dumps(parsed, ensure_ascii=False)[:1200]}")

def _free_google_text(value: Any) -> str:
    """Extract translated text from known consumer endpoint response shapes."""
    if isinstance(value, dict):
        sentences = value.get("sentences")
        if isinstance(sentences, list):
            out = []
            for row in sentences:
                if isinstance(row, dict) and row.get("trans") is not None:
                    out.append(str(row["trans"]))
            if out:
                return "".join(out)
    # Legacy client=gtx response: [[[translated, source, ...], ...], ...]
    if isinstance(value, list) and value and isinstance(value[0], list):
        out = []
        for row in value[0]:
            if isinstance(row, list) and row and row[0] is not None:
                out.append(str(row[0]))
        if out:
            return "".join(out)
    raise ProviderError(f"Unexpected free Google Translate response shape: {json.dumps(value, ensure_ascii=False)[:1200]}")


class GoogleFreeTranslateProvider(BaseProvider):
    """Unofficial no-key Google Translate provider.

    Stage 7.5 uses the browser RPC endpoint (MkEWBc) as the primary transport.
    The older client=gtx endpoint remains a compatibility fallback only for
    non-rate-limit RPC failures. A CAPTCHA/429 is never bypassed by switching
    endpoints; RPTS stops immediately and resumes later from SQLite.
    """
    name = "google_free"

    @staticmethod
    def _marker(index: int, item_id: str, side: str) -> str:
        import hashlib
        digest = int.from_bytes(hashlib.sha1(item_id.encode("utf-8")).digest()[:4], "big") % 100000000
        side_digit = 1 if side == "A" else 2
        return f"⟪9{index:04d}{side_digit}{digest:08d}⟫"

    def _request_text_rpc(self, text: str, source: str, target: str) -> str:
        endpoint = self.config.endpoint or "https://translate.google.com/_/TranslateWebserverUi/data/batchexecute"
        if "translate_a/single" in endpoint:
            endpoint = "https://translate.google.com/_/TranslateWebserverUi/data/batchexecute"
        inner = json.dumps([[text, source, target, True], [None]], ensure_ascii=False, separators=(",", ":"))
        request_payload = json.dumps([[["MkEWBc", inner, None, "generic"]]], ensure_ascii=False, separators=(",", ":"))
        raw = _http_form_text(
            endpoint,
            {"f.req": request_payload},
            {
                "rpcids": "MkEWBc",
                "bl": "boq_translate-webserver_20201207.13_p0",
                "soc-app": 1,
                "soc-platform": 1,
                "soc-device": 1,
                "rt": "c",
            },
            {},
            self.config.timeout_seconds,
        )
        translated = html.unescape(_google_rpc_text(raw))
        if self.config.request_delay_seconds > 0:
            time.sleep(self.config.request_delay_seconds)
        return translated

    def _request_text_gtx(self, text: str, source: str, target: str) -> str:
        url = "https://translate.googleapis.com/translate_a/single"
        body = {"client": "gtx", "sl": source, "tl": target, "dt": "t", "dj": "1", "q": text}
        data = _http_form_json_any(url, body, {}, self.config.timeout_seconds)
        translated = html.unescape(_free_google_text(data))
        if self.config.request_delay_seconds > 0:
            time.sleep(self.config.request_delay_seconds)
        return translated

    def _request_text(self, text: str, source: str, target: str) -> str:
        try:
            return self._request_text_rpc(text, source, target)
        except ProviderRateLimitError:
            # Do not route around a Google rate-limit/CAPTCHA. Stop and resume later.
            raise
        except ProviderError as rpc_error:
            try:
                return self._request_text_gtx(text, source, target)
            except ProviderRateLimitError:
                raise
            except ProviderError as gtx_error:
                raise ProviderError(f"Google browser RPC failed ({rpc_error}); GTX fallback failed ({gtx_error})") from None

    def _translate_group(self, items: list[dict[str, Any]], source: str, target: str) -> list[dict[str, str]]:
        if not items:
            return []
        if len(items) == 1:
            item = items[0]
            text = self._request_text(str(item.get("text") or ""), source, target)
            return [{"id": str(item["id"]), "text": text}]

        starts: list[str] = []
        ends: list[str] = []
        chunks: list[str] = []
        for index, item in enumerate(items):
            sid = str(item["id"])
            start = self._marker(index, sid, "A")
            end = self._marker(index, sid, "B")
            raw = str(item.get("text") or "")
            if start in raw or end in raw:
                raise ProviderBatchIntegrityError(f"Batch marker collision in segment {sid}")
            starts.append(start); ends.append(end); chunks.append(start + raw + end)

        translated = self._request_text("\n".join(chunks), source, target)
        out: list[dict[str, str]] = []
        cursor = 0
        try:
            for index, item in enumerate(items):
                start, end = starts[index], ends[index]
                if translated.count(start) != 1 or translated.count(end) != 1:
                    raise ProviderBatchIntegrityError(f"Google changed/dropped batch marker for segment {item['id']}")
                a = translated.index(start, cursor); b = translated.index(end, a + len(start))
                if a < cursor or b < a:
                    raise ProviderBatchIntegrityError("Google batch markers returned out of order")
                value = translated[a + len(start):b].strip("\r\n \t")
                out.append({"id": str(item["id"]), "text": value})
                cursor = b + len(end)
        except (ValueError, ProviderBatchIntegrityError) as e:
            if len(items) <= 1:
                raise ProviderBatchIntegrityError(str(e)) from None
            mid = len(items) // 2
            return self._translate_group(items[:mid], source, target) + self._translate_group(items[mid:], source, target)
        return out

    def translate(self, *, system_prompt: str, user_prompt: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not items:
            return []
        endpoint = self.config.endpoint or "https://translate.google.com/_/TranslateWebserverUi/data/batchexecute"
        if not endpoint.lower().startswith("https://"):
            raise ProviderError("google_free provider.endpoint must be a full https:// URL")
        source = (self.config.source_language_code or "auto").strip() or "auto"
        target = (self.config.target_language_code or "ru").strip()
        if not target:
            raise ProviderError("google_free target_language_code is required")
        return self._translate_group(items, source, target)


class GoogleTranslateProvider(BaseProvider):
    """Official Google Cloud Translation Basic (v2) provider.

    Uses an API key from an environment variable and sends it in X-goog-api-key,
    never in the URL or project database. Input order is preserved by the API and
    mapped back to RPTS segment ids.
    """
    name = "google_translate"

    def translate(self, *, system_prompt: str, user_prompt: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        if not items:
            return []
        if len(items) > 128:
            raise ProviderError("Google Cloud Translation Basic accepts at most 128 strings per request")
        api_key = os.environ.get(self.config.api_key_env, "") if self.config.api_key_env else ""
        if not api_key:
            raise ProviderError(f"Google API key is missing. Set environment variable {self.config.api_key_env!r}")
        url = self.config.endpoint or "https://translation.googleapis.com/language/translate/v2"
        if not url.lower().startswith(("http://", "https://")):
            raise ProviderError("google_translate provider.endpoint must be a full https:// URL when overridden")
        target = (self.config.target_language_code or "ru").strip()
        if not target:
            raise ProviderError("google_translate target_language_code is required")
        body: dict[str, Any] = {
            "q": [str(x.get("text") or "") for x in items],
            "target": target,
            "format": "text",
        }
        source = (self.config.source_language_code or "auto").strip()
        if source and source.lower() not in {"auto", "detect", "none"}:
            body["source"] = source
        model = (self.config.model or "nmt").strip()
        if model and model.lower() not in {"default", "auto"}:
            body["model"] = model
        data = _http_json(url, body, {"X-goog-api-key": api_key}, self.config.timeout_seconds)
        try:
            rows = data["data"]["translations"]
        except Exception:
            raise ProviderError(f"Unexpected Google Translation response shape: {json.dumps(data, ensure_ascii=False)[:1500]}") from None
        if not isinstance(rows, list) or len(rows) != len(items):
            raise ProviderError(f"Google Translation returned {len(rows) if isinstance(rows, list) else 'invalid'} results for {len(items)} inputs")
        out = []
        for item, row in zip(items, rows):
            if not isinstance(row, dict) or "translatedText" not in row:
                raise ProviderError("Google Translation result is missing translatedText")
            # The v2 API can return escaped entities in translatedText. Unescape
            # after the JSON layer, then RPTS restores protected Ren'Py markers.
            out.append({"id": str(item["id"]), "text": html.unescape(str(row["translatedText"]))})
        return out


class OllamaProvider(BaseProvider):
    name = "ollama"

    def translate(self, *, system_prompt: str, user_prompt: str, items: list[dict[str, Any]]) -> list[dict[str, str]]:
        if uses_single_text(self.config):
            if len(items) != 1:
                raise ProviderBatchIntegrityError("Single-text translation requires exactly one target")
            text = self._generate_json(system_prompt=system_prompt, user_prompt=user_prompt,
                schema=None, budget=self.output_budget(items), raw_text=True)
            return [{"id": str(items[0]["id"]), "text": text}]
        payload = self._generate_json(system_prompt=system_prompt, user_prompt=user_prompt,
            schema=_translation_schema(items, compact=self.config.compact_json, protocol=self.config.response_protocol) if self.config.structured_json else None,
            budget=self.output_budget(items))
        return _translations_for_items(payload, items, compact=self.config.compact_json, protocol=self.config.response_protocol)

    def generate_json(self, *, system_prompt: str, user_prompt: str, schema: dict) -> dict:
        return self._generate_json(system_prompt=system_prompt, user_prompt=user_prompt, schema=schema, budget=self.config.max_output_tokens)

    def _generate_json(self, *, system_prompt: str, user_prompt: str, schema: dict | None, budget: int, grammar: str | None = None, raw_text: bool = False) -> dict | str:
        base = (self.config.base_url or "http://127.0.0.1:11434").rstrip("/")
        endpoint = self.config.endpoint or "/api/chat"
        url = base + (endpoint if endpoint.startswith("/") else "/" + endpoint)
        body = {
            "model": self.config.model,
            "stream": False,
            "messages": chat_messages(self.config, system_prompt, user_prompt),
            "options": {"temperature": self.config.temperature, "top_p": self.config.top_p},
        }
        if budget > 0:
            body["options"]["num_predict"] = budget
        for name in ("top_k", "min_p", "repeat_penalty", "presence_penalty"):
            if getattr(self.config, name) is not None:
                body["options"][name] = getattr(self.config, name)
        if self.config.context_limit_tokens > 0:
            body["options"]["num_ctx"] = self.config.context_limit_tokens
        if self.config.enable_thinking is not None and supports_thinking_switch(self.config):
            body["think"] = self.config.enable_thinking
        if schema is not None:
            body["format"] = schema
        data = _http_json(url, body, {}, self.config.timeout_seconds)
        if data.get("done") is False or data.get("done_reason") in {"length", "max_tokens"}:
            raise ProviderOutputTruncatedError("Ollama output is unfinished or truncated")
        try:
            content = data["message"]["content"]
        except Exception:
            raise ProviderError(f"Unexpected Ollama response shape: {json.dumps(data, ensure_ascii=False)[:1500]}") from None
        if not isinstance(content, str):
            raise ProviderResponseFormatError("Ollama message.content must be a string")
        return _single_text_payload(content) if raw_text else _extract_json_payload(content)


def response_key(item: dict[str, Any], index: int) -> str:
    """Checkable row key. It validates structure, not semantic correspondence."""
    source = str(item["text"])
    digest = hashlib.sha256((str(item["id"]) + "\0" + source).encode("utf-8")).hexdigest()[:6]
    # Source excerpts in a key can be mistaken for the text to translate.
    # This digest identifies the slot; it cannot prove that its value translates
    # the right source (a model can still put another row's meaning under it).
    return f"{index}:{digest}"


def _russian_translation_grammar(items):
    """llama.cpp GBNF: valid JSON, all keys/markers, no unprotected Han.

    Only enabled for the local server. Quoted JSON characters use real escape
    productions; raw quotes/control characters cannot corrupt JSON. Unicode
    escapes are deliberately excluded so Han cannot slip through as \\uXXXX.
    This constrains syntax, not meaning or Russian grammar.
    """
    literal = lambda value: json.dumps(value, ensure_ascii=False)
    fields, rules = [], []
    for i, item in enumerate(items):
        markers = re.findall(r"⟪RPTS?\d+(?:_\d+)?⟫", str(item["text"]))
        body = ["char*"]
        for marker in markers:
            body.extend([literal(marker), "char*"])
        rules.append(f"text-{i} ::= " + literal('"') + " " + " ".join(body) + " " + literal('"'))
        fields.append(literal(json.dumps(response_key(item, i)) + ":") + f" text-{i}")
    root = "root ::= " + literal('{"t":{') + " " + (" " + literal(",") + " ").join(fields) + " " + literal("}}")
    # UTF-8 ranges are supported by GBNF, and JSON escapes cannot introduce a
    # marker or Han. Ordinary Latin brands/emails, Cyrillic and emoji remain.
    char_rule = r'char ::= [^"\\\x00-\x1F⟪⟫㐀-鿿豈-﫿𠀀-𯨟] | "\\" ["\\/bfnrt]'
    return "\n".join([root, *rules, char_rule])


def _translation_schema(items: list[dict[str, Any]], *, compact: bool, protocol: str = "source_echo") -> dict:
    if compact and protocol == "anchored_keys":
        slots = {response_key(item, i): {"type": "string", "minLength": 1} for i, item in enumerate(items)}
        return {"type": "object", "properties": {"t": {"type": "object", "properties": slots,
            "required": list(slots), "additionalProperties": False}}, "required": ["t"], "additionalProperties": False}
    if compact:
        slots = {str(i): {"type": "object", "properties": {
            "source": {"type": "string", "const": str(item["text"])},
            "text": {"type": "string", "minLength": 1}},
            "required": ["source", "text"], "additionalProperties": False} for i, item in enumerate(items)}
        return {"type": "object", "properties": {"t": {"type": "object", "properties": slots,
            "required": list(slots), "additionalProperties": False}}, "required": ["t"], "additionalProperties": False}
    return {"type": "object", "properties": {"translations": {"type": "array", "minItems": len(items), "maxItems": len(items),
        "items": {"type": "object", "properties": {"id": {"type": "string", "enum": [str(x["id"]) for x in items]},
            "source": {"type": "string"}, "text": {"type": "string", "minLength": 1}},
            "required": ["id", "source", "text"], "additionalProperties": False}}},
        "required": ["translations"], "additionalProperties": False}


def _translations_for_items(payload: dict, items: list[dict[str, Any]], *, compact: bool, protocol: str = "source_echo") -> list[dict[str, str]]:
    if compact and protocol == "anchored_keys":
        rows = payload.get("t")
        expected = {response_key(item, i): item for i, item in enumerate(items)}
        if not isinstance(rows, dict) or set(rows) != set(expected):
            raise ProviderBatchIntegrityError("Missing or unexpected source-bound response key; positional responses are unsafe")
        out = []
        for key, item in expected.items():
            value = rows[key]
            findings = None
            if item.get("review_stage") and isinstance(value, dict):
                # Legacy unstructured servers may still return diagnostics.
                # These are model opinions, never grounds for rejecting text.
                if not isinstance(value, dict) or set(value) != {"issues", "text"}:
                    raise ProviderResponseFormatError("Review requires issues and final text for each key")
                findings = value["issues"]
                if not isinstance(findings, list) or len(findings) > 4 or any(not isinstance(x, str) or not x.strip() for x in findings):
                    raise ProviderResponseFormatError("Review issues must be an array of up to four brief findings")
                value = value["text"]
            if not isinstance(value, str) or not value.strip():
                raise ProviderResponseFormatError("Each requested key requires a nonempty translation")
            result = {"id": str(item["id"]), "text": value}
            if findings is not None:
                result["_review_issues"] = findings
            out.append(result)
        return out
    if compact:
        rows = payload.get("t")
        expected = {str(i): item for i, item in enumerate(items)}
        if not isinstance(rows, dict) or set(rows) != set(expected):
            raise ProviderBatchIntegrityError("Response must contain exactly the requested numbered keys in 't'; positional arrays are unsafe")
    else:
        raw = payload.get("translations")
        if not isinstance(raw, list) or any(not isinstance(r, dict) or not isinstance(r.get("id"), str) for r in raw):
            raise ProviderResponseFormatError("Response must contain translations with id, source and text")
        rows = {r["id"]: r for r in raw}
        expected = {str(item["id"]): item for item in items}
        if len(rows) != len(raw) or set(rows) != set(expected):
            raise ProviderBatchIntegrityError("Missing, duplicate or unexpected translation id")
    out = []
    for key, item in expected.items():
        row = rows[key]
        if not isinstance(row, dict) or row.get("source") != item["text"]:
            raise ProviderBatchIntegrityError(f"Source echo does not match requested item {key}")
        if not isinstance(row.get("text"), str) or not row["text"].strip():
            raise ProviderResponseFormatError(f"Empty or non-string translation for item {key}")
        out.append({"id": str(item["id"]), "text": row["text"]})
    return out


def _translations_from_payload(payload: dict[str, Any]) -> list[dict[str, str]]:
    rows = payload.get("translations")
    if not isinstance(rows, list):
        raise ProviderResponseFormatError("Provider JSON must contain a 'translations' array")
    out = []
    for row in rows:
        if not isinstance(row, dict) or "id" not in row or "text" not in row:
            raise ProviderResponseFormatError("Each translation must contain string fields 'id' and 'text'")
        if not isinstance(row["id"], str) or not row["id"].strip() or not isinstance(row["text"], str) or not row["text"].strip():
            raise ProviderResponseFormatError("Each translation must contain nonempty string fields 'id' and 'text'")
        out.append({"id": row["id"], "text": row["text"]})
    return out


def create_provider(value: dict[str, Any] | None) -> BaseProvider:
    cfg = ProviderConfig.from_dict(value)
    kind = cfg.type.strip().lower()
    if kind == "mock":
        return MockProvider(cfg)
    if kind in {"openai_compatible", "openai-compatible", "openai"}:
        return OpenAICompatibleProvider(cfg)
    if kind in {"google_free", "google-free", "google_web", "google-web", "google_unofficial"}:
        return GoogleFreeTranslateProvider(cfg)
    if kind in {"google_translate", "google-translate", "google", "gcloud_translate"}:
        return GoogleTranslateProvider(cfg)
    if kind == "ollama":
        return OllamaProvider(cfg)
    raise ProviderError(f"Unknown provider type: {cfg.type}")
