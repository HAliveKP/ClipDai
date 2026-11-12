"""Tiny multi-provider LLM client. Every provider here has a free option."""
from __future__ import annotations

import json
import re
import time

import requests


class LLMError(Exception):
    pass


def _post(url: str, headers: dict | None, body: dict, timeout: int = 180, retries: int = 4) -> dict:
    delay = 6
    for attempt in range(retries):
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        if r.status_code in (429, 500, 502, 503) and attempt < retries - 1:
            time.sleep(delay)  # free tiers rate-limit: back off and retry
            delay *= 2
            continue
        if r.status_code >= 400:
            raise LLMError(f"{r.status_code}: {r.text[:300]}")
        return r.json()
    raise LLMError("retries exhausted")


def extract_json(text: str) -> dict:
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        a, b = text.find("{"), text.rfind("}")
        if a >= 0 and b > a:
            return json.loads(text[a:b + 1])
        raise LLMError("model did not return JSON")


def ask_json(s, system: str, user: str) -> dict:
    p = s.llm_provider
    if p == "gemini":
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{s.gemini_model}:generateContent"
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.4},
        }
        data = _post(url, {"x-goog-api-key": s.gemini_key}, body)
        try:
            return extract_json(data["candidates"][0]["content"]["parts"][0]["text"])
        except (KeyError, IndexError) as e:
            raise LLMError(f"unexpected Gemini response: {str(data)[:200]}") from e
    if p in ("groq", "openai"):
        if p == "groq":
            url, key, model = "https://api.groq.com/openai/v1/chat/completions", s.groq_key, s.groq_model
        else:
            url, key, model = s.openai_base.rstrip("/") + "/chat/completions", s.openai_key, s.openai_model
        body = {"model": model, "temperature": 0.4,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
                "response_format": {"type": "json_object"}}
        data = _post(url, {"Authorization": f"Bearer {key}"}, body)
        return extract_json(data["choices"][0]["message"]["content"])
    if p == "ollama":
        body = {"model": s.ollama_model, "stream": False, "format": "json",
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        data = _post(s.ollama_url.rstrip("/") + "/api/chat", None, body, timeout=600)
        return extract_json(data["message"]["content"])
    raise LLMError("no LLM provider configured")


def available(s) -> bool:
    return s.llm_provider != "none"
