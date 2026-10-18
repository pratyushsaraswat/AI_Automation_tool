"""Any other company site (Workday, SuccessFactors, custom careers pages...).

Every site differs (logins, multi-page wizards, captchas), so we never auto-submit here:
fill whatever form is visible with known answers, then YOU finish and submit in the browser."""
from src.core.forms import FormFiller
from src.core.review import console
from src.core.session import ApplySession

FORM = "form:has(input[type='email']), form:has(input[name*='name' i]), form:has(input[type='file'])"
NEEDS_ACCOUNT = ("successfactors", "taleo.net", "icims.com", "oraclecloud.com")


class GenericHandler:
    name = "generic"

    def apply(self, page, session: ApplySession) -> str:
        job = session.detail.job
        if any(d in page.url for d in NEEDS_ACCOUNT):
            console.print("[yellow]This ATS needs an account/login + multi-page wizard. "
                          "Log in in the browser; the tool will fill what it can on each page.[/]")
        while True:
            form = page.locator(FORM).first
            if form.count():
                filler = FormFiller(session, upload="always")
                controls = filler.fill(form)
                filler.summary(controls, f"generic: {job.title} — {job.company}")
            else:
                console.print("[dim]No recognisable form on this page.[/]")
            choice = session.reviewer.choose(
                "[magenta]Finish in the browser and submit yourself.[/] "
                "[cyan]f[/] fill this page again (after next page / login)  [cyan]d[/] I submitted  "
                "[cyan]s[/] skip  [cyan]q[/] quit", "fds")
            if choice == "d":
                return "applied_manual"
            if choice == "s":
                return "skipped"
