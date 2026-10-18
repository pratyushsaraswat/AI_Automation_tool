"""Provider-agnostic JSON completion: Gemini or OpenAI over plain HTTP."""
import json
import os
import re
import time

import httpx


class LLMUnavailable(Exception):
    """Bad key / no credits: stop calling the API for the rest of the run."""


class LLM:
    def __init__(self, provider: str, api_key: str, model: str):
        self.provider = provider
        self.api_key = api_key
        self.model = model
        self.http = httpx.Client(timeout=60)
        self.disabled_reason = ""

    def json(self, system: str, user: str) -> dict:
        if self.disabled_reason:
            raise LLMUnavailable(self.disabled_reason)
        for attempt in range(3):
            try:
                text = self._gemini(system, user) if self.provider == "gemini" else self._openai(system, user)
                return _parse_json(text)
            except httpx.HTTPStatusError as e:
                body = e.response.text
                if e.response.status_code in (401, 403) or "quota" in body or "credit" in body:
                    self.disabled_reason = f"{self.provider} unavailable ({e.response.status_code}): {body[:200]}"
                    print(f"  [llm] {self.disabled_reason}\n  [llm] continuing without AI for this run.")
                    raise LLMUnavailable(self.disabled_reason) from e
                if e.response.status_code in (429, 500, 502, 503) and attempt < 2:
                    time.sleep(3 * (attempt + 1))
                    continue
                raise
        return {}

    def _gemini(self, system: str, user: str) -> str:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model}:generateContent"
        body = {
            "system_instruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"responseMimeType": "application/json", "temperature": 0.2},
        }
        r = self.http.post(url, json=body, headers={"x-goog-api-key": self.api_key})
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]

    def _openai(self, system: str, user: str) -> str:
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "response_format": {"type": "json_object"},
        }
        r = self.http.post(
            "https://api.openai.com/v1/chat/completions",
            json=body,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]


def _parse_json(text: str) -> dict:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", text, re.S)
        return json.loads(m.group(0)) if m else {}


def from_env(provider: str) -> LLM | None:
    if provider == "gemini" and os.getenv("GEMINI_API_KEY"):
        return LLM("gemini", os.environ["GEMINI_API_KEY"], os.getenv("GEMINI_MODEL", "gemini-2.5-flash"))
    if provider == "openai" and os.getenv("OPENAI_API_KEY"):
        return LLM("openai", os.environ["OPENAI_API_KEY"], os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    return None
