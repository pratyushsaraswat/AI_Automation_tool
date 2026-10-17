import yaml

from src.core.llm import LLM
from src.core.models import JobDetail

SYSTEM = """You judge how well a candidate fits a job. Be strict and realistic.
Consider: required skills, years of experience, role level, domain.
Return JSON: {"score": <0..100>, "reason": "<one short sentence>"}"""


class Matcher:
    def __init__(self, profile: dict, resume_text: str, llm: LLM | None):
        self.profile = profile
        self.resume_text = resume_text[:8000]
        self.llm = llm
        self.skills = [s.lower() for s in profile.get("skills") or []]

    def score(self, detail: JobDetail) -> tuple[int, str]:
        if self.llm:
            try:
                return self._llm_score(detail)
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
        if not self.skills:
            return 100, "no skills in profile — not filtering"
        text = f"{detail.job.title} {detail.description}".lower()
        hits = [s for s in self.skills if s in text]
        score = min(100, int(len(hits) / min(len(self.skills), 6) * 100))
        return score, f"matched skills: {', '.join(hits) or 'none'}"
