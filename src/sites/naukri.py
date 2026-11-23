"""Naukri adapter.

Naukri flow: job page -> "Apply" -> either applied instantly, or a chatbot drawer
asks questions one at a time (text box / radio / checkbox / chips) and submits after the last one.
Clicking Apply on a job with no questions submits immediately — so the runner asks you
BEFORE clicking Apply, and every uncertain chatbot answer is shown to you before it is sent.

Naukri changes its markup now and then. All selectors live in SEL; when something breaks,
check data/debug/*.html and update them here.
"""
import html
import re
from datetime import datetime, timezone
from typing import Iterator
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.models import Field, Job, JobDetail, SkipJob
from src.core.session import ApplySession
from src.sites.base import SiteAdapter

BASE = "https://www.naukri.com"
REC_PAGE = f"{BASE}/mnjuser/recommended-jobs"
RECOMMENDED_APIS = (
    "/jobapi/v3/search?noOfResults=20&urlType=recommended&searchType=recommended&pageNo={page}&src=recommendedJobs",
    "/jobapi/v3/search?noOfResults=20&urlType=recommended_jobs&searchType=reco&pageNo={page}&src=mnjRecommended",
    "/jobapi/v4/search?noOfResults=20&searchType=recommend&pageNo={page}&src=recommendedJobs",
)

SEL = {
    "card": "div.srp-jobtuple-wrapper",
    "card_title": "a.title",
    "card_company": "a.comp-name",
    "card_location": ".locWdth",
    "card_exp": ".expwdth",
    "card_salary": ".sal-wrap, .salary, [title*='Lac' i], [title*='Lakh' i]",
    "card_tags": "ul.tags-gt li",
    "card_snippet": "span.job-desc",
    "key_skills": "[class*='key-skill'] [class*='chip']",
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
    "chat_radio_label": ".ssrc__radio-btn-container label",   # options are labels; nested boxes made blanks
    "chat_checkbox": ".mcc__checkbox, .multicheckboxes-container label",
    "chat_chip": ".chatbot_Chip",
    "chat_send": ".sendMsg",
    "chat_close": "[class*='chatbot_Drawer'] [class*='crossIcon']",
}
ACK_RE = re.compile(r"^(got it|great|thanks?|thank you|no worries|okay|ok|noted|perfect|awesome|sure|"
                    r"alright|cool)\b[!. ]*", re.I)
CHIP_NOISE = re.compile(r"^(i made a mistake|skip this question|skip)$", re.I)   # bot controls, not answers
SUCCESS_RE = re.compile(r"successfully applied|applied successfully|application sent|^\s*Applied to\b", re.I)


AGE_RE = re.compile(
    r"\b(\d+)\s*\+?\s*(hour|day|week|month)s?\s+ago\b|\b(just now|today|few hours ago)\b", re.I)


def age_in_days(value) -> float | None:
    """How old a Naukri 'posted' value is. None when the card doesn't say."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        ms = value if value > 10_000_000_000 else value * 1000
        return (datetime.now(timezone.utc).timestamp() * 1000 - ms) / 86_400_000
    text = str(value).strip()
    m = AGE_RE.search(text)
    if m:
        if m.group(3):
            return 0
        n, unit = int(m.group(1)), m.group(2).lower()
        if unit.startswith("hour"):
            return n / 24
        if unit.startswith("week"):
            return n * 7
        if unit.startswith("month"):
            return n * 30
        return float(n)
    token = text.split()[0][:10]
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%m/%d/%Y"):
        try:
            posted = datetime.strptime(token, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        return (datetime.now(timezone.utc) - posted).total_seconds() / 86_400
    return None


def within_days(value, max_days) -> bool:
    """True when the posting is recent enough, or when no date is shown."""
    if not max_days:
        return True
    age = age_in_days(value)
    return age is None or age <= float(max_days) + 0.5


def salary_ceiling_lpa(text: str) -> float | None:
    """Highest figure in a posted package, in LPA. None when the card says nothing or 'Not disclosed'."""
    if not text or re.search(r"not disclosed|unspecified", text, re.I):
        return None
    span = re.search(
        r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*(?:lacs?|lakhs?|lpa)\b", text, re.I)
    if span:
        return float(span.group(2))
    one = re.search(r"(\d+(?:\.\d+)?)\s*(?:lacs?|lakhs?|lpa)\b", text, re.I)
    if one:
        return float(one.group(1))
    # A bare 50,000 in the description is not a 0.5 LPA package. Only annual figures count.
    annual = [int(n.replace(",", "")) for n in re.findall(r"\d[\d,]{4,}", text)]
    annual = [n for n in annual if n >= 300000]
    if annual:
        return max(annual) / 100000
    return None


def _plain(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", text or ""))).strip()


def _placeholder(item: dict, *types: str) -> str:
    wanted = {t.lower() for t in types}
    for p in item.get("placeholders") or []:
        if isinstance(p, dict) and (p.get("type") or "").lower() in wanted:
            return (p.get("label") or p.get("text") or "").strip()
    return ""


def _posted_label(item: dict) -> str:
    label = item.get("footerPlaceholderLabel") or item.get("createdDate") or ""
    return str(label or _placeholder(item, "date", "freshness"))


def jobs_from_api(payload: dict, max_days=None) -> list[Job]:
    """Turn a jobapi search or recommended-jobs payload into jobs."""
    rows = (payload.get("jobDetails") or payload.get("recommendedJobs") or payload.get("jobs") or [])
    if isinstance(rows, dict):
        rows = rows.get("list") or rows.get("jobs") or rows.get("jobDetails") or []
    out = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        job_id = str(item.get("jobId") or item.get("groupId") or "").strip()
        title = _plain(item.get("title") or item.get("jobTitle") or "")
        url = item.get("jdURL") or item.get("staticUrl") or item.get("jobUrl") or ""
        if isinstance(url, str) and url.startswith("/"):
            url = BASE + url
        if not job_id or not title or not url:
            continue
        if not within_days(_posted_label(item), max_days):
            continue
        skills = item.get("tagsAndSkills") or item.get("tags") or ""
        if isinstance(skills, str):
            tags = [s.strip() for s in re.split(r",|\|", skills) if s.strip()]
        elif isinstance(skills, list):
            tags = [str(s).strip() for s in skills if str(s).strip()]
        else:
            tags = []
        out.append(Job(
            site="naukri", id=job_id, url=url.split("?")[0], title=title,
            company=_plain(item.get("companyName") or item.get("company") or ""),
            location=_placeholder(item, "location", "locations"),
            experience=_placeholder(item, "experience", "exp"),
            tags=tags,
            snippet=_plain(item.get("jobDescription") or item.get("description") or "")[:500],
            salary=_placeholder(item, "salary", "ctc"),
        ))
    return out


def _slug(text: str) -> str:
    """Naukri's URL words: 'C++ Developer' -> 'c-plus-plus-developer', 'C#' -> 'c-sharp', '.NET' -> 'dot-net'."""
    text = text.lower().replace("c++", "c plus plus").replace("c#", "c sharp").replace(".net", " dot net")
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")


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
        """Recommended jobs (Jobs menu) first, then one search per stack.
        A combined keyword makes Naukri pad the page with unrelated roles."""
        if self.cfg.get("recommended", True):
            yield {"pages": self.cfg.get("recommended_pages", 2), "recommended": True, "keywords": ""}
        base = {"pages": self.cfg.get("pages_per_query", 1), "job_age": self.cfg.get("job_age_days")}
        for url in self.cfg.get("search_urls") or []:
            k = parse_qs(urlsplit(url).query).get("k", [""])[0]
            for part in [p.strip() for p in k.split(",") if p.strip()] or [k]:
                yield {**base, "url": url, "keywords": part}
        for q in self.cfg.get("queries") or []:
            for loc in q.get("locations") or [""]:
                yield {**base, "keywords": q["keywords"], "location": loc, "experience": q.get("experience")}

    def _search_params(self, q: dict) -> dict:
        """The search-box query. Same shape as the results page that lists the role you typed.

        Department, salary-band, jobAge and sort parameters are not sent. On a keyword
        search Naukri answers those by filling the page with other roles (analysts, data).
        Salary and freshness are applied to the jobs this page actually returns.
        """
        if q.get("url"):
            params = parse_qs(urlsplit(q["url"]).query)
            if q.get("keywords"):
                params["k"] = [q["keywords"]]
            for drop in ("functionAreaIdGid", "ctcFilter", "jobAge", "sort"):
                params.pop(drop, None)
            return params
        params = {"k": q["keywords"]}
        if q.get("experience") not in (None, ""):
            params["experience"] = q["experience"]
        if q.get("location"):
            params["l"] = q["location"]
        if self.cfg.get("work_mode"):
            params["wfhType"] = self.cfg["work_mode"]
        return params

    def _search_url(self, q: dict, page_no: int = 1) -> str:
        """Public results URL, the same page you get from Naukri's search box."""
        params = self._search_params(q)
        if q.get("url") and page_no == 1:
            parts = urlsplit(q["url"])
            return urlunsplit(parts._replace(query=urlencode(params, doseq=True)))
        keyword = params["k"][0] if isinstance(params.get("k"), list) else params.get("k", "")
        path = _slug(keyword) + "-jobs"
        loc = q.get("location") or ""
        if not loc and isinstance(params.get("l"), list):
            loc = params["l"][0]
        if loc:
            path += f"-in-{_slug(loc)}"
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
        """Recommended jobs from the Jobs menu, otherwise the keyword results page."""
        if query.get("recommended"):
            yield from self._search_recommended(page, query)
            return
        yield from self._search_html(page, query)

    def _fetch_api(self, page, url: str) -> dict | None:
        try:
            data = page.evaluate(
                """async (url) => {
                    const r = await fetch(url, {
                        credentials: 'include',
                        headers: {
                            'accept': 'application/json',
                            'appid': '109',
                            'systemid': 'Naukri',
                            'clientid': 'd3skt0p',
                            'gid': 'LOCATION,INDUSTRY,EDUCATION,FAREA_ROLE',
                        },
                    });
                    const text = (await r.text()).trim();
                    if (!r.ok || !text.startsWith('{')) return null;
                    try { return JSON.parse(text); } catch (e) { return null; }
                }""",
                url,
            )
        except PlaywrightError:
            return None
        return data if isinstance(data, dict) and (
            "jobDetails" in data or "noOfJobs" in data or "recommendedJobs" in data) else None

    def _goto_capture(self, page, url: str) -> dict | None:
        """Open a results URL and keep the search JSON the page itself requests.

        That payload is the list on the screen. A handmade jobapi call from the
        homepage is rejected (recaptcha) and used to fall through to the wrong cards.
        """
        found: dict = {}

        def on_response(resp):
            u = resp.url
            if "/jobapi/v3/search" not in u or "noOfResults=20" not in u or resp.status != 200:
                return
            try:
                data = resp.json()
            except (PlaywrightError, ValueError):
                return
            if isinstance(data, dict) and ("jobDetails" in data or "noOfJobs" in data):
                found["data"] = data

        page.on("response", on_response)
        try:
            page.goto(url, wait_until="domcontentloaded")
            try:
                page.wait_for_selector(SEL["card"], timeout=15000)
            except PWTimeout:
                pass
            page.wait_for_timeout(600)
        except PlaywrightError:
            return found.get("data")
        finally:
            page.remove_listener("response", on_response)
        return found.get("data")

    @staticmethod
    def _on_keyword(jobs: list[Job], keyword: str) -> bool:
        """True when the first cards are actually about the words you searched."""
        words = [w for w in re.findall(r"[a-z0-9+#.]+", (keyword or "").lower())
                 if w not in {"developer", "engineer", "software", "senior", "junior"}]
        if not words or not jobs:
            return bool(jobs)
        blob = " ".join(f"{j.title} {j.snippet} {' '.join(j.tags)}" for j in jobs[:8]).lower()
        return any(w in blob for w in words)

    def _search_recommended(self, page, query: dict) -> Iterator[Job]:
        """Jobs menu → Recommended Jobs. The homepage list Naukri builds from your profile."""
        print("\nRecommended jobs — Jobs menu")
        seen = set()
        for page_no in range(1, query["pages"] + 1):
            jobs = []
            for pattern in RECOMMENDED_APIS:
                data = self._fetch_api(page, BASE + pattern.format(page=page_no))
                jobs = jobs_from_api(data or {})
                if jobs:
                    break
            if not jobs and page_no == 1:
                print("  recommendation API empty — opening Recommended Jobs")
                yield from self._recommended_html(page, seen)
                return
            fresh = [j for j in jobs if j.id not in seen]
            for job in fresh:
                seen.add(job.id)
            print(f"  page {page_no}: {len(fresh)} recommended")
            if not fresh:
                return
            yield from fresh

    def _recommended_html(self, page, seen: set) -> Iterator[Job]:
        page.goto(REC_PAGE, wait_until="domcontentloaded")
        page.wait_for_timeout(2500)
        for _ in range(4):
            page.mouse.wheel(0, 2200)
            page.wait_for_timeout(700)
        jobs = [j for j in self._read_cards(page) if j.id not in seen]
        print(f"  page: {len(jobs)} recommended")
        yield from jobs

    def _read_cards(self, page, max_days=None) -> list[Job]:
        root = page.locator("[class*='job-listing-container']")
        cards = root.locator(SEL["card"]) if root.count() else page.locator(SEL["card"])
        if not cards.count():
            cards = page.locator(SEL["card"])
        if not cards.count():
            cards = page.locator("div.cust-job-tuple, article.jobTuple, div[data-job-id]")
        jobs = []
        seen = set()
        old = 0
        for card in cards.all():
            link = card.locator("a.title, a[href*='job-listings']")
            href = link.first.get_attribute("href") if link.count() else None
            job_id = (card.get_attribute("data-job-id") or "").strip()
            if not job_id and href:
                found = re.search(r"(\d{6,})(?:\?|$)", href)
                job_id = found.group(1) if found else ""
            if not job_id or not href or job_id in seen:
                continue
            seen.add(job_id)
            try:
                posted = card.inner_text(timeout=1000)
            except PWTimeout:
                posted = ""
            if not within_days(posted, max_days):
                old += 1
                continue
            if href.startswith("/"):
                href = BASE + href
            sal = card.locator(SEL["card_salary"])
            salary = ""
            if sal.count():
                salary = (sal.first.get_attribute("title") or "").strip() or _text(sal)
            jobs.append(Job(
                site=self.name, id=job_id, url=href.split("?")[0],
                title=_text(link),
                company=_text(card.locator(SEL["card_company"])),
                location=_text(card.locator(SEL["card_location"])),
                experience=_text(card.locator(SEL["card_exp"])),
                tags=[t.strip() for t in card.locator(SEL["card_tags"]).all_inner_texts() if t.strip()],
                snippet=_text(card.locator(SEL["card_snippet"])),
                salary=salary,
            ))
        if old:
            print(f"  {old} older than {max_days} days")
        return jobs

    def _search_html(self, page, query: dict) -> Iterator[Job]:
        base = self._search_url(query)
        max_days = query.get("job_age") or self.cfg.get("job_age_days")
        keyword = query.get("keywords") or ""
        for page_no in range(1, query["pages"] + 1):
            url = base if page_no == 1 else self._page_url(base, page_no)
            print(f"\nSearching: {url}")
            data = self._goto_capture(page, url)
            matched = jobs_from_api(data or {}, max_days)
            if self.cfg.get("sort_by_date", True):
                self._sort_by_date(page)         # reorder this same keyword list; does not change the search
            self.human.pause()
            cards = self._read_cards(page, max_days)
            if cards and self._on_keyword(cards, keyword):
                jobs = cards
                if page_no == 1 and keyword and _slug(keyword) in urlsplit(page.url).path:
                    base = page.url.split("#")[0]
            elif matched:
                if cards:
                    print("  date order left the keyword — keeping the matching jobs")
                jobs = matched
            else:
                if not cards and not page.locator(SEL["card"]).count():
                    print("  no job cards found (end of results, or selectors changed)")
                    dump_debug(page, "naukri-search")
                    return
                jobs = cards
            total = (data or {}).get("noOfJobs")
            print(f"  page {page_no}: {len(jobs)} jobs" + (f" of {total}" if total else ""))
            if not jobs:
                return
            yield from jobs  # collected first so navigating to a job doesn't detach cards

    def open_job(self, page, job: Job) -> JobDetail:
        page.goto(job.url, wait_until="domcontentloaded")
        any_btn = f"{SEL['apply_btn']}, {SEL['company_site_btn']}, {SEL['already_applied']}"
        try:
            page.wait_for_selector(any_btn, timeout=15000)
        except PWTimeout:
            dump_debug(page, "naukri-job")
        self.human.pause()
        skills = [t.strip() for t in page.locator(SEL["key_skills"]).all_inner_texts() if t.strip()]
        if skills:
            job.tags = skills
        return JobDetail(
            job=job,
            description=_text(page.locator(SEL["jd"])) + ("\nKey skills: " + ", ".join(skills) if skills else ""),
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
        """Answer the chat one question at a time. A question = a NEW bot message (by position, not text)
        that isn't an acknowledgement and whose answer controls have rendered. This keeps answers in sync
        with questions (reading too early used to send each answer to the next question)."""
        answered, idle = 0, 0                      # number of bot messages already handled
        for _ in range(40):
            got = self._wait_question(page, answered)
            if got == "APPLIED":
                return "applied"
            if got is None:
                if self._applied(page):
                    return "applied"
                if not page.locator(SEL["chat_drawer"]).first.is_visible():
                    idle += 1
                    if idle > 2:
                        dump_debug(page, "naukri-apply-unknown")
                        return "unknown"
                    continue
                dump_debug(page, "naukri-chat-stuck")
                return "failed"
            idle = 0
            count, question = got
            field = self._detect_field(page, question)
            answered = count
            if field is None:
                continue
            value = session.answer(field)
            self._fill(page, field, value)
        return "failed"

    def _has_input(self, page) -> bool:
        if (page.locator(SEL["chat_radio_label"]).filter(visible=True).count()
                or page.locator(SEL["chat_checkbox"]).filter(visible=True).count()
                or page.locator(SEL["chat_chip"]).filter(visible=True).count()):
            return True
        box = page.locator(SEL["chat_text"])
        return box.count() > 0 and box.first.is_visible()

    def _wait_question(self, page, answered: int, timeout_ms: int = 15000):
        """Wait for a new, settled bot question after the `answered` bot messages.
        Returns (bot message count, question text), 'APPLIED', or None on timeout."""
        waited = 0
        while waited < timeout_ms:
            if self._applied(page):
                return "APPLIED"
            msgs = page.locator(SEL["chat_bot_msg"])
            n = msgs.count()
            if n > answered:
                text = " ".join(msgs.nth(n - 1).inner_text().split())
                if text and not ACK_RE.match(text) and self._has_input(page):
                    page.wait_for_timeout(700)                  # let options finish rendering
                    if page.locator(SEL["chat_bot_msg"]).count() == n:
                        return n, text
                    continue
            page.wait_for_timeout(400)
            waited += 400
        return None

    @staticmethod
    def _texts(loc) -> list[str]:
        out = []
        for t in loc.filter(visible=True).all_inner_texts():
            t = " ".join(t.split())
            if t and t not in out and not CHIP_NOISE.match(t):
                out.append(t)
        return out

    def _detect_field(self, page, question: str) -> Field | None:
        radios = self._texts(page.locator(SEL["chat_radio_label"]))
        if radios:
            return Field(question, "radio", radios, required=True)
        checks = self._texts(page.locator(SEL["chat_checkbox"]))
        if checks:
            return Field(question, "checkbox", checks, required=True)
        chips = self._texts(page.locator(SEL["chat_chip"]))
        if chips:
            return Field(question, "chips", chips, required=True)
        box = page.locator(SEL["chat_text"])
        if box.count() and box.first.is_visible():
            return Field(question, "text", required=True)
        return None          # bot message with nothing to answer ("Thank you for your responses")

    def _fill(self, page, field: Field, value: str):
        self.human.pause()
        exact = re.compile(rf"^\s*{re.escape(value)}\s*$", re.I)
        if field.kind == "radio":
            lab = page.locator(SEL["chat_radio_label"]).filter(visible=True).filter(has_text=exact)
            if not lab.count():
                lab = page.locator(SEL["chat_radio_label"]).filter(visible=True).filter(has_text=value)
            lab.first.click()
        elif field.kind == "checkbox":
            for v in [x.strip() for x in value.split(",") if x.strip()]:
                page.locator(SEL["chat_checkbox"]).filter(visible=True).filter(has_text=v).first.click()
                self.human.pause()
        elif field.kind == "chips":
            chip = page.locator(SEL["chat_chip"]).filter(visible=True).filter(has_text=exact)
            if not chip.count():
                chip = page.locator(SEL["chat_chip"]).filter(visible=True).filter(has_text=value)
            box = page.locator(SEL["chat_text"])
            if chip.count():
                chip.first.click()
            elif box.count() and box.first.is_visible():   # your answer isn't a button -> type it
                self.human.type(box.first, value)
            else:
                raise PlaywrightError(f"no option {value!r} in chat")
        else:
            self.human.type(page.locator(SEL["chat_text"]).first, value)
        self.human.pause()
        send = page.locator(SEL["chat_send"]).first
        if send.count() and send.is_visible():
            send.click()
        page.wait_for_timeout(800)

    def _close_chat(self, page):
        close = page.locator(SEL["chat_close"]).first
        if close.count():
            close.click()
        else:
            page.keyboard.press("Escape")
