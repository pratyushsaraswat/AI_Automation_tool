"""TeamLease Digital (teamleasedigital.com) adapter.

Search  : /<keywords>-jobs            e.g. /java-developer-jobs, /software-development-jobs (all on one page)
Card    : div.jobs-card[job_id][job_display_url] — .position, .company-name, experience "2 to 7 Years",
          salary, location, posted date. Real company names -> referral outreach works.
Job page: /jobs/<slug>-<id>; JSON-LD JobPosting + "Key Skills"; apply button span#qapply_<id>
          (data-job_applied="1" when you already applied).
Apply   : same handling as MyCareernet — instant apply / screening dialog / redirect / unknown flow (learned).
"""
import json
import re
from typing import Iterator

from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.models import Job, JobDetail
from src.sites.mycareernet import MyCareernetAdapter, _slug

BASE = "https://www.teamleasedigital.com"
EXP_RE = re.compile(r"(\d+(?:\.\d+)?)\s*to\s*(\d+(?:\.\d+)?)\s*Year", re.I)
CARDS_JS = r"""() => [...document.querySelectorAll('div.jobs-card[job_id]')].map(c => {
  const q = s => (c.querySelector(s)?.innerText || '').replace(/\s+/g, ' ').trim();
  return {id: c.getAttribute('job_id'), href: (c.getAttribute('job_display_url') || '').trim(),
          title: q('.position'), company: q('.company-name'), exp: q('.experience-block'),
          salary: q('.salary-block'), location: q('.location-block'), snippet: q('.job_det'),
          posted: q('.posted-on')};
})"""


class TeamLeaseAdapter(MyCareernetAdapter):
    name = "teamlease"

    def ensure_logged_in(self, page):
        page.goto(f"{BASE}/software-development-jobs", wait_until="domcontentloaded")
        page.wait_for_timeout(4000)
        while self._header_logged_out(page):
            input("Log in to TeamLease Digital in the browser window (Login, top right), then press Enter here...")
            page.goto(f"{BASE}/software-development-jobs", wait_until="domcontentloaded")
            page.wait_for_timeout(4000)
        print("Logged in to TeamLease Digital.")

    @staticmethod
    def _header_logged_out(page) -> bool:
        """Logged out: the header shows the 'Login / Register' block (.non-loggedin-block)."""
        return page.locator(".non-loggedin-block:visible, .login-reg-block:visible").count() > 0

    def _logged_out(self, page) -> bool:
        return self._header_logged_out(page)

    # --- search ----------------------------------------------------------------

    def _search_url(self, q: dict, page_no: int = 1) -> str:
        if q.get("url"):
            return q["url"]
        return f"{BASE}/{_slug(q['keywords'])}-jobs"

    def search(self, page, query: dict) -> Iterator[Job]:
        exp = query.get("experience")
        locs = [l.lower() for l in query.get("locations") or [] if l]
        url = self._search_url(query)
        print(f"\nSearching: {url}")
        page.goto(url, wait_until="domcontentloaded")
        try:
            page.wait_for_selector("div.jobs-card[job_id]", timeout=12000)
        except PWTimeout:
            print("  no jobs for this search on TeamLease Digital")
            return
        for _ in range(self.cfg.get("scrolls", 6)):        # list grows as you scroll
            before = page.locator("div.jobs-card[job_id]").count()
            page.mouse.wheel(0, 6000)
            page.wait_for_timeout(1500)
            if page.locator("div.jobs-card[job_id]").count() == before:
                break
        cards = page.evaluate(CARDS_JS)
        jobs = []
        for c in cards:
            m = EXP_RE.search(c["exp"])
            if exp is not None and m and not (float(m.group(1)) <= exp + 0.5 and exp <= float(m.group(2)) + 1):
                continue
            if locs and c["location"] and not any(l in c["location"].lower() for l in locs):
                continue
            if not c["id"] or not c["href"]:
                continue
            jobs.append(Job(site=self.name, id=c["id"], url=BASE + c["href"], title=c["title"],
                            company=c["company"], experience=c["exp"], location=c["location"],
                            snippet=f"{c['snippet']} | {c['salary']} | {c['posted']}"))
        print(f"  {len(cards)} jobs, {len(jobs)} in your experience/location")
        yield from jobs

    # --- job page ------------------------------------------------------------------

    def open_job(self, page, job: Job) -> JobDetail:
        page.goto(job.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        desc = ""
        for raw in page.locator("script[type='application/ld+json']").all_text_contents():
            try:
                data = json.loads(raw)
            except ValueError:
                continue
            for d in data if isinstance(data, list) else [data]:
                if isinstance(d, dict) and d.get("@type") == "JobPosting":
                    desc = re.sub(r"<[^>]+>", " ", d.get("description", ""))
        body = " ".join(page.locator("body").inner_text().split())
        tags = [t.strip() for t in page.locator("#skills-block .keyskills .cityJobNames").all_inner_texts() if t.strip()]
        if tags:
            job.tags = tags
        jd = re.search(r"Job Description\s+(.*?)\s+Job Details", body)
        desc = " ".join(filter(None, [desc, jd.group(1) if jd else "", "Key skills: " + ", ".join(job.tags)]))
        btn = page.locator(f"#qapply_{job.id}")
        applied = btn.count() > 0 and (btn.first.get_attribute("data-job_applied") or "0") == "1"
        return JobDetail(job=job, description=desc, already_applied=applied,
                         closed=bool(re.search(r"no longer (available|accepting)|job (has )?expired", body, re.I)))

    def _apply_button(self, page):
        btn = page.locator("[id^='qapply_']:visible, .det_apply_job:visible")
        if btn.count():
            return btn.first
        dump_debug(page, "teamlease-noapply")
        return None
