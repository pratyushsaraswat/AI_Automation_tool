"""Greenhouse: boards.greenhouse.io / job-boards.greenhouse.io, or embedded on a company site
via <iframe id="grnhse_iframe">. Single-page form: name, email, phone, resume, custom questions."""
import re

from playwright.sync_api import TimeoutError as PWTimeout

from src.ats.base import AtsHandler

IFRAME = "iframe#grnhse_iframe, iframe[src*='greenhouse.io']"
FORM = "form#application-form, form#application_form, form:has(input[name*='first_name']), form:has(#first_name)"
SUCCESS_RE = re.compile(r"thank you for applying|application (has been )?(received|submitted)|we.ve received", re.I)


class GreenhouseHandler(AtsHandler):
    name = "greenhouse"

    @classmethod
    def matches(cls, page) -> bool:
        return ("greenhouse.io" in page.url or "gh_jid=" in page.url
                or page.locator(IFRAME).count() > 0)

    def locate_form(self, page):
        scope = page.frame_locator(IFRAME).first if page.locator(IFRAME).count() else page
        form = scope.locator(FORM).first
        try:
            form.wait_for(state="attached", timeout=5000)
        except PWTimeout:
            # newer boards hide the form behind an "Apply" button
            btn = scope.get_by_role("button", name=re.compile(r"^apply", re.I)).first
            if btn.count():
                btn.click()
            try:
                form.wait_for(state="attached", timeout=10000)
            except PWTimeout:
                return None
        form.scroll_into_view_if_needed()
        return scope, form

    def success(self, page, scope) -> bool:
        return ("confirmation" in page.url or page.get_by_text(SUCCESS_RE).count() > 0
                or scope.get_by_text(SUCCESS_RE).count() > 0)
