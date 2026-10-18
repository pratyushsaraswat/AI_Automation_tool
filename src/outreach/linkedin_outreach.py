"""Find people at a company on LinkedIn and send them a referral message — after YOUR OK.

company page -> People tab filtered by keyword (recruiter, ...) -> profile ->
  "Message" available (1st-degree / open profile / InMail)  -> subject + message
  else "Connect" (or More -> Connect) -> "Add a note"        -> note (<= 300 chars)
Every message is shown to you first; mode: draft lets you press Send yourself.
"""
import re
from dataclasses import dataclass
from urllib.parse import quote, urljoin

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout
from rich.panel import Panel

from src.core.browser import dump_debug
from src.core.models import QuitRun
from src.core.review import console
from src.core.tracker import Tracker
from src.outreach.message import NOTE_LIMIT, Pitch, fit_note, with_name

def _company_token(company: str) -> str:
    """'Artech L.L.C.' -> 'artech', 'Scoutit' -> 'scoutit'."""
    words = [w for w in re.findall(r"[a-z0-9]+", company.lower()) if len(w) > 2 and w not in {"the", "pvt", "ltd", "inc", "llc"}]
    return words[0] if words else company.lower()


BASE = "https://www.linkedin.com"
PEOPLE_JS = r"""(me) => {
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/in/"]')) {
    const m = a.href.match(/linkedin\.com\/in\/([^/?#]+)/);
    if (!m || seen.has(m[1]) || m[1] === me) continue;
    const name = (a.innerText || '').trim().split('\n')[0].trim();
    if (!name || /linkedin member/i.test(name) || name.length > 60) continue;
    seen.add(m[1]);
    const card = a.closest('li') || a.parentElement?.parentElement?.parentElement;
    out.push({slug: m[1], url: 'https://www.linkedin.com/in/' + m[1] + '/', name,
              text: (card?.innerText || '').replace(/\s+/g, ' ').trim().slice(0, 200)});
  }
  return out;
}"""
BTN = "button:visible, a:visible, [role='button']:visible"


@dataclass
class Person:
    name: str
    url: str
    headline: str

    @property
    def first_name(self) -> str:
        return self.name.split()[0].title() if self.name else "there"


class LinkedInOutreach:
    def __init__(self, cfg: dict, reviewer, human, tracker: Tracker, my_slug: str = ""):
        self.cfg = cfg
        self.reviewer = reviewer
        self.human = human
        self.tracker = tracker
        self.my_slug = my_slug

    # --- company + people ----------------------------------------------------------

    def company_url(self, page, company: str, hint: str = "") -> str | None:
        if hint and "/company/" in hint:
            return re.sub(r"(/company/[^/?#]+).*", r"\1/", urljoin(BASE, hint))
        page.goto(f"{BASE}/search/results/companies/?keywords={quote(company)}", wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        links = page.locator("main a[href*='/company/']")
        for i in range(min(links.count(), 10)):
            href = links.nth(i).get_attribute("href") or ""
            if re.search(r"/company/[^/?#]+", href):
                return re.sub(r"(/company/[^/?#]+).*", r"\1/", urljoin(BASE, href))
        dump_debug(page, "outreach-company")
        return None

    def find_people(self, page, company_url: str, company: str, limit: int) -> list[Person]:
        """Company People tab (current employees) first; global search only as fallback, and then
        only people whose card mentions the company (avoids 'Scoutit' -> football scouts)."""
        people: list[Person] = []
        token = _company_token(company)

        def add(found, must_mention: bool):
            for p in found:
                if must_mention and token not in p["text"].lower():
                    continue
                if any(x.url == p["url"] for x in people) or self.tracker.contacted(p["url"]):
                    continue
                people.append(Person(p["name"], p["url"], p["text"]))

        for kw in (self.cfg.get("search_keywords") or ["recruiter"]) + [""]:   # "" = all employees
            page.goto(f"{company_url}people/" + (f"?keywords={quote(kw)}" if kw else ""), wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            page.mouse.wheel(0, 1500)
            page.wait_for_timeout(1500)
            add(page.evaluate(PEOPLE_JS, self.my_slug), must_mention=False)
            if len(people) >= limit * 3:
                return people
        for kw in self.cfg.get("search_keywords") or ["recruiter"]:
            page.goto(f"{BASE}/search/results/people/?keywords={quote(company + ' ' + kw)}", wait_until="domcontentloaded")
            page.wait_for_timeout(3500)
            add(page.evaluate(PEOPLE_JS, self.my_slug), must_mention=True)
            if len(people) >= limit * 3:
                break
        return people

    # --- one company -----------------------------------------------------------------

    def run(self, page, company: str, role: str, pitch: Pitch, company_hint: str = "") -> int:
        left = min(self.cfg.get("per_company", 2), self.cfg.get("daily_cap", 10) - self.tracker.outreach_today())
        if left <= 0:
            console.print("[yellow]Outreach daily cap reached.[/]")
            return 0
        url = self.company_url(page, company, company_hint)
        if not url:
            console.print(f"[yellow]Could not find {company!r} on LinkedIn.[/]")
            return 0
        console.print(f"  company page: {url}")
        people = self.find_people(page, url, company, left)
        if not people:
            dump_debug(page, "outreach-people")
            console.print("[yellow]No reachable people found.[/]")
            return 0

        sent = 0
        for person in people:
            if sent >= left:
                break
            console.print(Panel(f"[bold]{person.name}[/]\n{person.headline}\n{person.url}", title="Person",
                                border_style="cyan"))
            choice = self.reviewer.choose("[cyan]c[/] contact  [cyan]n[/] next person  [cyan]x[/] done with company", "cnx")
            if choice == "x":
                break
            if choice == "n":
                continue
            try:
                status, kind = self.contact(page, person, pitch)
            except PlaywrightError as e:
                dump_debug(page, "outreach-error")
                console.print(f"[red]browser error:[/] {str(e).splitlines()[0][:150]}")
                status, kind = "failed", "invite"
            self.tracker.log_outreach(company, role, person.name, person.url, kind, status)
            console.print(f"  → [bold]{status}[/] ({kind})")
            if status.startswith("sent"):
                sent += 1
                self.human.between_jobs()
        return sent

    # --- one person --------------------------------------------------------------------

    def _top_card(self, page, name_hint: str = ""):
        """The profile's own header card — never the sidebar / 'People similar'.
        Name: <h1>, else page title 'Name | LinkedIn', else the name from the people list."""
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(500)
        h1 = page.locator("main h1").first
        name = h1.inner_text().strip() if h1.count() else ""
        if not name:
            title = page.title().split("|")[0].strip()
            name = title if title and "linkedin" not in title.lower() else name_hint
        name = re.sub(r"\s*\(.*?\)|\s+[•·].*$", "", name).strip()
        card = page.locator("main section").filter(has_text=name).first if name else page.locator("main section").first
        return (card if card.count() else page.locator("main").first), name

    def contact(self, page, person: Person, pitch: Pitch) -> tuple[str, str]:
        page.goto(person.url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        top, name = self._top_card(page, person.name)
        if name:
            person.name = name
        if self._is_pending(page, person):
            return "already_pending", "invite"
        msg_btn = top.locator(BTN).filter(has_text=re.compile(r"^\s*Message\s*$")).first
        if self.cfg.get("prefer", "connect") == "message" and msg_btn.count():
            return self._message(page, msg_btn, person, pitch), "message"
        connect = self._find_connect(page, top, person)
        if connect:
            return self._invite(page, connect, person, pitch), "invite"
        dump_debug(page, "outreach-noconnect")
        if msg_btn.count() and self.reviewer.choose(
                "[yellow]No Connect option found.[/] Send a Message instead (may use a Premium InMail credit)? "
                "[cyan]y[/]/[cyan]n[/]", "yn") == "y":
            return self._message(page, msg_btn, person, pitch), "message"
        return "no_action", "-"

    @staticmethod
    def _slug(person: Person) -> str:
        m = re.search(r"/in/([^/?#]+)", person.url)
        return m.group(1).lower() if m else ""

    def _is_mine(self, label: str, href: str, person: Person) -> bool:
        """A control belongs to this person only if its label names them or its link has their slug."""
        label, href = (label or "").lower(), (href or "").lower()
        slug = self._slug(person)
        if slug and slug in href:
            return True
        parts = [w for w in re.findall(r"[a-z]+", person.name.lower()) if len(w) > 1]
        return bool(parts) and all(w in label for w in parts[:2])

    def _mine(self, page, selector: str, person: Person):
        """First visible element matching selector that belongs to this person (checks own label/href and
        its child's label: <a href=custom-invite><div aria-label="Invite X to connect">)."""
        cands = page.locator(selector).filter(visible=True)
        for i in range(min(cands.count(), 40)):
            el = cands.nth(i)
            try:
                label, href = el.evaluate("""e => { const c = e.querySelector('[aria-label]');
                    return [(e.getAttribute('aria-label') || '') + ' ' + (c ? c.getAttribute('aria-label') : ''),
                            e.getAttribute('href') || '']; }""")
            except PlaywrightError:
                continue
            if self._is_mine(label, href, person):
                return el
        return None

    CONNECT_SEL = ("[aria-label*='to connect' i], a[href*='custom-invite'], "
                   "[role='menuitem']:has([aria-label*='to connect' i])")

    def _is_pending(self, page, person: Person) -> bool:
        return self._mine(page, "[aria-label*='Pending' i][aria-label*='withdraw' i]", person) is not None

    def _find_connect(self, page, top, person: Person):
        """This person's Connect control — a direct button, or the item in the '…' menu.
        NEVER a bare 'Connect' text: the sidebar / 'People similar' are full of other people's buttons."""
        el = self._mine(top, self.CONNECT_SEL, person) or self._mine(page, self.CONNECT_SEL, person)
        if el:
            return el                           # Connect next to Message
        for more in self._more_buttons(top):    # Connect inside "…"
            if not self._click(page, more):
                continue
            page.wait_for_timeout(1200)
            el = self._mine(page, self.CONNECT_SEL, person)
            if el:
                return el
            page.keyboard.press("Escape")       # wrong menu / no Connect in it: close, try next
            page.wait_for_timeout(400)
        return None

    LIVE_JS = """e => { const r = e.getBoundingClientRect();
        return !e.closest('[inert], [aria-hidden="true"]') && r.width > 0 && r.height > 0; }"""

    def _more_buttons(self, top) -> list:
        """'…' buttons in the profile card that are really clickable. LinkedIn also renders a hidden,
        inert sticky-header copy with the same aria-label — clicking that does nothing."""
        cands = top.locator("button[aria-label='More' i], button[aria-label*='More actions' i], "
                            "button[aria-label^='More' i]").filter(visible=True)
        live = []
        for i in range(cands.count()):
            try:
                if cands.nth(i).evaluate(self.LIVE_JS):
                    live.append(cands.nth(i))
            except PlaywrightError:
                continue
        return live

    def _more_button(self, top):
        live = self._more_buttons(top)
        return live[0] if live else None

    @staticmethod
    def _click(page, el) -> bool:
        """Click a verified element. LinkedIn overlays (toasts, upsells, the interop layer) sometimes sit
        on top and swallow the pointer: close them with Escape and retry, then fall back to a DOM click
        on the SAME element (it was already verified to belong to this person)."""
        for _ in range(2):
            try:
                el.click(timeout=5000)
                return True
            except PlaywrightError:
                page.keyboard.press("Escape")
                page.wait_for_timeout(700)
        try:
            el.evaluate("e => e.click()")
            return True
        except PlaywrightError:
            return False

    @staticmethod
    def _in_frames(page, make, timeout_ms: int = 0):
        """Find a locator in the page or any iframe (LinkedIn renders the invite in /preload/ iframe or
        shadow DOM). make(scope) -> Locator. Polls up to timeout_ms."""
        waited = 0
        while True:
            for scope in [page] + [f for f in page.frames if f != page.main_frame]:
                try:
                    loc = make(scope)
                    if loc.count():
                        return loc.first
                except PlaywrightError:
                    continue
            if waited >= timeout_ms:
                return None
            page.wait_for_timeout(300)
            waited += 300

    def _review(self, kind: str, person: Person, text: str, subject: str = "", limit: int = 0) -> str | None:
        """Show the text; returns final text, or None to skip. Raises QuitRun on q."""
        while True:
            body = (f"[dim]Subject:[/] {subject}\n\n" if subject else "") + text
            console.print(Panel(body, title=f"{kind} → {person.name}  ({len(text)} chars)", border_style="magenta"))
            raw = console.input("[bold cyan][Enter][/] use this  [cyan]e[/] edit  [cyan]s[/] skip  [cyan]q[/] quit > ").strip()
            if raw.lower() == "q":
                raise QuitRun()
            if raw.lower() == "s":
                return None
            if raw.lower() == "e":
                new = console.input("New text (one line) > ").strip()
                text = new or text
                continue
            if limit and len(text) > limit:
                console.print(f"[red]{len(text)} characters — LinkedIn allows {limit}. Edit it (e) to shorten.[/]")
                continue
            return text

    def _send(self, page, send_btn, person: Person | None = None) -> str:
        if self.cfg.get("mode") == "draft":
            done = self.reviewer.choose("Filled. Press [bold]Send[/] in the browser yourself, then "
                                        "[cyan]d[/] sent  [cyan]s[/] not sent", "ds")
            return "sent" if done == "d" else "skipped"
        try:
            send_btn.wait_for(state="visible", timeout=3000)
            for _ in range(10):
                if send_btn.is_enabled():
                    break
                page.wait_for_timeout(300)
        except (PWTimeout, PlaywrightError):
            pass
        if not self._click(page, send_btn):
            return "failed"
        page.wait_for_timeout(2500)
        if person is None:
            return "sent"
        sent_toast = page.get_by_text(re.compile(r"invitation sent|your invitation to .* was sent", re.I)).count()
        return "sent" if sent_toast or self._is_pending(page, person) else "sent?"

    def _message(self, page, msg_btn, person: Person, pitch: Pitch) -> str:
        text = self._review("Message", person, with_name(pitch.message, person.first_name), pitch.subject)
        if text is None:
            return "skipped"
        msg_btn.click()
        page.wait_for_timeout(2500)
        box = page.locator("div[role='textbox'][contenteditable='true']:visible").last
        try:
            box.wait_for(timeout=8000)
        except PWTimeout:
            dump_debug(page, "outreach-nocomposer")
            return "failed"
        subject = page.locator("input[name='subject']:visible, input[placeholder*='Subject' i]:visible")
        if subject.count() and pitch.subject:
            subject.first.fill(pitch.subject)
        box.click()
        box.fill(text)
        send = page.get_by_role("button", name=re.compile(r"^send$", re.I)).filter(visible=True)
        if not send.count():
            dump_debug(page, "outreach-nosend")
            return "failed"
        status = self._send(page, send.last)
        self._close_composer(page)
        return status

    def _invite(self, page, connect, person: Person, pitch: Pitch) -> str:
        text = self._review("Connection note", person, fit_note(with_name(pitch.note, person.first_name)), limit=NOTE_LIMIT)
        if text is None:
            page.keyboard.press("Escape")
            return "skipped"
        add_note = lambda sc: sc.get_by_role("button", name=re.compile(r"^\s*add a note\s*$", re.I)).filter(visible=True)
        note_box = lambda sc: sc.locator("textarea[name='message'], textarea#custom-message, "
                                         "textarea[placeholder*='know each other' i], textarea").filter(visible=True)
        without = lambda sc: sc.get_by_role("button", name=re.compile(r"send without a note", re.I)).filter(visible=True)
        send = lambda sc: sc.get_by_role("button", name=re.compile(r"^\s*send( invitation| now)?\s*$", re.I)).filter(visible=True)
        ready = lambda sc: add_note(sc).or_(note_box(sc)).or_(without(sc))

        if not self._click(page, connect):
            dump_debug(page, "outreach-noclick")
            return "failed"
        if not self._in_frames(page, ready, 5000):
            try:
                connect.evaluate("e => (e.closest('a,button,[role=menuitem]') || e).click()")
            except PlaywrightError:
                pass
            if not self._in_frames(page, ready, 6000):
                dump_debug(page, "outreach-noinvite")
                console.print("[yellow]Invite dialog did not open.[/]")
                return "failed"

        # step 1: "Add a note to your invitation?"  [Add a note] [Send without a note]
        btn = self._in_frames(page, add_note)
        if btn:
            self._click(page, btn)
        box = self._in_frames(page, note_box, 6000)
        if not box:
            dump_debug(page, "outreach-nonote")
            console.print("[yellow]Note box did not appear.[/]")
            wo = self._in_frames(page, without)
            if wo and self.reviewer.choose("[cyan]y[/] send without note  [cyan]s[/] skip", "ys") == "y":
                return self._send(page, wo, person)
            page.keyboard.press("Escape")
            return "failed"

        # step 2: note box (0/300) -> [Send]
        box.click()
        box.fill(text)
        page.wait_for_timeout(600)
        snd = self._in_frames(page, send, 3000)
        if not snd:
            dump_debug(page, "outreach-nosend")
            page.keyboard.press("Escape")
            return "failed"
        return self._send(page, snd, person)

    def _close_composer(self, page):
        try:
            close = page.locator("button[aria-label*='Close your' i]:visible, button[aria-label*='close' i]:visible")
            if close.count():
                close.last.click()
        except PlaywrightError:
            pass
