from pathlib import Path

from src.core.answer_engine import AnswerEngine
from src.core.human import Human
from src.core.models import Field, JobDetail
from src.core.review import Reviewer


class ApplySession:
    """What a site adapter / ATS handler gets while applying to one job."""

    def __init__(self, detail: JobDetail, engine: AnswerEngine, reviewer: Reviewer, human: Human,
                 resume_path: Path):
        self.detail = detail
        self.engine = engine
        self.reviewer = reviewer
        self.human = human
        self.resume_path = resume_path
        self.last_source = ""      # where the last answer came from (shown in summaries)

    def answer(self, f: Field, optional: bool = False, force_ask: bool = False) -> str | None:
        """optional=True: return a known (profile/cache) answer or None — never asks the LLM or you.
        force_ask=True: always show the answer to you (used when the site rejected it)."""
        ans = self.engine.resolve(f, self.detail, use_llm=not optional)
        if optional:
            if ans is None:
                return None
            value = self.reviewer.confirm_answer(f, ans)
            self.last_source = ans.source if value == ans.value else "user"
            return value
        value = self.reviewer.confirm_answer(f, ans, force=force_ask)
        self.last_source = ans.source if ans and value == ans.value else "user"
        if ans is None or value != ans.value or ans.source != "profile":
            self.engine.remember(f, value, "llm-confirmed" if ans and ans.source == "llm" and value == ans.value
                                 else "user")
        return value
