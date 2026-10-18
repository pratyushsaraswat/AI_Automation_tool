import hashlib
import re
from urllib.parse import urlsplit

from rich.console import Console

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
from src.sites.linkedin import LinkedInAdapter
from src.sites.naukri import NaukriAdapter

ADAPTERS = {"naukri": NaukriAdapter, "linkedin": LinkedInAdapter}
console = Console()


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
        return out.run(tab, company, role, pitch, company_hint)
    finally:
        tab.close()
        page.bring_to_front()


def outreach(settings: Settings, company: str | None, role: str, pending_days: int | None, job_url: str = "",
             people: list[str] | None = None):
    """Standalone: `outreach --company X --role Y` or `outreach --pending 7`."""
    human = Human(settings.search.get("delays", {}))
    tracker = Tracker()
    reviewer = Reviewer(settings.search.get("review", {}))
    resume = load_resume_text(settings.resume_path)
    if people:
        return _contact_people(settings, people, company or "", role, job_url, human, tracker, reviewer, resume)
    targets = ([(company, role, job_url, "")] if company
               else [(c, t, u, s) for c, t, u, s in tracker.pending_outreach(pending_days or 7)])
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
                         company_hint=hint, job_url=url, ask=bool(not company))
        except QuitRun:
            console.print("Stopped by you.")
    console.print(f"Outreach log: data/outreach.csv")


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
