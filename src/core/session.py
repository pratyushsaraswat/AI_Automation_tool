from src.core.answer_engine import AnswerEngine
from src.core.human import Human
from src.core.models import Field, JobDetail
from src.core.review import Reviewer


class ApplySession:
    """What a site adapter gets while applying to one job."""

    def __init__(self, detail: JobDetail, engine: AnswerEngine, reviewer: Reviewer, human: Human):
        self.detail = detail
        self.engine = engine
        self.reviewer = reviewer
        self.human = human

    def answer(self, f: Field) -> str:
        ans = self.engine.resolve(f, self.detail)
        value = self.reviewer.confirm_answer(f, ans)
        if ans is None or ans.source == "llm" or value != ans.value:
            self.engine.remember(f, value)
        return value
