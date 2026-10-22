"""Naukri adapter.

Naukri flow: job page -> "Apply" -> either applied instantly, or a chatbot drawer
asks questions one at a time (text box / radio / checkbox / chips) and submits after the last one.
Clicking Apply on a job with no questions submits immediately — so the runner asks you
BEFORE clicking Apply, and every uncertain chatbot answer is shown to you before it is sent.

Naukri changes its markup now and then. All selectors live in SEL; when something breaks,
check data/debug/*.html and update them here.
"""
import re
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.models import Field, Job, JobDetail, SkipJob
from src.core.session import ApplySession
from src.sites.base import SiteAdapter

BASE = "https://www.naukri.com"

SEL = {
    "card": "div.srp-jobtuple-wrapper",
    "card_title": "a.title",
    "card_company": "a.comp-name",
    "card_location": ".locWdth",
    "card_exp": ".expwdth",
    "jd": "[class*='dang-inner-html'], .job-desc",
    "apply_btn": "#apply-button",
    "company_site_btn": "#company-site-button",
    "already_applied": "#already-applied",
    "sort_btn": "button#filter-sort",                  # "Sort by: Relevance ▾"
    "sort_date": "a[data-id='filter-sort-f']",         # dropdown item "Date"
    "chat_drawer": "[class*='chatbot_Drawer']",
    "chat_bot_msg": "li.botItem",
    "chat_text": "div.textArea[contenteditable='true']",
    "chat_radio": ".ssrc__radio-btn-container",
    "chat_checkbox": ".mcc__checkbox, .multicheckboxes-container label",
    "chat_chip": ".chatbot_Chip",
    "chat_send": ".sendMsg",
    "chat_close": "[class*='chatbot_Drawer'] [class*='crossIcon']",
}
SUCCESS_RE = re.compile(r"successfully applied|applied successfully|application sent|^\s*Applied to\b", re.I)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def _text(locator) -> str:
    try:
        return locator.first.inner_text(timeout=1000).strip() if locator.count() else ""
    except PWTimeout:
        return ""


class NaukriAdapter(SiteAdapter):
    name = "naukri"

    def ensure_logged_in(self, page):
        page.goto(f"{BASE}/mnjuser/homepage", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        while "nlogin" in page.url or "login" in page.url.lower():
            input("Log in to Naukri in the browser window, then press Enter here...")
            page.goto(f"{BASE}/mnjuser/homepage", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        print("Logged in to Naukri.")

    def queries(self) -> Iterator[dict]:
        """Pasted Naukri search links first; else ONE combined search per location with all your stacks
        (Naukri accepts k='golang developer, java developer, backend development')."""
        base = {"pages": self.cfg.get("pages_per_query", 1), "job_age": self.cfg.get("job_age_days")}
        for url in self.cfg.get("search_urls") or []:
            k = parse_qs(urlsplit(url).query).get("k", [""])[0]
            yield {**base, "url": url, "keywords": k}
        qs = self.cfg.get("queries") or []
        if not qs:
            return
        keywords = ", ".join(q["keywords"] for q in qs)
        for loc in qs[0].get("locations") or [""]:
            yield {**base, "keywords": keywords, "location": loc, "experience": qs[0].get("experience")}

    def _search_url(self, q: dict, page_no: int = 1) -> str:
        """Same shape as Naukri's own search:
        /golang-developer-java-developer-jobs[-in-pune]?k=golang developer, java developer&experience=2
          &functionAreaIdGid=3&functionAreaIdGid=5&jobAge=7&sort=f"""
        if q.get("url"):
            url = q["url"]
            parts = urlsplit(url)
            params = parse_qs(parts.query)
            if self.cfg.get("sort_by_date", True):
                params["sort"] = ["f"]
            if q.get("job_age") and "jobAge" not in params:
                params["jobAge"] = [str(q["job_age"])]
            return urlunsplit(parts._replace(query=urlencode(params, doseq=True)))
        path = "-".join(_slug(k) for k in q["keywords"].split(",") if k.strip()) + "-jobs"
        if q.get("location"):
            path += f"-in-{_slug(q['location'])}"
        params = {"k": q["keywords"], "l": q.get("location") or None,
                  "experience": q.get("experience"), "jobAge": q.get("job_age"),
                  "functionAreaIdGid": self.cfg.get("departments") or None,     # 3 = Engineering - Software & QA
                  "wfhType": self.cfg.get("work_mode") or None,
                  "sort": "f" if self.cfg.get("sort_by_date", True) else None}   # f = freshness (Date)
        params = {k: v for k, v in params.items() if v not in (None, "", [])}
        return f"{BASE}/{path}?{urlencode(params, doseq=True)}"

    @staticmethod
    def _page_url(url: str, page_no: int) -> str:
        """Page N of an (already sorted) results URL: /golang-jobs?… -> /golang-jobs-2?…"""
        parts = urlsplit(url)
        path = re.sub(r"-\d+$", "", parts.path.rstrip("/"))
        return urlunsplit(parts._replace(path=f"{path}-{page_no}" if page_no > 1 else path))

    def _sort_by_date(self, page) -> bool:
        """Results page: 'Sort by: Recommended' -> 'Date' (newest first)."""
        try:
            btn = page.locator(SEL["sort_btn"])
            if (btn.first.get_attribute("title") or "").lower() == "date":
                print("  already sorted by date")
                return True
            btn.first.click(timeout=5000)
            page.wait_for_timeout(700)
            opt = page.locator(SEL["sort_date"])
            if not opt.count():
                opt = page.get_by_text(re.compile(r"^\s*Date\s*$")).filter(visible=True)
            before = page.url
            opt.first.click(timeout=5000)
            page.wait_for_timeout(2500)
            page.wait_for_selector(SEL["card"], timeout=15000)
            ok = (page.locator(SEL["sort_btn"]).first.get_attribute("title") or "").lower() == "date"
            print(f"  sorted by date{'' if ok else ' (could not confirm)'}")
            return True
        except (PWTimeout, PlaywrightError):
            print("  could not switch to 'Sort by: Date' — using Naukri's default order")
            dump_debug(page, "naukri-sort")
            return False

    def search(self, page, query: dict) -> Iterator[Job]:
        base = self._search_url(query)
        for page_no in range(1, query["pages"] + 1):
            url = base if page_no == 1 else self._page_url(base, page_no)
            print(f"\nSearching: {url}")
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(SEL["card"], timeout=15000)
            except PWTimeout:
                print("  no job cards found (end of results, or selectors changed)")
                dump_debug(page, "naukri-search")
                return
            if self.cfg.get("sort_by_date", True):
                self._sort_by_date(page)         # no-op when ?sort=f already applied; clicks Date otherwise
            self.human.pause()
            jobs = []
            for card in page.locator(SEL["card"]).all():
                job_id = card.get_attribute("data-job-id")
                link = card.locator(SEL["card_title"])
                href = link.first.get_attribute("href") if link.count() else None
                if not job_id or not href:
                    continue
                jobs.append(Job(
                    site=self.name, id=job_id, url=href,
                    title=_text(link),
                    company=_text(card.locator(SEL["card_company"])),
                    location=_text(card.locator(SEL["card_location"])),
                    experience=_text(card.locator(SEL["card_exp"])),
                ))
            print(f"  page {page_no}: {len(jobs)} jobs")
            yield from jobs  # collected first so navigating to a job doesn't detach cards

    def open_job(self, page, job: Job) -> JobDetail:
        page.goto(job.url, wait_until="domcontentloaded")
        any_btn = f"{SEL['apply_btn']}, {SEL['company_site_btn']}, {SEL['already_applied']}"
        try:
            page.wait_for_selector(any_btn, timeout=15000)
        except PWTimeout:
            dump_debug(page, "naukri-job")
        self.human.pause()
        return JobDetail(
            job=job,
            description=_text(page.locator(SEL["jd"])),
            external=page.locator(SEL["company_site_btn"]).count() > 0
            and page.locator(SEL["apply_btn"]).count() == 0,
            already_applied=page.locator(SEL["already_applied"]).count() > 0,
        )

    def apply(self, page, session: ApplySession) -> str:
        btn = page.locator(SEL["apply_btn"]).first
        if not btn.count():
            return "failed"
        btn.click()
        page.wait_for_timeout(2500)
        try:
            return self._chatbot(page, session)
        except SkipJob:
            self._close_chat(page)
            raise

    def open_external(self, page):
        btn = page.locator(SEL["company_site_btn"]).first
        if not btn.count():
            return None
        self.human.pause()
        return self.click_to_new_tab(page, btn)

    # --- chatbot -----------------------------------------------------------

    def _applied(self, page) -> bool:
        return ("saveApply" in page.url or "myapply" in page.url
                or page.get_by_text(SUCCESS_RE).count() > 0
                or page.locator(SEL["already_applied"]).count() > 0)

    def _chatbot(self, page, session: ApplySession) -> str:
        last_q, repeats, idle = None, 0, 0
        for _ in range(40):
            if self._applied(page):
                return "applied"
            if not page.locator(SEL["chat_drawer"]).first.is_visible():
                idle += 1
                if idle > 4:
                    dump_debug(page, "naukri-apply-unknown")
                    return "unknown"
                page.wait_for_timeout(1500)
                continue
            idle = 0

            msgs = page.locator(SEL["chat_bot_msg"])
            question = msgs.nth(msgs.count() - 1).inner_text().strip() if msgs.count() else ""
            if not question or question == last_q:
                repeats += 1
                if repeats >= 4:
                    dump_debug(page, "naukri-chat-stuck")
                    return "failed"
                page.wait_for_timeout(1500)
                continue
            repeats = 0

            field = self._detect_field(page, question)
            if field is None:
                page.wait_for_timeout(1500)
                continue
            value = session.answer(field)
            self._fill(page, field, value)
            last_q = question
            page.wait_for_timeout(2000)
        return "failed"

    def _detect_field(self, page, question: str) -> Field | None:
        radios = page.locator(SEL["chat_radio"])
        if radios.count():
            return Field(question, "radio", [r.inner_text().strip() for r in radios.all()])
        checks = page.locator(SEL["chat_checkbox"])
        if checks.count():
            return Field(question, "checkbox", [c.inner_text().strip() for c in checks.all()])
        chips = page.locator(SEL["chat_chip"])
        if chips.count():
            return Field(question, "chips", [c.inner_text().strip() for c in chips.all()])
        box = page.locator(SEL["chat_text"])
        if box.count() and box.first.is_visible():
            return Field(question, "text")
        return None          # bot message with nothing to answer ("Thank you for your responses")

    def _fill(self, page, field: Field, value: str):
        self.human.pause()
        if field.kind == "radio":
            page.locator(SEL["chat_radio"]).filter(has_text=value).first.click()
        elif field.kind == "checkbox":
            for v in [x.strip() for x in value.split(",") if x.strip()]:
                page.locator(SEL["chat_checkbox"]).filter(has_text=v).first.click()
                self.human.pause()
        elif field.kind == "chips":
            page.locator(SEL["chat_chip"]).filter(has_text=value).first.click()
        else:
            self.human.type(page.locator(SEL["chat_text"]).first, value)
        self.human.pause()
        send = page.locator(SEL["chat_send"]).first
        if send.count() and send.is_visible():
            send.click()

    def _close_chat(self, page):
        close = page.locator(SEL["chat_close"]).first
        if close.count():
            close.click()
        else:
            page.keyboard.press("Escape")
