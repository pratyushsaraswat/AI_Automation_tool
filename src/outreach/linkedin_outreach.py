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
from rich.table import Table

from src.core.browser import dump_debug
from src.core.models import QuitRun
from src.core.review import ask_fresh, console
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

# who to contact, best first: (rank, label, pattern) — matched against the person's headline
ROLES = [
    (1, "Eng leader", r"\b(vp|vice president|head of (engineering|technology|tech|software|product)|cto|"
                      r"director[^|]{0,30}(engineering|technology|software)|engineering manager|eng(ineering)? mgr|"
                      r"\bem\b|software development manager|sdm|manager[^|,]*engineering)"),
    (2, "Recruiter/HR", r"(recruit|talent acquisition|\bta\b|\bhr\b|human resources|people partner|hiring|"
                        r"people operations|hrbp)"),
    (3, "Tech lead", r"(tech(nical)? lead|lead engineer|principal|staff engineer|architect)"),
    (4, "Engineer", r"(engineer|developer|\bsde\b|programmer)"),
]
SKIP_RE = re.compile(r"\b(intern|student|aspiring|fresher|seeking|open to work|looking for)\b", re.I)


def classify(headline: str) -> tuple[int, str]:
    if SKIP_RE.search(headline or ""):
        return 9, "skip"
    for rank, label, pat in ROLES:
        if re.search(pat, headline or "", re.I):
            return rank, label
    return 8, "other"


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
        self.auto = False          # True while sending a batch you approved once

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

    def pick_people(self, page, company: str, company_hint: str = "", want: int = 3) -> list[tuple]:
        """Find people at the company and rank them: engineering leaders, then recruiters/HR, then leads."""
        url = self.company_url(page, company, company_hint)
        if not url:
            console.print(f"[yellow]Could not find {company!r} on LinkedIn.[/]")
            return []
        console.print(f"  company page: {url}")
        people = self.find_people(page, url, company, max(want, 4))
        ranked = sorted(((classify(p.headline), p) for p in people), key=lambda x: x[0][0])
        ranked = [(r, p) for r, p in ranked if r[0] < 8]
        # mix: at least one leader and one recruiter when both exist
        pick, seen_kinds = [], set()
        for want_kind in ("Eng leader", "Recruiter/HR"):
            for r, p in ranked:
                if r[1] == want_kind and (r, p) not in pick:
                    pick.append((r, p))
                    seen_kinds.add(want_kind)
                    break
        for r, p in ranked:
            if len(pick) >= want:
                break
            if (r, p) not in pick:
                pick.append((r, p))
        return [(r[1], p) for r, p in pick[:want]]

    def run_batch(self, page, company: str, role: str, pitch: Pitch, company_hint: str = "") -> int:
        """One approval per company: show who will get the note + the note, Enter sends to all."""
        left = min(self.cfg.get("per_company", 3), self.cfg.get("daily_cap", 20) - self.tracker.outreach_today())
        if left <= 0:
            console.print("[yellow]Outreach daily cap reached (config/outreach.yaml: daily_cap).[/]")
            return 0
        chosen = self.pick_people(page, company, company_hint, left)
        if not chosen:
            dump_debug(page, "outreach-people")
            console.print("[yellow]No suitable people found.[/]")
            return 0
        while True:
            table = Table(title=f"{company} — will send connection notes to:")
            for col in ("#", "Name", "Who", "Headline"):
                table.add_column(col)
            for i, (kind, p) in enumerate(chosen, 1):
                table.add_row(str(i), p.name, kind, re.sub(r"^.*?(degree connection|·)\s*", "", p.headline)[:70])
            console.print(table)
            preview = fit_note(with_name(pitch.note, chosen[0][1].first_name))
            console.print(Panel(preview, title=f"Note ({len(preview)}/300) — name changes per person", border_style="magenta"))
            raw = ask_fresh("[bold cyan][Enter][/] send to all  [cyan]-2[/] remove #2 (or -2,4)  "
                            "[cyan]e[/] edit note  [cyan]s[/] skip company  [cyan]q[/] quit > ").strip().lower()
            raw = raw.replace("\u2212", "-").replace("\u2013", "-")     # − / – typed instead of -
            if raw == "q":
                raise QuitRun()
            if raw == "s":
                return 0
            if raw == "e":
                new = console.input("Note text (use {first_name}; keep under 300) > ").strip()
                if new:
                    pitch.note = new.replace("{first_name}", "there")
                continue
            drop = re.fullmatch(r"(?:-|remove\s*|r\s*)\s*([\d\s,-]+)", raw)
            if drop:
                nums = {int(n) for n in re.findall(r"\d+", drop.group(1))}
                bad = [n for n in nums if not 1 <= n <= len(chosen)]
                if bad:
                    console.print(f"[yellow]No person #{', #'.join(map(str, bad))} — pick 1-{len(chosen)}.[/]")
                    continue
                for n in sorted(nums, reverse=True):
                    kind, person = chosen.pop(n - 1)
                    console.print(f"  removed #{n} {person.name}")
                if not chosen:
                    console.print("  nobody left — company skipped")
                    return 0
                continue                     # show the list again and ask again
            if raw:
                console.print(f"[yellow]Didn't understand {raw!r} — nothing sent. Use Enter, -2, e, s or q.[/]")
                continue
            if raw == "":
                break
        sent = 0
        self.auto = True
        try:
            for kind, person in chosen:
                try:
                    status, how = self.contact(page, person, pitch)
                except PlaywrightError as e:
                    dump_debug(page, "outreach-error")
                    console.print(f"[red]browser error:[/] {str(e).splitlines()[0][:150]}")
                    status, how = "failed", "invite"
                self.tracker.log_outreach(company, role, person.name, person.url, how, status)
                console.print(f"  {person.name} ({kind}) → [bold]{status}[/]")
                if status.startswith("sent"):
                    sent += 1
                    self.human.between_jobs()
        finally:
            self.auto = False
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
        for _ in range(2):
            page.keyboard.press("Escape")
            page.wait_for_timeout(400)
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
        if self.auto:
            return "no_connect", "-"     # batch mode never spends InMail credits
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
    def _scopes(page):
        return [page] + [f for f in page.frames if f != page.main_frame]

    INVITE_TITLE = re.compile(r"add a note to your invitation", re.I)
    ADD_NOTE = re.compile(r"^\s*add a note\s*$", re.I)
    SEND_BTN = re.compile(r"^\s*send(\s+(now|invitation))?\s*$", re.I)
    NOTE_FIELD = "textarea, [contenteditable='true'], [role='textbox']"

    def _usable_scopes(self, page):
        """Main page, plus iframes that are actually on screen. LinkedIn's /preload/ iframe covers the
        viewport at opacity 0 and holds stray editors — never type into those."""
        scopes = [page]
        for frame in page.frames:
            if frame == page.main_frame:
                continue
            try:
                host = frame.frame_element()
                opacity = float(host.evaluate("e => parseFloat(getComputedStyle(e).opacity || '1')"))
                box = host.bounding_box()
                if opacity < 0.05 or not box or box["width"] < 40 or box["height"] < 40:
                    continue
                if host.is_visible():
                    scopes.append(frame)
            except (PlaywrightError, ValueError, TypeError):
                continue
        return scopes

    def _invite_heading(self, scope):
        return scope.get_by_text(self.INVITE_TITLE).last

    def _invite_visible(self, scope) -> bool:
        try:
            return self._invite_heading(scope).count() > 0 and self._invite_heading(scope).is_visible()
        except PlaywrightError:
            return False

    @staticmethod
    def _in_frames(page, make, timeout_ms: int = 0, last: bool = False):
        """Find a locator in the page or any iframe. make(scope) -> Locator. Polls up to timeout_ms."""
        waited = 0
        pick = (lambda loc: loc.last if last else loc.first)
        while True:
            for scope in LinkedInOutreach._scopes(page):
                try:
                    loc = make(scope)
                    if loc.count():
                        return pick(loc)
                except PlaywrightError:
                    continue
            if waited >= timeout_ms:
                return None
            page.wait_for_timeout(300)
            waited += 300

    def _invite_dialogs(self, page) -> list:
        """Visible invite modals, smallest first (the card, not a page-sized wrapper)."""
        found = []
        for scope in self._usable_scopes(page):
            try:
                loc = scope.locator("dialog, [role='dialog'], .artdeco-modal").filter(has_text=self.INVITE_TITLE)
                for i in range(min(loc.count(), 6)):
                    el = loc.nth(i)
                    box = el.bounding_box()
                    if el.is_visible() and box and box["width"] > 80 and box["height"] > 40:
                        found.append((box["width"] * box["height"], el))
            except PlaywrightError:
                continue
        found.sort(key=lambda item: item[0])
        return [el for _, el in found]

    def _note_box(self, dlg):
        """Textarea / editor inside THIS dialog. The messaging widget's editor is not one."""
        try:
            loc = dlg.locator(self.NOTE_FIELD).filter(visible=True)
            for i in range(min(loc.count(), 6)):
                el = loc.nth(i)
                box = el.bounding_box()
                if box and box["width"] > 80 and box["height"] > 28:
                    return el
        except PlaywrightError:
            return None
        return None

    def _find_invite_note(self, page, timeout_ms: int = 8000):
        """The note field after [Add a note] — only inside the invite dialog."""
        waited = 0
        while True:
            for dlg in self._invite_dialogs(page):
                box = self._note_box(dlg)
                if box:
                    return box
            if waited >= timeout_ms:
                return None
            page.wait_for_timeout(300)
            waited += 300

    def _composer_limit(self, page, default: int = NOTE_LIMIT) -> int:
        """Read the live counter ('0/200') so a Premium 200-char box is not overfilled."""
        for dlg in self._invite_dialogs(page):
            try:
                caps = [int(n) for n in re.findall(r"/\s*(\d{2,3})\b", dlg.inner_text() or "")]
            except PlaywrightError:
                continue
            caps = [n for n in caps if 50 <= n <= NOTE_LIMIT]
            if caps:
                return min(caps)
        return default

    def _invite_send_button(self, page):
        """Send inside the invite dialog. Never the messaging composer's Send."""
        for dlg in self._invite_dialogs(page):
            try:
                btn = dlg.locator("button, [role='button'], a").filter(has_text=self.SEND_BTN).filter(visible=True)
                if btn.count():
                    return btn.last
            except PlaywrightError:
                continue
        return None

    def _invite_ready(self, scope):
        if self._invite_visible(scope):
            return self._invite_heading(scope)
        wo = scope.locator("button, [role='button'], a").filter(
            has_text=re.compile(r"send without a note", re.I)).filter(visible=True)
        return wo if wo.count() else self._invite_heading(scope)

    def _click_add_note(self, page) -> bool:
        """Open the note step with a real click. LinkedIn's control is an <a href="the profile">,
        so block that navigation — otherwise the dialog is thrown away and the textarea never opens."""
        for dlg in self._invite_dialogs(page):
            try:
                dlg.evaluate("""root => {
                    const doc = root.ownerDocument;
                    if (doc.__stopInviteNav) return;
                    doc.__stopInviteNav = (ev) => {
                        const a = ev.target && ev.target.closest && ev.target.closest('a');
                        if (a && /^\\s*add a note\\s*$/i.test((a.innerText || '').trim())) ev.preventDefault();
                    };
                    doc.addEventListener('click', doc.__stopInviteNav, true);
                }""")
                link = dlg.locator("a, button, [role='button']").filter(has_text=self.ADD_NOTE).filter(visible=True)
                if link.count():
                    link.last.click(timeout=5000, no_wait_after=True)
                    return True
                point = dlg.evaluate("""root => {
                    const el = [...root.querySelectorAll('a, button, [role="button"]')]
                        .find(e => /^\\s*add a note\\s*$/i.test((e.innerText || '').trim()));
                    if (!el) return null;
                    const r = el.getBoundingClientRect();
                    return r.width ? {x: r.x + r.width / 2, y: r.y + r.height / 2} : null;
                }""")
                if not point:
                    continue
                page.mouse.click(point["x"], point["y"])
                return True
            except PlaywrightError:
                continue
        return False

    def _open_invite_note(self, page) -> bool:
        """Step 1 is [Add a note] / [Send without a note]. Step 2 is the textarea. A note field
        anywhere else on the profile (messaging, search) does not count."""
        if self._find_invite_note(page, 600):
            return True
        if not self._invite_dialogs(page) and not self._in_frames(page, self._invite_ready, 2000):
            return False
        for _ in range(3):
            if not self._click_add_note(page):
                return self._find_invite_note(page, 500) is not None
            if self._find_invite_note(page, 5000):
                return True
            page.wait_for_timeout(400)
        return self._find_invite_note(page, 800) is not None

    @staticmethod
    def _note_stuck(box, text: str) -> bool:
        try:
            got = box.evaluate("el => (el.value || el.innerText || '').replace(/\\s+/g, ' ').trim()")
        except PlaywrightError:
            return False
        snippet = " ".join(text.split())[:24]
        return bool(snippet) and snippet in got

    def _type_invite_note(self, page, box, text: str) -> bool:
        """Type the note and check it actually landed. ProseMirror ignores a plain .value set."""
        try:
            box.click(timeout=4000)
            try:
                box.fill(text, timeout=5000)
            except PlaywrightError:
                page.keyboard.press("Control+a")
                page.keyboard.insert_text(text)
        except PlaywrightError:
            pass
        if self._note_stuck(box, text):
            return True
        try:
            box.evaluate("""(el, text) => {
                el.focus();
                const area = el.tagName === 'TEXTAREA' || el.tagName === 'INPUT';
                if (area) {
                    const proto = el.tagName === 'TEXTAREA' ? HTMLTextAreaElement : HTMLInputElement;
                    const desc = Object.getOwnPropertyDescriptor(proto.prototype, 'value');
                    if (desc && desc.set) desc.set.call(el, text);
                    else el.value = text;
                } else {
                    const sel = window.getSelection();
                    const range = document.createRange();
                    range.selectNodeContents(el);
                    sel.removeAllRanges();
                    sel.addRange(range);
                    document.execCommand('insertText', false, text);
                }
                el.dispatchEvent(new InputEvent('input', {bubbles: true, data: text, inputType: 'insertText'}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""", text)
        except PlaywrightError:
            return False
        return self._note_stuck(box, text)

    def _review(self, kind: str, person: Person, text: str, subject: str = "", limit: int = 0) -> str | None:
        """Show the text; returns final text, or None to skip. Raises QuitRun on q.
        In batch mode the note was already approved once for the whole company."""
        if self.auto:
            return fit_note(text) if limit else text
        while True:
            body = (f"[dim]Subject:[/] {subject}\n\n" if subject else "") + text
            console.print(Panel(body, title=f"{kind} → {person.name}  ({len(text)} chars)", border_style="magenta"))
            raw = ask_fresh("[bold cyan][Enter][/] use this  [cyan]e[/] edit  [cyan]s[/] skip  [cyan]q[/] quit > ").strip()
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
        if not self._click(page, connect):
            dump_debug(page, "outreach-noclick")
            return "failed"
        page.wait_for_timeout(800)
        if not self._in_frames(page, self._invite_ready, 6000):
            try:
                connect.evaluate("e => (e.closest('a,button,[role=menuitem]') || e).click()")
            except PlaywrightError:
                pass
            page.wait_for_timeout(800)
            if not self._in_frames(page, self._invite_ready, 8000):
                dump_debug(page, "outreach-noinvite")
                console.print("[yellow]Invite dialog did not open.[/]")
                return "failed"

        if not self._open_invite_note(page):
            dump_debug(page, "outreach-nonote")
            console.print("[yellow]Note box did not appear.[/]")
            wo = self._in_frames(
                page, lambda sc: sc.get_by_role("button", name=re.compile(r"send without a note", re.I)).filter(visible=True),
            )
            if wo and not self.auto and self.reviewer.choose(
                    "[cyan]y[/] send without note  [cyan]s[/] skip", "ys") == "y":
                return self._send(page, wo, person)
            if wo and self.auto:
                console.print("[yellow]Batch mode: skipping — cannot send without note when note UI failed.[/]")
            page.keyboard.press("Escape")
            return "failed"

        box = self._find_invite_note(page, 2000)
        text = fit_note(text, self._composer_limit(page))
        if not box or not self._type_invite_note(page, box, text):
            dump_debug(page, "outreach-nonote")
            console.print("[yellow]Could not type into the note field.[/]")
            page.keyboard.press("Escape")
            return "failed"

        page.wait_for_timeout(500)
        snd = self._invite_send_button(page)
        if not snd:
            dump_debug(page, "outreach-nosend")
            console.print("[yellow]Send button not found in invite dialog.[/]")
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
