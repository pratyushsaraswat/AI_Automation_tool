"""Resolve a form question: profile.yaml -> saved cache -> LLM -> (reviewer asks user)."""
import difflib
import json
import re

import yaml

from src.config import DATA_DIR
from src.core.llm import LLM
from src.core.models import Answer, Field, JobDetail

SYSTEM = """You fill job application form questions on behalf of a candidate.
Rules:
- Answer ONLY from the candidate profile and resume provided. Never invent skills, employers or years.
- If options are given, "value" must be exactly one of the options (for checkbox kind: comma-separated options).
- Numeric questions (years, CTC, notice days): return just the number.
- Keep text answers short (one line) unless the question asks for a description.
- If the information is not in the profile/resume, give the most conservative honest answer and confidence below 0.5.
Return JSON: {"value": "<answer>", "confidence": <0..1>}"""


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _words(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _closest_option(value: str, options: list[str]) -> str | None:
    if not options:
        return value
    for opt in options:
        if opt.strip().lower() == value.strip().lower():
            return opt
    match = difflib.get_close_matches(value, options, n=1, cutoff=0.6)
    return match[0] if match else None


class AnswerEngine:
    def __init__(self, profile: dict, resume_text: str, llm: LLM | None):
        self.profile = profile
        self.resume_text = resume_text[:12000]
        self.llm = llm
        self.cache_path = DATA_DIR / "answers_cache.json"
        self.cache: dict[str, str] = (
            json.loads(self.cache_path.read_text()) if self.cache_path.exists() else {}
        )

    def resolve(self, f: Field, detail: JobDetail) -> Answer | None:
        return self._from_profile(f) or self._from_cache(f) or self._from_llm(f, detail)

    def remember(self, f: Field, value: str):
        self.cache[_norm(f.label)] = value
        self.cache_path.write_text(json.dumps(self.cache, indent=2, ensure_ascii=False))

    def _from_profile(self, f: Field) -> Answer | None:
        label_words = _words(f.label)
        best = None
        for key, value in (self.profile.get("answers") or {}).items():
            if value in ("", None):
                continue
            if _words(key) <= label_words and (best is None or len(key) > len(best[0])):
                best = (key, str(value))
        if not best:
            return None
        value = _closest_option(best[1], f.options)
        return Answer(value, 0.95, "profile") if value else None

    def _from_cache(self, f: Field) -> Answer | None:
        key = _norm(f.label)
        match = difflib.get_close_matches(key, list(self.cache), n=1, cutoff=0.92)
        if not match:
            return None
        value = _closest_option(self.cache[match[0]], f.options)
        return Answer(value, 0.9, "cache") if value else None

    def _from_llm(self, f: Field, detail: JobDetail) -> Answer | None:
        if not self.llm:
            return None
        profile = {k: v for k, v in self.profile.items() if k != "answers"}
        user = (
            f"CANDIDATE PROFILE:\n{yaml.safe_dump(profile, sort_keys=False)}\n"
            f"RESUME:\n{self.resume_text}\n\n"
            f"JOB: {detail.job.title} at {detail.job.company}\n\n"
            f"QUESTION: {f.label}\nKIND: {f.kind}\nOPTIONS: {f.options or 'free text'}"
        )
        try:
            data = self.llm.json(SYSTEM, user)
        except Exception as e:
            print(f"  [llm error] {e}")
            return None
        value = str(data.get("value", "")).strip()
        confidence = float(data.get("confidence", 0) or 0)
        if f.kind != "checkbox":
            mapped = _closest_option(value, f.options)
            if mapped is None:
                return Answer(value, 0.0, "llm")
            value = mapped
        return Answer(value, confidence, "llm") if value else None
