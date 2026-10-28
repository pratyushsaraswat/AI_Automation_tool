import re

import yaml

from src.core.llm import LLM, LLMUnavailable
from src.core.models import JobDetail
from src.outreach.skills import _COMPILED, find_skills

SYSTEM = """You judge how well a candidate fits a job. Be strict and realistic.
Consider: required skills, years of experience, role level, domain.
Return JSON: {"score": <0..100>, "reason": "<one short sentence>"}"""


class Matcher:
    def __init__(self, profile: dict, resume_text: str, llm: LLM | None):
        self.profile = profile
        self.resume_text = resume_text[:8000]
        self.resume_text_full = resume_text
        self.llm = llm
        self.skills = [s.lower() for s in profile.get("skills") or []]
        self._patterns = {s: _skill_patterns(s) for s in self.skills}

    def score(self, detail: JobDetail) -> tuple[int, str]:
        if self.llm:
            try:
                return self._llm_score(detail)
            except LLMUnavailable:
                pass
            except Exception as e:
                print(f"  [llm error] {e} — using keyword match")
        return self._keyword_score(detail)

    def _llm_score(self, detail: JobDetail) -> tuple[int, str]:
        profile = {k: v for k, v in self.profile.items() if k != "answers"}
        user = (
            f"CANDIDATE:\n{yaml.safe_dump(profile, sort_keys=False)}\nRESUME:\n{self.resume_text}\n\n"
            f"JOB: {detail.job.title} at {detail.job.company} ({detail.job.experience}, {detail.job.location})\n"
            f"DESCRIPTION:\n{detail.description[:6000]}"
        )
        data = self.llm.json(SYSTEM, user)
        return int(data.get("score", 0)), str(data.get("reason", ""))

    def _keyword_score(self, detail: JobDetail) -> tuple[int, str]:
        """No LLM: how much of what the JOB asks for you have (resume + profile skills).
        A JD asking Java, Spring Boot, Microservices, AWS -> you have all 4 -> 100."""
        job_text = f"{detail.job.title}\n{' '.join(detail.job.tags)}\n{detail.description}"
        wanted = find_skills(job_text)
        if not wanted:
            return 50, "no recognisable tech skills in the JD"
        mine = set(find_skills(self.resume_text_full)) | {
            n for n in find_skills(" , ".join(self.profile.get("skills") or []))}
        have = [w for w in wanted if w in mine]
        missing = [w for w in wanted if w not in mine]
        score = min(100, round(len(have) / min(len(wanted), 8) * 100))
        if len(wanted) < 3:              # 1 matching skill out of 1 isn't evidence of fit ("Api Tester": REST)
            score = min(score, 55)
        reason = f"you have {len(have)}/{len(wanted)}: {', '.join(have[:6]) or 'none'}"
        if missing:
            reason += f" · missing: {', '.join(missing[:4])}"
        return score, reason

def _skill_patterns(skill: str) -> list[re.Pattern]:
    """Whole-word patterns for a profile skill: 'go' must not hit 'good', 'rest' not 'interest',
    'java' not 'javascript'. Reuses the strict vocabulary in outreach/skills.py when the skill is there."""
    for name, pats in _COMPILED.items():
        if name.lower() == skill or name.lower().split()[0] == skill:
            return pats
    return [re.compile(rf"(?<![a-z0-9]){re.escape(skill)}(?![a-z0-9#+])", re.I)]
