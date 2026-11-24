"""Wellfound (wellfound.com, ex-AngelList Talent) adapter — startup jobs.

Search  : role pages  /role/l/<role>/<place>   e.g. /role/l/software-engineer/india, .../bangalore
                      /role/r/<role>           remote jobs;  paging ?page=N
          Each page carries its results as JSON (__NEXT_DATA__ apollo state): title, company, full
          description, yearsExperienceMin, liveStartAt (posted), jobType — read directly, no card scraping.
Job page: /jobs/<id>-<slug>   JobListing:<id>.currentUserApplied tells if you already applied.
Apply   : "Apply" opens a modal on Wellfound itself (also for Greenhouse/Ashby/Lever-backed jobs):
          "What interests you about working for this company?" (+ ATS questions: phone, LinkedIn,
          yes/no, dropdowns) -> [data-test=JobApplicationModal--SubmitButton] "Send application".
          The interest note is written from the job + your profile; other fields via FormFiller.
          Jobs that don't accept applicants from your location have every input disabled -> skipped.
Your Wellfound profile (resume, skills) is what recruiters see — keep it complete.
"""
import re
import time
from typing import Iterator

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.forms import FormFiller
from src.core.models import Job, JobDetail, SkipJob
from src.core.resume_parser import load_resume_text
from src.core.review import console
from src.core.session import ApplySession
from src.outreach.skills import find_skills
from src.sites.base import SiteAdapter
from src.sites.randstad import jd_min_years

BASE = "https://wellfound.com"
DEFAULT_ROLES = ["software engineer", "backend engineer", "full stack engineer", "java developer",
                 "golang developer"]
SEL = {
    "apply": "button:visible",
    "modal": "[role=dialog]:visible",
    "submit": "[data-test='JobApplicationModal--SubmitButton']",
}
APPLY_RE = re.compile(r"^\s*apply( now)?\s*$", re.I)
APPLIED_RE = re.compile(r"^[\W_]*applied\s*$", re.I)      # "✓ Applied"
INTERN_RE = re.compile(r"\bintern(ship)?\b|\btrainee\b", re.I)
BLOCKED_RE = re.compile(r"not accepting applications from your (current )?location", re.I)
DONE_RE = re.compile(r"application (was )?(sent|submitted)|you('ve| have) applied|applied successfully", re.I)
NOTE_RE = re.compile(r"what interests you|why (do you want|are you interested|.*join)|cover letter|note to|"
                     r"why .*(company|us)\b", re.I)

# apollo state of the page -> list of jobs (search) or one job (job page)
DATA_JS = r"""(jobId) => {
  const s = document.querySelector('#__NEXT_DATA__'); if (!s) return null;
  const find = o => { if (o && typeof o === 'object') { if (o.apolloState) return o.apolloState;
      for (const v of Object.values(o)) { const r = find(v); if (r) return r; } } return null; };
  const ap = find(JSON.parse(s.textContent)); if (!ap) return null;
  const d = ap.data || ap;
  if (jobId) {
    const j = d['JobListing:' + jobId] || d['JobListingSearchResult:' + jobId] || {};
    return {applied: !!j.currentUserApplied, description: j.description || '', ymin: j.yearsExperienceMin};
  }
  const company = {};
  for (const v of Object.values(d)) if (v.__typename === 'StartupResult')
    for (const r of v.highlightedJobListings || []) company[r.__ref] = {name: v.name, slug: v.slug};
  return Object.entries(d).filter(([k, v]) => v.__typename === 'JobListingSearchResult').map(([k, j]) => ({
    id: j.id, slug: j.slug, title: j.title, description: j.description || '', ymin: j.yearsExperienceMin,
    posted: j.liveStartAt || 0, type: j.jobType || '', remote: (j.remoteConfig || {}).kind || '',
    places: (j.locationNames || []).concat(j.acceptedRemoteLocationNames || []).slice(0, 3).join(', '),
    pay: j.compensation || '', company: (company[k] || {}).name || '', companySlug: (company[k] || {}).slug || '',
  }));
}"""


def role_slug(text: str) -> str:
    """'Java Developer' -> 'java-developer';  'full stack' -> 'full-stack-engineer'."""
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    s = {"golang": "golang-developer", "go-developer": "golang-developer", "java": "java-developer",
         "backend": "backend-engineer", "backend-developer": "backend-engineer", "full-stack": "full-stack-engineer",
         "full-stack-developer": "full-stack-engineer", "software-developer": "software-engineer",
         "sde": "software-engineer"}.get(s, s)
    return s


def place_slug(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (text or "india").lower()).strip("-")
    return {"bengaluru": "bangalore", "anywhere": "india", "": "india", "gurugram": "gurgaon"}.get(s, s)


def _plain(md: str) -> str:
    return " ".join(re.sub(r"<[^>]+>|[*_#`>]", " ", md or "").split())


class WellfoundAdapter(SiteAdapter):
    name = "wellfound"

    def __init__(self, search_cfg: dict, human):
        super().__init__(search_cfg, human)
        self._desc: dict[str, str] = {}

    def ensure_logged_in(self, page):
        page.goto(f"{BASE}/jobs", wait_until="domcontentloaded")
        page.wait_for_timeout(5000)
        while self._logged_out(page):
            input("Log in to Wellfound in the browser window (complete your profile + upload resume once), "
                  "then press Enter here...")
            page.goto(f"{BASE}/jobs", wait_until="domcontentloaded")
            page.wait_for_timeout(5000)
        print("Logged in to Wellfound.")

    @staticmethod
    def _logged_out(page) -> bool:
        if any(k in page.url for k in ("/login", "/join", "/signup")):
            return True
        return page.locator("a[href*='/login']:visible").count() > 0 and \
            page.get_by_text("Messages", exact=True).count() == 0

    # --- search ----------------------------------------------------------------

    def queries(self) -> Iterator[dict]:
        for url in self.cfg.get("search_urls") or []:
            yield {"url": url, "keywords": ""}
        for q in self.cfg.get("queries") or []:
            for loc in q.get("locations") or [""]:
                yield {"keywords": q["keywords"], "location": loc}
        if not self.cfg.get("search_urls") and not self.cfg.get("queries"):
            for role in DEFAULT_ROLES:
                yield {"keywords": role, "location": "india"}

    def _page_url(self, q: dict, page_no: int) -> str:
        if q.get("url"):
            base = re.sub(r"[?&]page=\d+", "", q["url"])
        else:
            loc = q.get("location", "")
            base = (f"{BASE}/role/r/{role_slug(q['keywords'])}" if place_slug(loc) == "remote"
                    else f"{BASE}/role/l/{role_slug(q['keywords'])}/{place_slug(loc)}")
        if page_no == 1:
            return base
        return f"{base}{'&' if '?' in base else '?'}page={page_no}"

    def search(self, page, query: dict) -> Iterator[Job]:
        want = int(self.cfg.get("experience", 2))
        days = self.cfg.get("date_posted_days") or 30
        oldest = time.time() - int(days) * 86400
        for page_no in range(1, self.cfg.get("pages_per_query", 4) + 1):
            url = self._page_url(query, page_no)
            print(f"\nSearching: {url}")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
            if "/role/" in url and page.url.rstrip("/").endswith("/jobs"):
                print("  Wellfound has no page for this role — try e.g. 'software engineer', 'backend engineer'")
                return
            try:
                cards = page.evaluate(DATA_JS, None) or []
            except PlaywrightError:
                cards = []
            if not cards:
                print("  no more jobs")
                return
            old = senior = intern = 0
            jobs = []
            for c in cards:
                if c["posted"] and c["posted"] < oldest:
                    old += 1
                    continue
                if c["ymin"] is not None and c["ymin"] > want + 2:
                    senior += 1
                    continue
                if want >= 1 and ("intern" in c["type"].lower() or INTERN_RE.search(c["title"])):
                    intern += 1
                    continue
                self._desc[c["id"]] = c["description"]
                where = "Remote" if c["remote"] == "REMOTE" else c["places"]
                jobs.append(Job(site=self.name, id=c["id"], url=f"{BASE}/jobs/{c['id']}-{c['slug']}",
                                title=c["title"], company=c["company"], location=where,
                                experience=f"{c['ymin']}+ yrs" if c["ymin"] is not None else "",
                                snippet=f"{c['pay']} {_plain(c['description'])[:300]}"))
            skipped = ", ".join(f"{n} {why}" for n, why in ((old, f"older than {days}d"),
                                (senior, f"need > {want + 2} yrs"), (intern, "internships")) if n)
            print(f"  page {page_no}: {len(cards)} jobs" + (f" (skipped {skipped})" if skipped else ""))
            yield from jobs
            if len(cards) < 20:
                return

    # --- job page ------------------------------------------------------------------

    def open_job(self, page, job: Job) -> JobDetail:
        page.goto(job.url, wait_until="domcontentloaded")
        try:
            page.locator(SEL["apply"]).filter(has_text=re.compile(r"^[\W_]*(apply( now)?|applied)\s*$", re.I)) \
                .first.wait_for(timeout=8000)
        except PWTimeout:
            pass
        page.wait_for_timeout(1000)
        info = {}
        try:
            info = page.evaluate(DATA_JS, job.id) or {}
        except PlaywrightError:
            pass
        desc = _plain(info.get("description") or self._desc.get(job.id, ""))
        if not desc:
            desc = " ".join(page.locator("main, body").first.inner_text().split())[:8000]
        ymin = info.get("ymin")
        if ymin is None:
            ymin = jd_min_years(desc)
        want = int(self.cfg.get("experience", 2))
        applied = bool(info.get("applied")) or page.locator("button:visible").filter(has_text=APPLIED_RE).count() > 0
        company = page.locator("a[href^='/company/']:visible").first
        return JobDetail(
            job=job, description=desc, already_applied=applied,
            closed=not applied and page.locator(SEL["apply"]).filter(has_text=APPLY_RE).count() == 0,
            company_url=(BASE + (company.get_attribute("href") or "")) if company.count() else "",
            skip_reason="too_senior" if ymin is not None and ymin > want + 2 else "",
        )

    # --- apply ---------------------------------------------------------------------

    def _note(self, session: ApplySession) -> str:
        """Short, specific answer to 'What interests you about working for this company?'."""
        profile = session.engine.profile
        exp = profile.get("experience") or {}
        job = session.detail.job
        core = [s for s in profile.get("skills") or []]                   # your main stack first
        mine = set(find_skills(load_resume_text(session.resume_path))) | set(core)
        wanted = [s for s in find_skills(session.detail.description) if s in mine]
        skills = (sorted(wanted, key=lambda s: (s not in core, wanted.index(s))) or core)[:4]
        tmpl = self.cfg.get("note") or (
            "I'm a {title} at {my_company} with {years} years of hands-on experience in {skills}. "
            "{company}'s {role} role lines up closely with what I build every day, and I'm excited by the "
            "chance to own features end to end at a fast-moving startup. I'd love to bring my backend and "
            "product experience to {company} and grow with the team.")
        return tmpl.format(title=exp.get("current_title", "Software Developer"),
                           my_company=exp.get("current_company", ""), years=exp.get("total_years", 2),
                           skills=", ".join(skills[:-1]) + (f" and {skills[-1]}" if len(skills) > 1 else
                                                            (skills[0] if skills else "software development")),
                           company=job.company or "your", role=job.title)

    def apply(self, page, session: ApplySession) -> str:
        btn = page.locator(SEL["apply"]).filter(has_text=APPLY_RE).first
        if not btn.count():
            dump_debug(page, "wellfound-noapply")
            return "failed"
        self.human.pause()
        btn.click()
        modal = page.locator(SEL["modal"]).first
        try:
            modal.wait_for(timeout=10000)
        except PWTimeout:
            if DONE_RE.search(page.locator("body").inner_text()):
                return "applied"
            dump_debug(page, "wellfound-nomodal")
            return "failed"
        page.wait_for_timeout(1500)
        text = modal.inner_text()
        if BLOCKED_RE.search(text):
            console.print("  [yellow]company doesn't accept applicants from your location — skipped[/]")
            page.keyboard.press("Escape")
            return "skipped"

        # the "why this company" note — written from the job + your profile (shown in the summary)
        note = self._note(session)
        for box in modal.locator("textarea:visible").all():
            label = box.evaluate("e => (e.closest('label') || e.parentElement).innerText || ''")
            if NOTE_RE.search(label) and not box.input_value().strip():
                box.click()
                box.fill(note)          # ~400 chars: typing key by key would take > 30 s
                self.human.pause()

        filler = FormFiller(session, upload="always")
        controls = filler.fill(modal)
        submit = modal.locator(SEL["submit"])
        if not submit.count():
            submit = modal.locator("button:visible").filter(has_text=re.compile(r"send application|submit", re.I))
        if not submit.count():
            dump_debug(page, "wellfound-nosubmit")
            return "failed"
        try:
            return filler.review_and_submit(
                modal, controls, submit.first,
                success=lambda: not page.locator(SEL["modal"]).count()
                or bool(DONE_RE.search(page.locator("body").inner_text())),
                title=f"Wellfound: {session.detail.job.title} — {session.detail.job.company}")
        except SkipJob:
            page.keyboard.press("Escape")
            raise
