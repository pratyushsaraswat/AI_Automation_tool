"""Any other company site (SuccessFactors, iCIMS, Oracle, custom careers pages...).

Every site differs (logins, multi-page wizards, captchas), so we never auto-submit here:
  1. job-description page? click its "Apply" / "Apply now" button and follow to the form;
  2. fill every visible field we already know (profile + answer memory);
  3. YOU finish and submit — meanwhile the tool records what you type;
  4. on "d" the answers you typed are saved to memory (reused on every future site) and the
     site's notes (needs login, apply button, applications) go to data/site_knowledge.json.
"""
import re

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.ats.learn import SiteKnowledge, SiteRecorder
from src.core.forms import FormFiller
from src.core.review import console
from src.core.session import ApplySession

FORM = "form:has(input[type='email']), form:has(input[name*='name' i]), form:has(input[type='file'])"
NEEDS_ACCOUNT = ("successfactors", "taleo.net", "icims.com", "oraclecloud.com", "brassring", "smartrecruiters")
APPLY_RE = re.compile(r"^\s*(apply( now| for this (job|position|role)| online| here)?|i'?m interested|easy apply)\s*$", re.I)
FIELDS = "input:not([type=hidden]):not([type=submit]):not([type=button]):not([type=search]), select, textarea"


class GenericHandler:
    name = "generic"

    def apply(self, page, session: ApplySession) -> str:
        job = session.detail.job
        kb = SiteKnowledge()
        domain = kb.domain(page.url)
        hint = kb.hint(page.url)
        if hint:
            console.print(f"  [cyan]site memory ({domain}):[/] {hint}")
        needs_account = any(d in page.url for d in NEEDS_ACCOUNT) or kb.get(page.url).get("needs_account")
        if needs_account:
            console.print("[yellow]This site needs an account/login. Log in in the browser; "
                          "the tool fills what it can on each page.[/]")
        kb.update(page.url, visits=1)

        recorder = SiteRecorder(page)
        apply_button, new = self._open_form(page, kb.get(page.url).get("apply_button"))
        if apply_button:
            kb.update(page.url, apply_button=apply_button)
        if new:
            page, recorder = new, self._follow(new, recorder)

        filled: dict[str, str] = {}      # label -> what the tool filled (your corrections get learned)
        while True:
            root = self._form_root(page)
            if root is not None:
                filler = FormFiller(session, upload="always", known_only=True)
                controls = filler.fill(root)
                filled.update({c.field.label: c.value for c in controls if c.source not in ("", "skipped")})
                filler.summary(controls, f"generic: {job.title} — {job.company}")
            else:
                console.print("[dim]No form fields on this page yet.[/]")
            choice = session.reviewer.choose(
                "[magenta]Finish in the browser and submit yourself (I'm recording your answers).[/]\n"
                "[cyan]f[/] fill again (new page / after login)  [cyan]a[/] click Apply for me  "
                "[cyan]d[/] I submitted  [cyan]s[/] skip  [cyan]q[/] quit", "fads")
            if choice == "a":
                b, new = self._open_form(page, None, force=True)
                if b:
                    kb.update(page.url, apply_button=b)
                if new:
                    page, recorder = new, self._follow(new, recorder)
                continue
            if choice == "f":
                if page.locator("input[type='password']:visible").count():
                    kb.update(page.url, needs_account=True)
                continue
            saved = recorder.save_to_memory(session.engine, domain, filled)
            if saved:
                console.print(f"  [green]learned {saved} answers[/] — they'll fill automatically next time")
            if choice == "d":
                kb.update(page.url, applied=1)
                return "applied_manual"
            return "skipped"

    def _form_root(self, page):
        """A <form> with application fields, else the main area when it holds 2+ visible fields
        (many career sites are single-page apps without a <form> element)."""
        form = page.locator(FORM).filter(visible=True)
        if form.count():
            return form.first
        for sel in ("main", "[role='main']", "body"):
            root = page.locator(sel).first
            if root.count() and root.locator(FIELDS).filter(visible=True).count() >= 2:
                return root
        return None

    @staticmethod
    def _follow(new_page, old: SiteRecorder) -> SiteRecorder:
        """Form opened in a new tab: record there too, keep what was already learned."""
        rec = SiteRecorder(new_page)
        rec.answers.update(old.answers)
        return rec

    def _open_form(self, page, remembered: str | None, force: bool = False):
        """On a job-description page, click its Apply button (the one that worked last time first).
        Returns (button text clicked or '', new tab or None)."""
        if not force and self._form_root(page) is not None:
            return "", None
        cands = page.locator("a:visible, button:visible, [role='button']:visible")
        order = []
        if remembered:
            order.append(cands.filter(has_text=re.compile(rf"^\s*{re.escape(remembered)}\s*$", re.I)))
        order.append(cands.filter(has_text=APPLY_RE))
        for loc in order:
            if not loc.count():
                continue
            btn = loc.first
            text = (btn.inner_text() or "").strip()[:40]
            console.print(f"  clicking [bold]{text}[/] on the job page")
            new = None
            try:
                with page.context.expect_page(timeout=4000) as info:
                    btn.click()
                new = info.value
                new.wait_for_load_state("domcontentloaded")
                new.bring_to_front()
                console.print(f"  form opened in a new tab: {new.url[:80]}")
            except PWTimeout:
                page.wait_for_timeout(2500)      # same tab navigation / modal
            except PlaywrightError:
                continue
            return text, new
        return "", None
