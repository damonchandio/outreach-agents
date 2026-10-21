"""The language model: any OpenAI-compatible endpoint (Groq, OpenRouter, LM Studio, vLLM...) if LLM_BASE_URL is
set, otherwise Ollama on this machine. Prospects' replies are personal data: pick a provider you trust with them."""
from __future__ import annotations

import json
import re

import requests

from . import config


def chat(system: str, prompt: str, json_mode: bool = False, max_tokens: int = 500, temperature: float = 0.4) -> str:
    s = config.settings
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if s.llm_base_url:
        body = {"model": s.llm_model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = requests.post(f"{s.llm_base_url}/chat/completions", json=body, timeout=120,
                          headers={"Authorization": f"Bearer {s.llm_api_key}"} if s.llm_api_key else {})
        r.raise_for_status()
        text = (r.json()["choices"][0]["message"].get("content") or "")
    else:
        body = {"model": s.ollama_model, "stream": False, "messages": messages,
                "options": {"temperature": temperature, "num_predict": max_tokens}}
        if json_mode:
            body["format"] = "json"
        r = requests.post(f"{s.ollama_url}/api/chat", json=body, timeout=180)
        r.raise_for_status()
        text = (r.json().get("message") or {}).get("content", "")
    return re.sub(r"<think>.*?</think>", "", text, flags=re.S).strip()


def chat_json(system: str, prompt: str, **kw) -> dict:
    raw = chat(system, prompt, json_mode=True, **kw)
    m = re.search(r"\{.*\}", raw, re.S)
    try:
        return json.loads(m.group(0) if m else raw)
    except ValueError:
        return {}
