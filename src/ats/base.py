from abc import ABC, abstractmethod

from src.core.browser import dump_debug
from src.core.forms import FormFiller
from src.core.session import ApplySession


class AtsHandler(ABC):
    name: str = "ats"
    submit_selector = "button[type='submit'], input[type='submit']"

    @classmethod
    @abstractmethod
    def matches(cls, page) -> bool:
        ...

    @abstractmethod
    def locate_form(self, page):
        """Navigate to / reveal the application form. Return (scope, form_locator) or None."""

    @abstractmethod
    def success(self, page, scope) -> bool:
        ...

    def apply(self, page, session: ApplySession) -> str:
        found = self.locate_form(page)
        if not found:
            dump_debug(page, f"{self.name}-noform")
            print(f"  {self.name}: application form not found")
            return "failed"
        scope, form = found
        filler = FormFiller(session, upload="always")
        controls = filler.fill(form)
        job = session.detail.job
        return filler.review_and_submit(
            form, controls, form.locator(self.submit_selector).last,
            success=lambda: self.success(page, scope),
            title=f"{self.name}: {job.title} — {job.company}",
        )
