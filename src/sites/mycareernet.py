"""MyCareernet (mycareernet.co) adapter.

Search  : /mycareernet/jobs/listings/<keywords>-jobs?page=N        (the site's own search URL)
Card    : title, "Company: …", "Exp: a - b yrs", location, link /mycareernet/jobs/<Title>-<id>
Job page: JSON-LD JobPosting (description incl. "Key Skills: …"); button "Login to Apply" when logged out,
          "Apply"/"Apply Now" when logged in.
Apply   : click Apply, then whatever the site does —
          already/instantly applied ............ detected
          a questions form / dialog ............ filled with FormFiller (profile + memory, asks the rest)
          redirect / new tab to a company site . handed to src/ats (Workday/Greenhouse/Lever/learned sites)
          anything else ........................ you finish in the browser; the tool records + learns

The job API responses are encrypted, so this reads the pages like you do.
Most companies are shown anonymised ("IT Services Organization"); referral outreach is skipped for those.
"""
import json
import re
from typing import Iterator
from urllib.parse import quote, urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.forms import FormFiller
from src.core.models import Job, JobDetail, SkipJob
from src.core.review import console
from src.core.session import ApplySession
from src.sites.base import SiteAdapter

BASE = "https://mycareernet.co"
SEL = {
    "card_link": "a[href*='/mycareernet/jobs/']",
    "login_btn": "text=/Signup\\s*\\/\\s*Login/i",
    "login_to_apply": "button:has-text('Login to Apply')",
    "dialog": "[role='dialog']:visible, .modal.show:visible, .modal-dialog:visible, mat-dialog-container:visible",
}
APPLY_RE = re.compile(r"^\s*(apply|apply now|quick apply|easy apply)\s*$", re.I)
APPLIED_RE = re.compile(r"(already applied|applied successfully|successfully applied|application (has been )?"
                        r"(submitted|received)|you have applied|^\s*applied\s*$)", re.I)
SUBMIT_RE = re.compile(r"^\s*(submit|apply|confirm|continue|next|save)( application)?\s*$", re.I)
EXP_RE = re.compile(r"Exp:\s*(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)", re.I)

CARDS_JS = r"""() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/mycareernet/jobs/"]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/-(\d+)\/?$/);
    if (!m || seen.has(m[1]) || !/apply/i.test(a.innerText)) continue;
    seen.add(m[1]);
    let card = a; for (let i = 0; i < 6 && card.parentElement; i++) { card = card.parentElement; if (/Company:/.test(card.innerText) && /Exp:/.test(card.innerText)) break; }
    const lines = card.innerText.split('\n').map(s => s.trim()).filter(Boolean);
    const get = re => (lines.find(l => re.test(l)) || '').replace(re, '').trim();
    out.push({id: m[1], href, title: lines[0] || '', company: get(/^Company:\s*/i), exp: (lines.find(l => /^Exp:/i.test(l)) || ''),
              text: lines.join(' | ')});
  }
  return out;
}"""
ANON_COMPANY = re.compile(r"\b(organi[sz]ation|large global|leading|reputed|confidential|client|mnc|company|firm)\b", re.I)


def _slug(text: str) -> str:
    text = text.lower().replace("c++", "c plus plus").replace("c#", "c sharp").replace(".net", " dot net")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


class MyCareernetAdapter(SiteAdapter):
    name = "mycareernet"

    def ensure_logged_in(self, page):
        page.goto(f"{BASE}/mycareernet/jobs", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        while page.locator(SEL["login_btn"]).filter(visible=True).count():
            input("Log in to MyCareernet in the browser window (Signup/Login, top right), then press Enter here...")
            page.goto(f"{BASE}/mycareernet/jobs", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        print("Logged in to MyCareernet.")

    # --- search ----------------------------------------------------------------

    def queries(self) -> Iterator[dict]:
        for url in self.cfg.get("search_urls") or []:
            yield {"url": url, "keywords": ""}
        for q in self.cfg.get("queries") or []:
            yield {"keywords": q["keywords"], "locations": q.get("locations") or [""],
                   "experience": q.get("experience")}

    def _search_url(self, q: dict, page_no: int) -> str:
        if q.get("url"):
            base = re.sub(r"([?&])page=\d+", "", q["url"]).rstrip("?&")
            return f"{base}{'&' if '?' in base else '?'}page={page_no}"
        return f"{BASE}/mycareernet/jobs/listings/{quote(_slug(q['keywords']))}-jobs?page={page_no}"

    def search(self, page, query: dict) -> Iterator[Job]:
        exp = query.get("experience")
        locs = [l.lower() for l in query.get("locations") or [] if l]
        for page_no in range(1, self.cfg.get("pages_per_query", 3) + 1):
            url = self._search_url(query, page_no)
            print(f"\nSearching: {url}")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(SEL["card_link"], timeout=15000)
            except PWTimeout:
                print("  no job cards (end of results, or the page changed)")
                dump_debug(page, "mycareernet-search")
                return
            page.wait_for_timeout(1500)
            cards = page.evaluate(CARDS_JS)
            jobs = []
            for c in cards:
                m = EXP_RE.search(c["exp"])
                if exp is not None and m and not (float(m.group(1)) <= exp + 0.5 and exp <= float(m.group(2)) + 1):
                    continue                     # outside your experience band
                if locs and not any(l in c["text"].lower() for l in locs) and "pan india" not in c["text"].lower():
                    continue
                loc = c["text"].split("|")[-2].strip() if c["text"].count("|") >= 2 else ""
                jobs.append(Job(site=self.name, id=c["id"], url=BASE + c["href"], title=c["title"],
                                company=c["company"], experience=c["exp"].replace("Exp:", "").strip(),
                                location=loc, snippet=c["text"]))
            print(f"  page {page_no}: {len(cards)} jobs, {len(jobs)} in your experience/location")
            yield from jobs
            if len(cards) < 10:
                return

    # --- job page ------------------------------------------------------------------

    def open_job(self, page, job: Job) -> JobDetail:
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3500)
        desc = ""
        for raw in page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for d in data if isinstance(data, list) else [data]:
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    desc = re.sub(r"<[^>]+>", " ", d.get("description", ""))
                    org = (d.get("hiringOrganization") or {}).get("name") if isinstance(d.get("hiringOrganization"), dict) else ""
                    if org and not job.company:
                        job.company = org
        if not desc:
            desc = page.locator("main, body").first.inner_text()[:8000]
        desc = " ".join(desc.split())
        skills = re.search(r"Key Skills:\s*([^.]{3,300}?)(?:Roles|Responsibilit|Qualification|$)", desc, re.I)
        if skills:
            job.tags = [s.strip() for s in skills.group(1).split(",") if s.strip()]
        body = page.locator("body").inner_text()
        return JobDetail(
            job=job, description=desc,
            already_applied=bool(re.search(r"\b(already applied|you have applied)\b", body, re.I))
                            or self._apply_button(page) is None and bool(re.search(r"\bApplied\b", body)),
            closed=bool(re.search(r"no longer accepting|job (has )?expired|position (is )?closed", body, re.I)),
        )

    def _logged_out(self, page) -> bool:
        return page.locator(SEL["login_to_apply"]).filter(visible=True).count() > 0

    def _apply_button(self, page):
        if page.locator(SEL["login_to_apply"]).filter(visible=True).count():
            return None
        btn = page.locator("button:visible, a:visible").filter(has_text=APPLY_RE)
        return btn.first if btn.count() else None

    # --- apply ---------------------------------------------------------------------

    def apply(self, page, session: ApplySession) -> str:
        if self._logged_out(page):
            console.print(f"[yellow]Logged out — run: python -m src.main login --site {self.name}[/]")
            return "failed"
        btn = self._apply_button(page)
        if not btn:
            dump_debug(page, "mycareernet-noapply")
            return "failed"
        self.human.pause()
        host_before = urlsplit(page.url).netloc
        tabs_before = set(page.context.pages)
        text_before = self._applied(page)                 # success words already on the page don't count
        try:
            btn.click(timeout=8000)
        except PlaywrightError:
            btn.evaluate("e => e.click()")

        # watch what happens (every 0.3s, up to 12s): success message (often a short toast), new tab,
        # redirect to another site, or a questions dialog
        outcome, new_tab = None, None
        for _ in range(40):
            page.wait_for_timeout(300)
            new = [t for t in page.context.pages if t not in tabs_before]
            if new:
                outcome, new_tab = "tab", new[0]
                break
            try:
                if not text_before and self._applied(page):
                    return "applied"
                if urlsplit(page.url).netloc != host_before:
                    outcome = "redirect"
                    break
                if page.locator(SEL["dialog"]).count():
                    outcome = "dialog"
                    break
            except PlaywrightError:
                continue                                  # page is navigating

        if outcome == "tab":                              # company site in a new tab
            from src.ats import apply_external
            new_tab.wait_for_load_state("domcontentloaded")
            try:
                status, _ = apply_external(new_tab, session)
            finally:
                new_tab.close()
                page.bring_to_front()
            return status
        if outcome == "redirect":                         # redirected to another site in the same tab
            from src.ats import apply_external
            status, _ = apply_external(page, session)
            return status
        if outcome == "dialog":                           # screening questions / confirmation
            return self._fill_dialog(page, page.locator(SEL["dialog"]).last, session)

        # still on the job site, no message seen: don't scan the page (it's a listing) — check the job itself
        dump_debug(page, f"{self.name}-after-apply")
        if self._check_applied_on_job_page(page, session.detail.job):
            return "applied"
        if session.reviewer.auto_submit:
            console.print("  [yellow]couldn't confirm — logged as 'unknown' (check data/debug)[/]")
            return "unknown"
        ok = session.reviewer.choose("Couldn't confirm. Did it apply? [cyan]y[/] yes  [cyan]n[/] no", "yn")
        return "applied" if ok == "y" else "failed"

    def _check_applied_on_job_page(self, page, job) -> bool:
        """Reload the job page and ask it (subclasses know their 'applied' markers)."""
        try:
            page.goto(job.url, wait_until="domcontentloaded")
            page.wait_for_timeout(2500)
            return self.open_job(page, job).already_applied
        except PlaywrightError:
            return False

    def _applied(self, page) -> bool:
        """Success text anywhere visible ('Your application has been submitted successfully.')."""
        try:
            return bool(APPLIED_RE.search(page.locator("body").inner_text(timeout=2000)))
        except PlaywrightError:
            return False

    def _fill_dialog(self, page, dialog, session: ApplySession) -> str:
        filler = FormFiller(session, upload="always")
        for _ in range(8):                                # multi-step dialogs
            controls = filler.fill(dialog)
            job = session.detail.job
            try:
                filler.review_edits(dialog, controls, f"MyCareernet: {job.title}", "SUBMIT")
            except SkipJob:
                page.keyboard.press("Escape")
                raise
            nxt = dialog.locator("button:visible").filter(has_text=SUBMIT_RE)
            if not nxt.count():
                dump_debug(page, "mycareernet-nosubmit")
                return "failed"
            try:
                nxt.last.click()
            except PlaywrightError:
                nxt.last.evaluate("e => e.click()")
            page.wait_for_timeout(2500)
            if self._applied(page):
                return "applied"
            if not page.locator(SEL["dialog"]).count():
                return "applied" if self._applied(page) else "unknown"
            dialog = page.locator(SEL["dialog"]).last
        return "unknown"


def is_anonymous_company(name: str) -> bool:
    """'IT Services Organization', 'A Large Global Organization' -> no real company to message."""
    return not name or bool(ANON_COMPANY.search(name))
