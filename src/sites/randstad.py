"""Randstad India (randstad.in) adapter.

Search  : IT categories (keyword search there finds almost nothing):
            /jobs/s-it/s2-software-and-application-developers/   (software / app developers)
            /jobs/s-it/                                          (all IT)
          or keywords -> /jobs/q-<keywords>/ ; pages: .../page-N/
Job page: /jobs/<title>_<city>_<uuid>/ — JSON-LD JobPosting (description), "experience N" (years),
          button[data-locator-id=jobdetails_header_apply_button]
Apply   : -> /my-randstad/apply/<uuid>/ form: resume upload, name, phones, skills, experience (+ terms).
          Filled by FormFiller (profile + memory; new questions asked once); password fields are never touched.
A OneTrust cookie banner blocks clicks until accepted — handled.
Companies are shown as "A client of Randstad India" -> referral outreach is skipped.
"""
import json
import re
from typing import Iterator
from urllib.parse import quote

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.forms import FormFiller
from src.core.models import Job, JobDetail, SkipJob
from src.core.review import console
from src.core.session import ApplySession
from src.sites.base import SiteAdapter
from src.sites.mycareernet import _slug

BASE = "https://www.randstad.in"
JOB_RE = re.compile(r"/jobs/[^/]+_[^/]+_([0-9a-f-]{36})/?$")
DEFAULT_URLS = [f"{BASE}/jobs/s-it/s2-software-and-application-developers/", f"{BASE}/jobs/s-it/"]
SEL = {
    "cookies": "#onetrust-accept-btn-handler",
    "apply": "button[data-locator-id=jobdetails_header_apply_button]",
    "applied": "[data-locator-id=jobdetails_applyjobs_viewapplicationbutton]",   # 'view application' = done
    "form": "form:has(input[name='firstName']), form:has(input[type='file']), main",
}
SUBMIT_RE = re.compile(r"^\s*(apply|apply now|submit|submit application|send application)\s*$", re.I)
DONE_RE = re.compile(r"(thank you for (your )?appl|application (has been )?(sent|submitted|received)|"
                     r"you('ve| have) (already )?applied|successfully applied)", re.I)
CARDS_JS = r"""() => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('a[href*="/jobs/"]')) {
    const href = a.getAttribute('href') || '';
    const m = href.match(/\/jobs\/[^/]+_[^/]+_([0-9a-f-]{36})\/?$/);
    if (!m || seen.has(m[1])) continue;
    seen.add(m[1]);
    let card = a; for (let i = 0; i < 5 && card.parentElement; i++) { card = card.parentElement; if (card.tagName === 'LI' || card.tagName === 'ARTICLE') break; }
    const lines = (card.innerText || a.innerText).split('\n').map(s => s.trim()).filter(Boolean);
    out.push({id: m[1], href, title: (a.innerText || lines[0] || '').trim().split('\n')[0], text: lines.join(' | ')});
  }
  return out;
}"""


YEARS_RE = re.compile(r"(?:at least|minimum(?: of)?|min\.?)?\s*(\d{1,2})\s*(?:\+|plus)?\s*(?:(?:-|–|to)\s*(\d{1,2}))?"
                      r"\s*\+?\s*(?:years?|yrs?)", re.I)


def jd_min_years(text: str) -> int | None:
    """Smallest 'N years' / 'N-M years' / 'at least N years' requirement in a JD (ignores > 25)."""
    vals = [int(m.group(1)) for m in YEARS_RE.finditer(text or "") if int(m.group(1)) <= 25]
    return min(vals) if vals else None


class RandstadAdapter(SiteAdapter):
    name = "randstad"

    @staticmethod
    def _cookies(page):
        btn = page.locator(SEL["cookies"])
        try:
            if btn.count() and btn.first.is_visible():
                btn.first.click(timeout=3000)
                page.wait_for_timeout(500)
        except PlaywrightError:
            pass

    def ensure_logged_in(self, page):
        page.goto(f"{BASE}/my-randstad/", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        self._cookies(page)
        while self._logged_out(page):
            input("Log in to Randstad in the browser window (my randstad -> login; create an account once if "
                  "you don't have one), then press Enter here...")
            page.goto(f"{BASE}/my-randstad/", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            self._cookies(page)
        print("Logged in to Randstad.")

    @staticmethod
    def _logged_out(page) -> bool:
        return ("login" in page.url.lower() or page.locator("input[type='password']:visible").count() > 0)

    # --- search ----------------------------------------------------------------

    def queries(self) -> Iterator[dict]:
        urls = self.cfg.get("search_urls") or []
        for url in urls:
            yield {"url": url, "keywords": ""}
        for q in self.cfg.get("queries") or []:
            yield {"keywords": q["keywords"], "experience": q.get("experience"),
                   "locations": q.get("locations") or [""]}
        if not urls and not self.cfg.get("queries"):
            for url in DEFAULT_URLS:
                yield {"url": url, "keywords": ""}

    def _page_url(self, q: dict, page_no: int) -> str:
        base = q.get("url") or f"{BASE}/jobs/q-{quote(_slug(q['keywords']))}/"
        base = re.sub(r"page-\d+/?$", "", base.rstrip("/") + "/")
        return base if page_no == 1 else f"{base}page-{page_no}/"

    def search(self, page, query: dict) -> Iterator[Job]:
        locs = [l.lower() for l in query.get("locations") or [] if l]
        for page_no in range(1, self.cfg.get("pages_per_query", 4) + 1):
            url = self._page_url(query, page_no)
            print(f"\nSearching: {url}")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            self._cookies(page)
            cards = page.evaluate(CARDS_JS)
            if not cards:
                print("  no more jobs")
                return
            jobs = []
            for c in cards:
                if locs and not any(l in c["text"].lower() or l in c["href"] for l in locs):
                    continue
                city = c["href"].rstrip("/").split("_")[-2].replace("-", " ") if c["href"].count("_") >= 2 else ""
                jobs.append(Job(site=self.name, id=c["id"], url=BASE + c["href"], title=c["title"],
                                company="A client of Randstad India", location=city, snippet=c["text"]))
            print(f"  page {page_no}: {len(cards)} jobs")
            yield from jobs
            if len(cards) < 25:
                return

    # --- job page ------------------------------------------------------------------

    def open_job(self, page, job: Job) -> JobDetail:
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        self._cookies(page)
        desc = ""
        for raw in page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for d in data if isinstance(data, list) else [data]:
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    desc = re.sub(r"<[^>]+>", " ", d.get("description", ""))
                    job.title = d.get("title") or job.title
        body = " ".join(page.locator("body").inner_text().split())
        min_years = jd_min_years(desc or body)
        if min_years is not None:
            job.experience = f"{min_years}+ yrs (from JD)"
        want = self.cfg.get("experience", 2)
        too_senior = min_years is not None and want is not None and min_years > want + 2
        return JobDetail(
            job=job, description=" ".join((desc or body[:6000]).split()),
            already_applied=bool(re.search(r"you('ve| have) (already )?applied", body, re.I))
            or page.locator(SEL["applied"]).count() > 0,
            closed=bool(re.search(r"this job (is )?(no longer|closed|expired)", body, re.I)),
            skip_reason="too_senior" if too_senior else "",
        )

    # --- apply ---------------------------------------------------------------------

    def apply(self, page, session: ApplySession) -> str:
        self._cookies(page)
        btn = page.locator(SEL["apply"]).first
        if not btn.count() and page.locator(SEL["applied"]).count():
            return "applied"
        if not btn.count():
            dump_debug(page, "randstad-noapply")
            return "failed"
        self.human.pause()
        try:
            btn.click(timeout=8000)
        except PlaywrightError:
            btn.evaluate("e => e.click()")
        try:
            page.wait_for_url(re.compile(r"/apply/"), timeout=10000)
        except PWTimeout:
            pass
        page.wait_for_timeout(2500)
        self._cookies(page)
        if DONE_RE.search(page.locator("body").inner_text()):
            return "applied"
        if page.locator("input[name='retypePassword']:visible").count():
            console.print("[yellow]Randstad asks to create an account — log in first: "
                          "python -m src.main login --site randstad[/]")
            return "failed"

        form = page.locator(SEL["form"]).first
        filler = FormFiller(session, upload="always")
        controls = filler.fill(form)
        job = session.detail.job
        submit = form.locator("button:visible").filter(has_text=SUBMIT_RE)
        if not submit.count():
            submit = page.locator("button:visible").filter(has_text=SUBMIT_RE)
        if not submit.count():
            dump_debug(page, "randstad-nosubmit")
            return "failed"
        try:
            return filler.review_and_submit(
                form, controls, submit.last,
                success=lambda: bool(DONE_RE.search(page.locator("body").inner_text())) or "/apply/" not in page.url,
                title=f"Randstad: {job.title}")
        except SkipJob:
            raise
