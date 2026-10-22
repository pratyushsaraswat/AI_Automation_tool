"""LinkedIn adapter (new 2026 jobs UI at /jobs/search-results/, with fallbacks for the classic UI).

Flow, all on the search page:
  search URL -> read job cards -> click a card -> details pane on the right ->
    * "Easy Apply": multi-step modal (rendered inside a shadow DOM) filled step by step by
      FormFiller; you see a full summary before "Submit application".
    * "Apply on company website" (a link): its URL is opened in a new tab and handed to src/ats.
  -> next card ... -> "Page 2" button -> ...

New UI class names are obfuscated, so selectors use componentkey / aria-label / role / text.
LinkedIn is strict about automation: keep daily_cap low (~15) and delays human.
"""
import re
from typing import Iterator
from urllib.parse import urlencode, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout

from src.core.browser import dump_debug
from src.core.forms import FormFiller
from src.core.models import Job, JobDetail, SkipJob
from src.core.review import console
from src.core.session import ApplySession
from src.sites.base import SiteAdapter

BASE = "https://www.linkedin.com"
DATE_POSTED = {"day": "r86400", "week": "r604800", "month": "r2592000"}

CARD_NEW = "div[role='button'][componentkey^='job-card-component-ref-']"
CARD_OLD = "[data-occludable-job-id], li[data-job-id]"
SEL = {
    "external": "a[aria-label*='company website' i]:visible, "
                "button.jobs-apply-button:visible:not(:has-text('Easy Apply'))",
    "modal": "dialog[open], [data-testid='dialog']:visible, .jobs-easy-apply-modal:visible, [role='dialog']:visible",
    "error": ".artdeco-inline-feedback--error:visible, [role='alert']:visible",
    "description": "[data-testid='expandable-text-box'], #job-details, .jobs-description__content",
}
EASY_RE = re.compile(r"^\s*Easy Apply\s*$", re.I)
NEXT_RE = re.compile(r"^(next|continue|continue to next step)$", re.I)
REVIEW_RE = re.compile(r"^review( your application)?$", re.I)
SUBMIT_RE = re.compile(r"^submit( application)?$", re.I)
APPLIED_RE = re.compile(r"Applied \d+ \w+ ago|Application submitted|You applied", re.I)
CLOSED_RE = re.compile(r"No longer accepting applications", re.I)
SENT_RE = re.compile(r"application was sent|Application sent|application was submitted", re.I)

CARDS_JS = """(sel) => [...document.querySelectorAll(sel)].map(c => {
  const key = c.getAttribute('componentkey') || '';
  const id = key.replace('job-card-component-ref-', '') || c.getAttribute('data-occludable-job-id')
             || c.getAttribute('data-job-id');
  const dismiss = c.querySelector('button[aria-label^="Dismiss"]');
  let title = dismiss ? dismiss.getAttribute('aria-label').replace(/^Dismiss\\s+/, '').replace(/\\s+job$/, '') : '';
  const ps = [...c.querySelectorAll('p, .artdeco-entity-lockup__subtitle, .artdeco-entity-lockup__caption')]
    .map(p => ((p.querySelector('span[aria-hidden="true"]') || p).innerText || '').trim()).filter(Boolean);
  if (!title) title = ps[0] || '';
  const rest = ps.filter(t => t !== title);
  const text = c.innerText || '';
  return {id, title, company: rest[0] || '', location: rest[1] || '',
          easy: /Easy Apply/.test(text), applied: /\\bApplied\\b/.test(text)};
})"""


class LinkedInAdapter(SiteAdapter):
    name = "linkedin"

    def ensure_logged_in(self, page):
        page.goto(f"{BASE}/feed/", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        while any(k in page.url for k in ("login", "checkpoint", "authwall", "signup", "uas/")):
            input("Log in to LinkedIn in the browser window (complete any OTP), then press Enter here...")
            page.goto(f"{BASE}/feed/", wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
        print("Logged in to LinkedIn.")

    # --- search ----------------------------------------------------------------

    def queries(self) -> Iterator[dict]:
        for url in self.cfg.get("search_urls") or []:
            yield {"url": url}
        for q in self.cfg.get("queries") or []:
            for loc in q.get("locations") or [""]:
                yield {"url": self._search_url(q["keywords"], loc), "keywords": q["keywords"]}

    def _search_url(self, keywords: str, location: str) -> str:
        params = {"keywords": keywords}
        if location:
            params["location"] = location
        if self.cfg.get("date_posted") in DATE_POSTED:
            params["f_TPR"] = DATE_POSTED[self.cfg["date_posted"]]
        if self.cfg.get("experience_levels"):
            params["f_E"] = ",".join(str(x) for x in self.cfg["experience_levels"])
        if self.cfg.get("easy_apply_only", True):
            params["f_AL"] = "true"
        if self.cfg.get("sort_by_date", True):
            params["sortBy"] = "DD"          # most recent first
        return f"{BASE}/jobs/search-results/?{urlencode(params)}"

    def search(self, page, query: dict) -> Iterator[Job]:
        url = re.sub(r"[?&]currentJobId=\d+", "", query["url"]).replace("search-results/&", "search-results/?")
        print(f"\nSearching: {url}")
        page.goto(url, wait_until="domcontentloaded")
        self.search_url = url
        easy_only = self.cfg.get("easy_apply_only", True)
        for page_no in range(1, self.cfg.get("pages_per_query", 1) + 1):
            if page_no > 1 and not self._goto_page(page, page_no):
                return
            try:
                page.wait_for_selector(f"{CARD_NEW}, {CARD_OLD}", timeout=15000)
            except PWTimeout:
                print("  no job cards found (end of results, or selectors changed)")
                dump_debug(page, "linkedin-search")
                return
            self._scroll_results(page)
            sel = CARD_NEW if page.locator(CARD_NEW).count() else CARD_OLD
            cards, seen = [], set()
            for c in page.evaluate(CARDS_JS, sel):
                if c["id"] and c["id"] not in seen:
                    seen.add(c["id"])
                    cards.append(c)
            print(f"  page {page_no}: {len(cards)} jobs "
                  f"({sum(c['easy'] for c in cards)} Easy Apply, {sum(c['applied'] for c in cards)} already applied)")
            for c in cards:
                if c["applied"] or (easy_only and not c["easy"]):
                    continue
                yield Job(site=self.name, id=c["id"], url=f"{BASE}/jobs/view/{c['id']}/",
                          title=c["title"], company=c["company"], location=c["location"])

    def _goto_page(self, page, page_no: int) -> bool:
        btn = page.locator(f"button[aria-label='Page {page_no}']").first
        if not btn.count():
            return False
        self.human.pause()
        btn.click()
        page.wait_for_timeout(3000)
        return True

    def _scroll_results(self, page):
        """The results list lazy-loads cards as it scrolls; scroll its container to the bottom."""
        page.evaluate("""async (sel) => {
            let el = document.querySelector(sel);
            while (el && el.scrollHeight <= el.clientHeight + 5) el = el.parentElement;
            if (!el) return;
            for (let y = 0; y <= el.scrollHeight; y += 350) {
                el.scrollTop = y;
                await new Promise(r => setTimeout(r, 200 + Math.random() * 200));
            }
            el.scrollTop = 0;
        }""", f"{CARD_NEW}, {CARD_OLD}")

    # --- job details -------------------------------------------------------------

    def _easy_btn(self, page):
        return (page.locator("button:visible, a:visible").filter(has_text=EASY_RE)
                .or_(page.locator("button[aria-label*='Easy Apply' i]:visible, a[aria-label*='Easy Apply' i]:visible"))
                .first)

    def open_job(self, page, job: Job) -> JobDetail:
        card = page.locator(f"{CARD_NEW}[componentkey='job-card-component-ref-{job.id}'], "
                            f"[data-occludable-job-id='{job.id}']").first
        if card.count():
            card.scroll_into_view_if_needed()
            self.human.pause()
            card.click()
        else:
            page.goto(f"{self.search_url}&currentJobId={job.id}", wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        easy, external = self._easy_btn(page), page.locator(SEL["external"]).first
        try:
            easy.or_(external).first.wait_for(timeout=8000)
        except PWTimeout:
            pass
        desc = page.locator(SEL["description"]).first
        description = desc.inner_text(timeout=2000).strip() if desc.count() else ""
        is_easy = easy.count() > 0
        company_link = page.locator("main a[href*='/company/']:visible").first
        return JobDetail(
            company_url=company_link.get_attribute("href") or "" if company_link.count() else "",
            job=job,
            description=description,
            external=not is_easy and external.count() > 0,
            already_applied=page.get_by_text(APPLIED_RE).count() > 0 and not is_easy,
            closed=not is_easy and not external.count() and page.get_by_text(CLOSED_RE).count() > 0,
        )

    def open_external(self, page):
        link = page.locator(SEL["external"]).first
        if not link.count():
            return None
        href = link.get_attribute("href")
        if href:                      # new UI: a plain link — open it ourselves, no popups/dialogs
            new = page.context.new_page()
            new.goto(urljoin(BASE, href), wait_until="domcontentloaded")
            return new
        self.human.pause()            # classic UI: a button that opens a new tab
        return self.click_to_new_tab(page, link, timeout=8000)

    # --- Easy Apply ----------------------------------------------------------------

    def _modal(self, page):
        steps = page.get_by_role("button", name=re.compile(
            f"{NEXT_RE.pattern}|{REVIEW_RE.pattern}|{SUBMIT_RE.pattern}", re.I))
        return page.locator(SEL["modal"]).filter(has=steps).last

    def apply(self, page, session: ApplySession) -> str:
        btn = self._easy_btn(page)
        if not btn.count():
            return "failed"
        self.human.pause()
        btn.click()
        modal = self._modal(page)
        try:
            modal.wait_for(timeout=12000)
        except PWTimeout:
            dump_debug(page, "linkedin-nomodal")
            return "failed"
        try:
            return self._easy_apply(page, modal, session)
        except SkipJob:
            self._discard(page)
            raise

    def _step_button(self, modal, pattern):
        b = modal.get_by_role("button", name=pattern).filter(visible=True)
        return b.first if b.count() else None

    def _easy_apply(self, page, modal, session: ApplySession) -> str:
        filler = FormFiller(session, upload="on_error", all_required=True)
        answered: dict[str, object] = {}     # label -> Control, across all steps (for final summary)
        had_errors, same_step_errors = False, 0
        job = session.detail.job

        for _ in range(15):
            page.wait_for_timeout(1000)
            self._unfollow(modal)            # never auto-follow the company
            for c in filler.fill(modal, force_invalid=had_errors):
                answered[c.field.label] = c

            submit = self._step_button(modal, SUBMIT_RE)
            if submit:
                status = filler.review_and_submit(
                    modal, list(answered.values()), submit,
                    success=lambda: page.get_by_text(SENT_RE).count() > 0 or not modal.is_visible(),
                    title=f"LinkedIn Easy Apply: {job.title} — {job.company}",
                    editable=False,      # earlier steps are no longer on screen
                )
                self._close_done_dialog(page)
                return status

            nxt = self._step_button(modal, REVIEW_RE) or self._step_button(modal, NEXT_RE)
            if not nxt:
                dump_debug(page, "linkedin-nostep")
                return "failed"
            self.human.pause()
            nxt.click()
            page.wait_for_timeout(1500)

            had_errors = modal.locator(SEL["error"]).count() > 0
            same_step_errors = same_step_errors + 1 if had_errors else 0
            if same_step_errors >= 2:
                dump_debug(page, "linkedin-step-errors")
                choice = session.reviewer.choose(
                    "[yellow]LinkedIn still rejects this step.[/] Fix the red fields in the browser, then "
                    "[cyan]c[/] continue  [cyan]s[/] skip job", "cs")
                if choice == "s":
                    raise SkipJob()
                same_step_errors, had_errors = 0, False
        return "failed"

    def _unfollow(self, modal):
        box = modal.get_by_label(re.compile(r"^follow", re.I))
        try:
            if box.count() and box.first.is_visible() and box.first.is_checked():
                box.first.uncheck(force=True, timeout=2000)
        except PlaywrightError:
            box.first.evaluate("e => e.click()")

    def _close_done_dialog(self, page):
        page.wait_for_timeout(1500)
        done = page.get_by_role("button", name=re.compile(r"^(dismiss|done|not now)$", re.I)).filter(visible=True)
        if done.count():
            done.first.click()

    def _discard(self, page):
        d = page.get_by_role("button", name=re.compile(r"^dismiss$", re.I)).filter(visible=True)
        if d.count():
            d.first.click()
            page.wait_for_timeout(800)
            confirm = page.get_by_role("button", name=re.compile(r"^discard$", re.I)).filter(visible=True)
            if confirm.count():
                confirm.first.click()
        console.print("  [dim]application discarded[/]")
