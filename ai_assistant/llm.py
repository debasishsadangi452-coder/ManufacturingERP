"""LLM client factory.

Gemini (via Google's OpenAI-compatible endpoint) is preferred when a Gemini
key is configured; otherwise we fall back to Groq. The Gemini client mimics the
small slice of the Groq/OpenAI SDK the app uses —
``client.chat.completions.create(...)`` returning ``choices[0].message`` with
``.content`` / ``.tool_calls`` — so the chat loop is provider-agnostic.
Stdlib only: no extra dependency.
"""

import json
import time
import urllib.error
import urllib.request
from types import SimpleNamespace

GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_MODEL = "gemini-flash-latest"


class LLMError(Exception):
    """Raised when the Gemini API returns an error."""


def _ns(value):
    """Recursively turn JSON dicts into attribute-access namespaces."""
    if isinstance(value, dict):
        return SimpleNamespace(**{k: _ns(v) for k, v in value.items()})
    if isinstance(value, list):
        return [_ns(v) for v in value]
    return value


def _to_plain(value):
    """Inverse of ``_ns``: namespaces back to JSON-serialisable dicts."""
    if isinstance(value, SimpleNamespace):
        return {k: _to_plain(v) for k, v in vars(value).items()}
    if isinstance(value, list):
        return [_to_plain(v) for v in value]
    return value


def _normalise(response):
    """Give every message the attributes the chat loop reads, as the SDKs do."""
    for choice in getattr(response, "choices", None) or []:
        message = choice.message
        for name in ("content", "tool_calls"):
            if not hasattr(message, name):
                setattr(message, name, None)
    return response


def _message_to_dict(message):
    """Serialise a message (dict, or a response message object) for the API."""
    if isinstance(message, dict):
        return message
    out = {"role": getattr(message, "role", "assistant"), "content": getattr(message, "content", None)}
    tool_calls = getattr(message, "tool_calls", None)
    if tool_calls:
        out["tool_calls"] = []
        for tc in tool_calls:
            call = {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.function.name, "arguments": tc.function.arguments or "{}"},
            }
            # Gemini 3 requires its thought_signature echoed back on each tool call.
            extra = getattr(tc, "extra_content", None)
            if extra is not None:
                call["extra_content"] = _to_plain(extra)
            out["tool_calls"].append(call)
    return out


class _Completions:
    def __init__(self, api_key):
        self._api_key = api_key

    def create(self, model, messages, **kwargs):
        payload = {"model": model, "messages": [_message_to_dict(m) for m in messages]}
        for key in ("tools", "tool_choice", "max_tokens", "temperature", "response_format"):
            if kwargs.get(key) is not None:
                payload[key] = kwargs[key]
        # Gemini rejects tool_choice without tools.
        if not payload.get("tools"):
            payload.pop("tool_choice", None)
        # Gemini 2.5 spends max_tokens on hidden "thinking"; give it headroom.
        if payload.get("max_tokens"):
            payload["max_tokens"] = max(payload["max_tokens"], 2048)

        request = urllib.request.Request(
            f"{GEMINI_BASE_URL}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self._api_key}",
            },
            method="POST",
        )
        for attempt in range(3):
            try:
                with urllib.request.urlopen(request, timeout=60) as resp:
                    return _normalise(_ns(json.loads(resp.read().decode("utf-8"))))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:500]
                if exc.code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                raise LLMError(f"Gemini API error {exc.code}: {detail}") from exc
            except (urllib.error.URLError, ConnectionError, TimeoutError) as exc:
                # Transient network drops (TLS resets) are common; retry before failing.
                if attempt < 2:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                reason = getattr(exc, "reason", exc)
                raise LLMError(f"Could not reach Gemini API: {reason}") from exc


class GeminiClient:
    def __init__(self, api_key):
        self.chat = SimpleNamespace(completions=_Completions(api_key))


def gemini_api_key(config):
    return config.get("GEMINI_API_KEY") or None


def get_llm(config):
    """Return ``(client, model, vision_model)`` or ``None`` if no key is set."""
    gemini_key = gemini_api_key(config)
    if gemini_key:
        model = config.get("GEMINI_MODEL") or DEFAULT_GEMINI_MODEL
        return GeminiClient(gemini_key), model, model

    groq_key = config.get("GROQ_API_KEY")
    if groq_key:
        from groq import Groq

        return (
            Groq(api_key=groq_key),
            config.get("MODEL", "llama-3.1-8b-instant"),
            config.get("VISION_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
        )
    return None
