"""Lever: jobs.lever.co/<company>/<id>  (form lives at .../<id>/apply). Often has hCaptcha on submit."""
import re
from urllib.parse import urlsplit, urlunsplit

from playwright.sync_api import TimeoutError as PWTimeout

from src.ats.base import AtsHandler

FORM = "form#application-form, form.application-form, form:has(input[name='email'])"
SUCCESS_RE = re.compile(r"application submitted|thanks for applying|thank you for applying", re.I)


class LeverHandler(AtsHandler):
    name = "lever"
    submit_selector = "button#btn-submit, button[type='submit']"

    @classmethod
    def matches(cls, page) -> bool:
        return "lever.co" in page.url

    def locate_form(self, page):
        parts = urlsplit(page.url)
        if not parts.path.rstrip("/").endswith("/apply"):
            page.goto(urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/apply")),
                      wait_until="domcontentloaded")
        form = page.locator(FORM).first
        try:
            form.wait_for(state="attached", timeout=12000)
        except PWTimeout:
            return None
        return page, form

    def success(self, page, scope) -> bool:
        return "/thanks" in page.url or page.get_by_text(SUCCESS_RE).count() > 0
