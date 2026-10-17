from rich.console import Console

from src.config import Settings
from src.core import llm as llm_mod
from src.core.answer_engine import AnswerEngine
from src.core.browser import open_browser
from src.core.human import Human
from src.core.matcher import Matcher
from src.core.models import QuitRun, SkipJob
from src.core.resume_parser import load_resume_text
from src.core.review import Reviewer
from src.core.session import ApplySession
from src.core.tracker import Tracker
from src.sites.naukri import NaukriAdapter

ADAPTERS = {"naukri": NaukriAdapter}
console = Console()


def login(site: str, settings: Settings):
    adapter = ADAPTERS[site](settings.search, Human(settings.search.get("delays", {})))
    with open_browser() as page:
        adapter.ensure_logged_in(page)


def run(site: str, settings: Settings, limit: int | None, dry_run: bool):
    human = Human(settings.search.get("delays", {}))
    adapter = ADAPTERS[site](settings.search, human)
    limits = settings.search.get("limits", {})
    tracker = Tracker()

    llm = llm_mod.from_env(settings.llm_provider)
    console.print(f"LLM: [bold]{f'{llm.provider} ({llm.model})' if llm else 'none — profile/cache answers only'}[/]")
    resume = load_resume_text(settings.resume_path)
    engine = AnswerEngine(settings.profile, resume, llm)
    matcher = Matcher(settings.profile, resume, llm)
    reviewer = Reviewer(settings.search.get("review", {}))

    remaining = limits.get("daily_cap", 25) - tracker.applied_today(site)
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
                    if detail.already_applied or detail.external:
                        status = "already_applied" if detail.already_applied else "external"
                        console.print(f"  [dim]{status}: {job.title} — {job.company}[/]")
                        if not dry_run:
                            tracker.log(job, status)
                        continue

                    score, reason = matcher.score(detail)
                    if score < min_score:
                        console.print(f"  [dim]low match {score}: {job.title} — {job.company} ({reason})[/]")
                        if not dry_run:
                            tracker.log(job, "low_match", score, reason)
                        continue
                    if dry_run:
                        console.print(f"  [green]would apply[/] {score}: {job.title} — {job.company}")
                        continue

                    if not reviewer.confirm_apply(detail, score, reason):
                        tracker.log(job, "skipped", score)
                        continue
                    try:
                        status = adapter.apply(page, ApplySession(detail, engine, reviewer, human))
                    except SkipJob:
                        status = "abandoned"
                    tracker.log(job, status, score)
                    console.print(f"  → [bold]{status}[/]")
                    if status == "applied":
                        applied += 1
                        if applied >= remaining:
                            console.print(f"Reached limit ({applied} applied).")
                            return
                    human.between_jobs()
        except QuitRun:
            console.print("Stopped by you.")
        finally:
            console.print(f"Applied this run: [bold]{applied}[/]")


def report():
    tracker = Tracker()
    for site, status, n in tracker.summary():
        console.print(f"{site:8} {status:16} {n}")
    console.print("\nRecent:")
    for row in tracker.recent():
        console.print("  " + " | ".join("" if v is None else str(v) for v in row))
