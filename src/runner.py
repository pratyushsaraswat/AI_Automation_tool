import hashlib
import re
from urllib.parse import parse_qs, urlsplit

from rich.console import Console
from rich.markup import escape

from src.ats import apply_external
from src.config import Settings
from src.core import llm as llm_mod
from src.core.answer_engine import AnswerEngine
from playwright.sync_api import Error as PlaywrightError

from src.core.browser import dump_debug, open_browser
from src.core.human import Human
from src.core.matcher import Matcher
from src.core.models import Job, JobDetail, QuitRun, SkipJob
from src.core.resume_parser import load_resume_text
from src.core.review import Reviewer
from src.core.session import ApplySession
from src.core.tracker import CSV_PATH, Tracker
from src.outreach.linkedin_outreach import LinkedInOutreach
from src.outreach.message import build_pitch, write_prep
from src.outreach.skills import find_skills
from src.sites.linkedin import LinkedInAdapter
from src.sites.naukri import NaukriAdapter

ADAPTERS = {"naukri": NaukriAdapter, "linkedin": LinkedInAdapter}
console = Console()


def _defaults(site: str, cfg: dict) -> dict:
    qs = cfg.get("queries") or []
    kws = [q["keywords"] for q in qs]
    if site == "linkedin" and not kws:
        kws = [parse_qs(urlsplit(u).query).get("keywords", [""])[0] for u in cfg.get("search_urls") or []]
    locs = (qs[0].get("locations") if qs else None) or []
    days = cfg.get("job_age_days") or {"day": 1, "week": 7, "month": 30}.get(cfg.get("date_posted"), 7)
    return {"keywords": [k for k in kws if k], "exp": (qs[0].get("experience") if qs else None) or 2,
            "locations": [l for l in locs if l], "days": days, "sort": cfg.get("sort_by_date", True)}


GENERIC_ROLE_WORDS = {"development", "developer", "engineer", "software", "senior", "junior", "sr", "jr", "backend", "back",
                      "end", "frontend", "front", "full", "stack", "fullstack", "sde", "programmer", "lead",
                      "jobs", "job", "remote", "intern", "associate", "application", "web", "and", "or"}
STACK_ALIASES = {"golang": ["golang", "go"], "go": ["golang", "go"], "node": ["node", "nodejs", "node.js"],
                 "react": ["react", "reactjs", "react.js"], "dotnet": [".net", "dotnet", "c#"],
                 "net": [".net", "dotnet", "c#"], "js": ["javascript", "js"]}


def stack_terms(keywords: str) -> list[str]:
    """'golang developer' -> ['golang', 'go'];  'java full stack developer' -> ['java']."""
    if any(not [w for w in re.findall(r"[a-z0-9+#.]+", k.lower()) if w not in GENERIC_ROLE_WORDS]
           for k in keywords.split(",") if k.strip()):
        return []        # a generic role like "backend development" is in the search: don't filter by stack
    words = [w for w in re.findall(r"[a-z0-9+#.]+", keywords.lower()) if w not in GENERIC_ROLE_WORDS]
    out = []
    for w in words:
        out += STACK_ALIASES.get(w.strip("."), [w])
    return out


def on_stack(terms: list[str], text: str) -> bool:
    if not terms:
        return True
    low = text.lower()
    for t in terms:
        if t == "go":                     # plain English "go" -> use the strict Go-language matcher
            if "Go" in find_skills(text):
                return True
        elif re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9#])", low):
            return True
    return False


def choose_search(site: str, settings: Settings, keywords=None, exp=None, location=None, days=None, ask=True):
    """Ask which stack / experience / location / freshness to search (or take them from CLI flags)
    and rewrite this run's search config. Nothing is saved to search.yaml."""
    cfg = settings.search.setdefault(site, {})
    d = _defaults(site, cfg)
    if ask and keywords is None:
        console.rule(f"[bold]What should I search on {site}?[/] (Enter = keep default)")
        raw = console.input(escape(f"  Stack / role (comma for several) — or paste a {site} search link "
                                   f"[{', '.join(d['keywords'])}] > ")).strip()
        if raw.startswith("http"):
            cfg["search_urls"], cfg["queries"] = [raw], []
            raw2 = console.input(escape(f"  Posted within days (1, 3, 7, 15, 30) [{d['days']}] > ")).strip()
            days = int(raw2) if raw2.isdigit() else d["days"]
            if site == "naukri":
                cfg["job_age_days"] = days
            console.print(f"  → using your link · last {days} day(s) · newest first")
            return
        keywords = raw or ", ".join(d["keywords"])
        raw = console.input(escape(f"  Experience in years [{d['exp']}] > ")).strip()
        exp = int(raw) if raw.isdigit() else d["exp"]
        raw = console.input(escape(f"  Locations (comma; '-' = anywhere) [{', '.join(d['locations']) or 'anywhere'}] > ")).strip()
        location = "" if raw == "-" else (raw or ", ".join(d["locations"]))
        raw = console.input(escape(f"  Posted within days (1, 3, 7, 15, 30) [{d['days']}] > ")).strip()
        days = int(raw) if raw.isdigit() else d["days"]
        raw = console.input(escape(f"  Newest first (sort by date)? [{'Y/n' if d['sort'] else 'y/N'}] > ")).strip().lower()
        sort = d["sort"] if not raw else raw.startswith("y")
    else:
        sort = d["sort"]
    kws = [k.strip() for k in (keywords if keywords is not None else ", ".join(d["keywords"])).split(",") if k.strip()]
    exp = d["exp"] if exp is None else exp
    locs = [l.strip() for l in (location if location is not None else ", ".join(d["locations"])).split(",") if l.strip()]
    days = d["days"] if days is None else days

    cfg["queries"] = [{"keywords": k, "locations": locs or [""], "experience": exp} for k in kws]
    cfg["search_urls"] = []
    cfg["sort_by_date"] = sort
    if site == "naukri":
        cfg["job_age_days"] = days
    else:
        cfg["date_posted"] = "day" if days <= 1 else "week" if days <= 7 else "month"
        cfg["experience_levels"] = [1, 2] if exp < 2 else [2, 3] if exp < 5 else [3, 4] if exp < 10 else [4, 5]
    console.print(f"  → {', '.join(kws)} · {exp} yrs · {', '.join(locs) or 'anywhere'} · last {days} day(s)"
                  f"{' · newest first' if sort else ''}")


def login(site: str, settings: Settings):
    adapter = ADAPTERS[site](settings.search, Human(settings.search.get("delays", {})))
    with open_browser() as page:
        adapter.ensure_logged_in(page)


def _daily_cap(limits: dict, site: str) -> int:
    return (limits.get("daily_cap_by_site") or {}).get(site, limits.get("daily_cap", 25))


def run(site: str, settings: Settings, limit: int | None, dry_run: bool):
    human = Human(settings.search.get("delays", {}))
    adapter = ADAPTERS[site](settings.search, human)
    limits = settings.search.get("limits", {})
    external_on = (settings.search.get("external") or {}).get("enabled", True)
    tracker = Tracker()

    llm = llm_mod.from_env(settings.llm_provider)
    console.print(f"LLM: [bold]{f'{llm.provider} ({llm.model})' if llm else 'none — profile/cache answers only'}[/]")
    resume = load_resume_text(settings.resume_path)
    engine = AnswerEngine(settings.profile, resume, llm)
    matcher = Matcher(settings.profile, resume, llm)
    reviewer = Reviewer(settings.search.get("review", {}))

    remaining = _daily_cap(limits, site) - tracker.applied_today(site)
    if limit:
        remaining = min(remaining, limit)
    if remaining <= 0 and not dry_run:
        console.print("Daily cap reached. Come back tomorrow.")
        return
    min_score = limits.get("min_match_score", 0)
    applied = 0

    with open_browser() as page:
        adapter.ensure_logged_in(page)
        try:
            for query in adapter.queries():
                terms = stack_terms(query.get("keywords", ""))
                for job in adapter.search(page, query):
                    if tracker.seen(job):
                        continue
                    detail = adapter.open_job(page, job)
                    skip = ("already_applied" if detail.already_applied else "closed" if detail.closed
                            else "external" if detail.external and not external_on else None)
                    if skip:
                        console.print(f"  [dim]{skip}: {job.title} — {job.company}[/]")
                        if not dry_run:
                            tracker.log(job, skip, apply_via="external" if skip == "external" else "")
                        continue

                    if not on_stack(terms, f"{job.title}\n{detail.description}"):
                        console.print(f"  [dim]off stack ({'/'.join(terms)} not mentioned): {job.title} — {job.company}[/]")
                        if not dry_run:
                            tracker.log(job, "off_stack", note=f"wanted {'/'.join(terms)}")
                        continue
                    score, reason = matcher.score(detail)
                    kind = "external" if detail.external else site
                    if score < min_score:
                        console.print(f"  [dim]low match {score}: {job.title} — {job.company} ({reason})[/]")
                        if not dry_run:
                            tracker.log(job, "low_match", score, reason)
                        continue
                    if dry_run:
                        console.print(f"  [green]would apply[/] {score} [{kind}]: {job.title} — {job.company}")
                        continue

                    if not reviewer.confirm_apply(detail, score, reason):
                        tracker.log(job, "skipped", score)
                        continue

                    session = ApplySession(detail, engine, reviewer, human, settings.resume_path)
                    status, via, ext_url = _apply_one(adapter, page, session, site)
                    tracker.log(job, status, score, apply_via=via, external_url=ext_url)
                    console.print(f"  → [bold]{status}[/] via {via}")
                    if status.startswith("applied") and settings.outreach.get("ask_after_apply"):
                        referral(page, settings, job.company, job.title, detail.description, resume, reviewer,
                                 human, tracker, company_hint=detail.company_url, job_url=job.url)
                    if status.startswith("applied"):
                        applied += 1
                        if applied >= remaining:
                            console.print(f"Reached limit ({applied} applied).")
                            return
                    human.between_jobs()
        except QuitRun:
            console.print("Stopped by you.")
        finally:
            console.print(f"Applied this run: [bold]{applied}[/]   History CSV: {CSV_PATH}")


def referral(page, settings: Settings, company: str, role: str, job_text: str, resume: str,
             reviewer: Reviewer, human: Human, tracker: Tracker, company_hint: str = "", job_url: str = "",
             ask: bool = True) -> int:
    """Write prep notes, then (after your OK) message people at the company on LinkedIn."""
    cfg = settings.outreach
    if not cfg.get("enabled"):
        return 0
    pitch = build_pitch(cfg, role, company, job_text, resume)
    prep = write_prep(pitch, role, company, job_text, job_url)
    console.print(f"  prep notes: {prep}   [dim]skills used: {', '.join(pitch.matched)}[/]")
    if ask:
        if reviewer.choose(f"Message people at [bold]{company}[/] for a referral? [cyan]y[/]/[cyan]n[/]", "yn") == "n":
            return 0
    if company_hint and "/company/" not in company_hint:
        company_hint = ""
    if not company_hint:
        typed = console.input(f"Company name to search on LinkedIn [{company}] > ").strip()
        company = typed or company
    me = re.search(r"/in/([^/?#]+)", settings.profile.get("personal", {}).get("linkedin_url", "") or "")
    out = LinkedInOutreach(cfg, reviewer, human, tracker, me.group(1) if me else "")
    tab = page.context.new_page()
    try:
        return out.run_batch(tab, company, role, pitch, company_hint)
    finally:
        tab.close()
        page.bring_to_front()


def outreach(settings: Settings, companies: list[str] | None, role: str, pending_days: int | None,
             job_url: str = "", people: list[str] | None = None, job_id: str = "", targets_file: str = ""):
    """Standalone: `outreach --company A B C [--role R]`, `outreach --pending 7`, `outreach --person URL`."""
    company = companies[0] if companies else None
    human = Human(settings.search.get("delays", {}))
    tracker = Tracker()
    reviewer = Reviewer(settings.search.get("review", {}))
    resume = load_resume_text(settings.resume_path)
    if people:
        return _contact_people(settings, people, company or "", role, job_url, human, tracker, reviewer, resume)
    default_role = role or settings.outreach.get("default_role", "Software Engineer")
    if companies:
        targets = [{"company": c, "job_id": job_id, "job_url": job_url, "role": default_role} for c in companies]
        return _contact_companies(settings, targets, human, tracker, reviewer, resume)
    if targets_file:
        return _contact_companies(settings, parse_targets_file(targets_file, default_role), human, tracker,
                                  reviewer, resume)
    if pending_days is None:
        targets = ask_targets(default_role)
        if targets:
            _contact_companies(settings, targets, human, tracker, reviewer, resume)
        return
    targets = [(c, t, u, s) for c, t, u, s in tracker.pending_outreach(pending_days or 7)]
    if not targets:
        console.print("Nothing to do: no applied jobs without outreach in that window.")
        return
    with open_browser() as page:
        LinkedInAdapter(settings.search, human).ensure_logged_in(page)
        try:
            for comp, title, url, site in targets:
                console.rule(f"[bold]{comp}[/] — {title}")
                job_text, hint = "", ""
                if url:
                    page.goto(url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    job_text = page.locator("body").inner_text()[:8000]
                    link = page.locator("main a[href*='/company/']:visible").first
                    hint = (link.get_attribute("href") or "") if site == "linkedin" and link.count() else ""
                referral(page, settings, comp, title, job_text, resume, reviewer, human, tracker,
                         company_hint=hint, job_url=url, ask=True)
        except QuitRun:
            console.print("Stopped by you.")
    console.print(f"Outreach log: data/outreach.csv")


def ask_targets(default_role: str) -> list[dict]:
    """Interactive: company, job ID, job link, role — repeat until Enter on company."""
    console.rule("[bold]Referral outreach[/] — Enter on 'Company name' to start sending")
    targets = []
    while True:
        company = console.input(f"\n[bold]Company name[/] {'(Enter = start)' if targets else ''} > ").strip()
        if not company:
            break
        job_id = console.input("  Job ID (Enter if none) > ").strip()
        job_url = console.input("  Job link (Enter if none) > ").strip()
        role = console.input(f"  Role [{default_role}] > ").strip() or default_role
        targets.append({"company": company, "job_id": job_id, "job_url": job_url, "role": role})
        console.print(f"  [green]added[/] {company} · {role}" + (f" · Job ID {job_id}" if job_id else "")
                      + (" · link" if job_url else ""))
    return targets


def parse_targets_file(path: str, default_role: str) -> list[dict]:
    """companies.txt — one per line:  Company | job id | job link | role   (only Company is required)."""
    out = []
    for line in open(path):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        parts = [x.strip() for x in line.split("|")] + ["", "", ""]
        out.append({"company": parts[0], "job_id": parts[1], "job_url": parts[2], "role": parts[3] or default_role})
    return out


def _contact_companies(settings, targets: list[dict], human, tracker, reviewer, resume):
    """For each target find eng leaders + recruiters/HR, approve once per company, send all."""
    cfg = settings.outreach
    me = re.search(r"/in/([^/?#]+)", settings.profile.get("personal", {}).get("linkedin_url", "") or "")
    out = LinkedInOutreach(cfg, reviewer, human, tracker, me.group(1) if me else "")
    total = 0
    with open_browser() as page:
        LinkedInAdapter(settings.search, human).ensure_logged_in(page)
        try:
            for i, t in enumerate(targets, 1):
                comp, role, job_id, job_url = t["company"], t["role"], t.get("job_id", ""), t.get("job_url", "")
                console.rule(f"[bold]{i}/{len(targets)} · {comp}[/] — {role}" + (f" (Job ID {job_id})" if job_id else ""))
                job_text, hint = "", ""
                if job_url:
                    page.goto(job_url, wait_until="domcontentloaded")
                    page.wait_for_timeout(3000)
                    job_text = page.locator("body").inner_text()[:8000]
                    link = page.locator("main a[href*='/company/']:visible").first
                    hint = (link.get_attribute("href") or "") if "linkedin.com" in job_url and link.count() else ""
                applied = bool(job_id or job_url)
                pitch = build_pitch(cfg, role, comp, job_text, resume, general=not applied, job_id=job_id)
                if job_text:
                    console.print(f"  prep notes: {write_prep(pitch, role, comp, job_text, job_url)}")
                total += out.run_batch(page, comp, role, pitch, hint)
                if out.tracker.outreach_today() >= cfg.get("daily_cap", 20):
                    console.print("[yellow]Daily cap reached — stopping.[/]")
                    break
        except QuitRun:
            console.print("Stopped by you.")
    console.print(f"Sent {total} connection notes. Log: data/outreach.csv")


def _contact_people(settings, urls, company, role, job_url, human, tracker, reviewer, resume):
    """outreach --person URL ...: skip the search, contact these profiles with a JD-tailored note."""
    from src.outreach.linkedin_outreach import Person
    with open_browser() as page:
        LinkedInAdapter(settings.search, human).ensure_logged_in(page)
        job_text = ""
        if job_url:
            page.goto(job_url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            job_text = page.locator("body").inner_text()[:8000]
        pitch = build_pitch(settings.outreach, role, company, job_text, resume)
        console.print(f"  prep notes: {write_prep(pitch, role, company, job_text, job_url)}")
        out = LinkedInOutreach(settings.outreach, reviewer, human, tracker)
        try:
            for url in urls:
                url = url.split("?")[0]
                page.goto(url, wait_until="domcontentloaded")
                page.wait_for_timeout(2500)
                name = page.locator("main h1").first.inner_text().strip() if page.locator("main h1").count() else ""
                try:
                    status, kind = out.contact(page, Person(name, url, ""), pitch)
                except PlaywrightError as e:
                    dump_debug(page, "outreach-error")
                    console.print(f"[red]browser error:[/] {str(e).splitlines()[0][:150]}")
                    status, kind = "failed", "invite"
                tracker.log_outreach(company, role, name, url, kind, status)
                console.print(f"  {name or url} → [bold]{status}[/] ({kind})")
        except QuitRun:
            console.print("Stopped by you.")


def _apply_one(adapter, page, session: ApplySession, site: str) -> tuple[str, str, str]:
    """Returns (status, apply_via, external_url)."""
    if not session.detail.external:
        try:
            return adapter.apply(page, session), f"{site}_easy" if site == "linkedin" else site, ""
        except SkipJob:
            return "abandoned", site, ""

    ext = adapter.open_external(page)
    if ext is None:
        return "failed", "external", ""
    ext_url = ext.url
    try:
        status, via = apply_external(ext, session)
        ext_url = ext.url
    except SkipJob:
        status, via = "abandoned", "external"
    finally:
        if ext is not page:
            ext.close()
            page.bring_to_front()
    return status, via, ext_url


def apply_links(urls: list[str], settings: Settings):
    """Apply to job links you give (Workday, Greenhouse, Lever, any careers page)."""
    human = Human(settings.search.get("delays", {}))
    tracker = Tracker()
    llm = llm_mod.from_env(settings.llm_provider)
    console.print(f"LLM: [bold]{f'{llm.provider} ({llm.model})' if llm else 'none — profile/cache answers only'}[/]")
    engine = AnswerEngine(settings.profile, load_resume_text(settings.resume_path), llm)
    reviewer = Reviewer(settings.search.get("review", {}))

    with open_browser() as page:
        for i, url in enumerate(urls, 1):
            console.rule(f"[bold]Link {i}/{len(urls)}[/]")
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            header = page.locator("[data-automation-id='jobPostingHeader'], h1").filter(visible=True)
            host = urlsplit(url).hostname or ""
            job = Job(site="direct", id=hashlib.md5(url.encode()).hexdigest()[:12], url=url,
                      title=(header.first.inner_text().strip() if header.count() else page.title())[:120],
                      company=host.split(".")[0])
            detail = JobDetail(job, description=page.locator("body").inner_text()[:6000], external=True)
            session = ApplySession(detail, engine, reviewer, human, settings.resume_path)
            try:
                status, via = apply_external(page, session)
            except SkipJob:
                status, via = "abandoned", "direct"
            except QuitRun:
                console.print("Stopped by you.")
                break
            tracker.log(job, status, apply_via=via, external_url=page.url)
            console.print(f"  → [bold]{status}[/] via {via}")
            if status.startswith("applied") and settings.outreach.get("ask_after_apply"):
                try:
                    referral(page, settings, job.company, job.title, detail.description, engine.resume_text,
                             reviewer, human, tracker, job_url=url)
                except QuitRun:
                    break
    console.print(f"History CSV: {CSV_PATH}")


def report():
    tracker = Tracker()
    for site, status, n in tracker.summary():
        console.print(f"{site:9} {status:16} {n}")
    console.print("\nRecent:")
    for row in tracker.recent():
        console.print("  " + " | ".join("" if v is None else str(v) for v in row))


def export(applied_only: bool):
    n = Tracker().export_csv(applied_only=applied_only)
    console.print(f"Wrote {n} rows to {CSV_PATH}")
