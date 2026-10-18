"""Workday (*.myworkdayjobs.com / *.wd1.myworkdaysite.com ...).

Flow:
  job page -> "Apply" -> "Autofill with Resume" (Workday parses your PDF into the form)
  -> Sign in / Create account  (YOU do this in the browser — the tool never stores passwords)
  -> wizard pages: My Information, My Experience, Application Questions, Voluntary Disclosures,
     Self Identify ... each page: fill -> show table -> you edit / confirm -> "Save and Continue"
  -> Review page: the tool STOPS. You check everything and click Submit yourself.

Workday tags almost everything with data-automation-id, which is stable across companies.
"""
import re

from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.forms import FormFiller
from src.core.models import SkipJob
from src.core.review import console
from src.core.session import ApplySession

AID = "[data-automation-id='{}']"
SEL = {
    "apply": "a[data-automation-id='adventureButton'], button[data-automation-id='adventureButton']",
    "autofill": AID.format("autofillWithResume"),
    "manual": AID.format("applyManually"),
    "last_app": AID.format("useMyLastApplication"),
    "password": "input[type='password']:visible",
    "email": "input[data-automation-id='email']:visible, input[type='email']:visible",
    "root": "[data-automation-id='applyFlowPage'], form, main",
    "next": "button[data-automation-id='pageFooterNextButton']:visible, "
            "button[data-automation-id='bottom-navigation-next-button']:visible",
    "step": "[data-automation-id='progressBarActiveStep'], [data-automation-id='pageHeader'] h2, main h2",
    "error": "[data-automation-id='errorMessage']:visible, [data-automation-id='inlineErrorMessage']:visible, "
             "[role='alert']:visible",
    "file": "input[type='file'][data-automation-id='file-upload-input-ref'], input[type='file']",
    "uploaded": "[data-automation-id='file-upload-successful'], [data-automation-id='delete-file']",
    "submitted": "text=/application (has been )?submitted|thank you for applying/i",
}
NEXT_RE = re.compile(r"^(save and continue|continue|next)$", re.I)
SUBMIT_RE = re.compile(r"^submit$", re.I)


class WorkdayHandler:
    name = "workday"

    @classmethod
    def matches(cls, page) -> bool:
        return bool(re.search(r"myworkday(jobs|site)\.com|\.workday\.com", page.url))

    def apply(self, page, session: ApplySession) -> str:
        self._start(page)
        self._wait_signed_in(page, session)
        job = session.detail.job
        seen_steps: list[str] = []

        for _ in range(15):
            page.wait_for_timeout(2500)
            step = self._step_name(page)
            if self._is_review(page, step):
                return self._handover(page, session)
            root = self._root(page)
            console.rule(f"[bold]Workday · {step or 'page'}[/] — {job.title} @ {job.company}")
            self._upload_resume(page, session)       # first: Workday parses it into the other fields
            filler = FormFiller(session, upload="on_error")
            controls = filler.fill(root)

            choice = filler.review_edits(root, controls, f"Workday · {step}", "SAVE & CONTINUE",
                                         extra="f refill (after you change the page)  m manual-done  ")
            if choice == "f":
                continue
            if choice == "m":
                return "applied_manual"

            nxt = self._next_button(page)
            if not nxt:
                dump_debug(page, "workday-nonext")
                return self._handover(page, session)
            nxt.click()
            page.wait_for_timeout(3000)
            if page.locator(SEL["error"]).count():
                console.print("[yellow]Workday shows errors on this page — re-asking those fields.[/]")
                filler.fill(root, force_invalid=True)
                if page.locator(SEL["error"]).count():
                    session.reviewer.choose("[yellow]Fix the red fields in the browser[/], then "
                                            "[cyan]c[/] continue", "c")
                nxt = self._next_button(page)
                if nxt and self._step_name(page) == step:
                    nxt.click()
                    page.wait_for_timeout(3000)
            seen_steps.append(step)
            if seen_steps[-3:].count(step) == 3:
                dump_debug(page, "workday-stuck")
                return self._handover(page, session)
        return self._handover(page, session)

    # --- steps ---------------------------------------------------------------

    def _start(self, page):
        if page.locator(SEL["apply"]).count():
            page.locator(SEL["apply"]).first.click()
            page.wait_for_timeout(2500)
        for key in ("autofill", "last_app", "manual"):
            btn = page.locator(SEL[key]).filter(visible=True)
            if btn.count():
                console.print(f"  Workday: choosing [bold]{btn.first.inner_text().strip()}[/]")
                btn.first.click()
                page.wait_for_timeout(2500)
                return

    def _wait_signed_in(self, page, session: ApplySession):
        """Workday needs an account per company. User signs in / creates it; we only prefill email."""
        while page.locator(SEL["password"]).count():
            email = page.locator(SEL["email"]).first
            if email.count() and not email.input_value():
                email.fill(session.engine.profile.get("personal", {}).get("email", ""))
            choice = session.reviewer.choose(
                "[magenta]Workday sign-in / create account:[/] finish it in the browser "
                "(email is prefilled; password is yours), then [cyan]c[/] continue  [cyan]s[/] skip", "cs")
            if choice == "s":
                raise SkipJob()
            page.wait_for_timeout(2000)

    def _upload_resume(self, page, session: ApplySession):
        inp = page.locator(SEL["file"])
        if inp.count() and not page.locator(SEL["uploaded"]).count():
            console.print(f"  uploading {session.resume_path.name} — Workday will parse it...")
            inp.first.set_input_files(str(session.resume_path))
            try:
                page.locator(SEL["uploaded"]).first.wait_for(timeout=20000)
            except PWTimeout:
                pass
            page.wait_for_timeout(4000)

    def _root(self, page):
        for sel in SEL["root"].split(", "):
            loc = page.locator(sel).filter(visible=True)
            if loc.count():
                return loc.first
        return page.locator("body")

    def _step_name(self, page) -> str:
        for sel in SEL["step"].split(", "):
            loc = page.locator(sel).filter(visible=True)
            if loc.count():
                return loc.first.inner_text().strip().split("\n")[0][:60]
        return ""

    def _is_review(self, page, step: str) -> bool:
        if "review" in step.lower():
            return True
        return page.get_by_role("button", name=SUBMIT_RE).filter(visible=True).count() > 0

    def _next_button(self, page):
        btn = page.locator(SEL["next"])
        if btn.count():
            return btn.first
        btn = page.get_by_role("button", name=NEXT_RE).filter(visible=True)
        return btn.first if btn.count() else None

    def _handover(self, page, session: ApplySession) -> str:
        console.print("[bold magenta]Review page reached.[/] Check every section in the browser and "
                      "click [bold]Submit[/] yourself.")
        choice = session.reviewer.choose("[cyan]d[/] I submitted it  [cyan]s[/] I did not submit", "ds")
        if choice == "d":
            try:
                page.locator(SEL["submitted"]).first.wait_for(timeout=3000)
            except PWTimeout:
                pass
            return "applied_manual"
        return "skipped"
