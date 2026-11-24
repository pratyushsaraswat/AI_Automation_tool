import hashlib
from datetime import datetime
import re
from collections import Counter
from urllib.parse import parse_qs, urlsplit

from rich.console import Console
from rich.markup import escape

from src.ats import apply_external, detect
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
from src.sites.mycareernet import MyCareernetAdapter, is_anonymous_company
from src.sites.naukri import NaukriAdapter, salary_ceiling_lpa
from src.sites.randstad import RandstadAdapter
from src.sites.teamlease import TeamLeaseAdapter
from src.sites.wellfound import WellfoundAdapter

ADAPTERS = {"naukri": NaukriAdapter, "linkedin": LinkedInAdapter, "mycareernet": MyCareernetAdapter,
            "teamlease": TeamLeaseAdapter, "randstad": RandstadAdapter, "wellfound": WellfoundAdapter}
console = Console()


def _defaults(site: str, cfg: dict) -> dict:
    qs = cfg.get("queries") or []
    kws = [q["keywords"] for q in qs]
    if site == "linkedin" and not kws:
        kws = [parse_qs(urlsplit(u).query).get("keywords", [""])[0] for u in cfg.get("search_urls") or []]
    locs = (qs[0].get("locations") if qs else None) or []
    days = (cfg.get("job_age_days") or cfg.get("date_posted_days")
            or {"day": 1, "week": 7, "month": 30}.get(cfg.get("date_posted"), 7))
    return {"keywords": [k for k in kws if k], "exp": (qs[0].get("experience") if qs else None) or 2,
            "locations": [l for l in locs if l], "days": days, "sort": cfg.get("sort_by_date", True),
            "ctc": int(cfg.get("min_ctc_lpa") or 0)}


GENERIC_ROLE_WORDS = {"development", "developer", "engineer", "software", "senior", "junior", "sr", "jr", "backend", "back",
                      "end", "frontend", "front", "full", "stack", "fullstack", "sde", "programmer", "lead",
                      "jobs", "job", "remote", "intern", "associate", "application", "web", "and", "or"}
STACK_ALIASES = {"golang": ["golang", "go"], "go": ["golang", "go"], "node": ["node", "nodejs", "node.js"],
                 "react": ["react", "reactjs", "react.js"], "dotnet": [".net", "dotnet", "c#"],
                 "net": [".net", "dotnet", "c#"], "js": ["javascript", "js"]}


def stack_terms(keywords: str) -> list[str]:
    """'golang developer' -> ['golang', 'go'].
    'golang developer, backend development' still requires golang. A generic phrase
    does not throw away the stacks you actually named."""
    specific = []
    for k in keywords.split(","):
        specific += [w for w in re.findall(r"[a-z0-9+#.]+", k.lower()) if w not in GENERIC_ROLE_WORDS]
    if not specific:
        return []                    # 'backend development' / 'software developer': use preferred stacks
    out = []
    for w in specific:
        out += STACK_ALIASES.get(w.strip("."), [w])
    return list(dict.fromkeys(out))


SOFTWARE_ROLE = re.compile(
    r"\b(software|sde|swe|backend|back[- ]end|full[- ]?stack|programmer|developer|development engineer|"
    r"application engineer|platform engineer|member of technical staff|mts|java|golang|go|spring|microservices?|api)\b", re.I)
NOT_SOFTWARE_ROLE = re.compile(
    r"\b(test(er|ing)?|qa|quality|automation tester|embedded|firmware|hardware|vlsi|asic|rtl|validation|"
    r"verification|data (analyst|scientist)|analyst|analytics|support|sales|marketing|consultant|"
    r"mechanical|electrical|civil|power|modem|camera|kernel|cpu|gpu|dsp|pmic|wlan|cad)\b", re.I)


EXCLUDE_TITLE = re.compile(
    r"\b(test(er|ing)?|qa|sdet\d*|quality assurance|guidewire|sap|salesforce|servicenow|mainframe|cobol|"
    r"pega|siebel|plm|enovia|data entry|bpo|voice process|sales|telecaller|technical support|"
    r"support engineer|desktop support|helpdesk|ios|android|mobile|flutter|react[- ]native|iphone)\b", re.I)


def excluded_title(title: str, extra: list[str] | None = None, wanted: str = "") -> bool:
    """Roles you didn't ask for (testers, Guidewire/SAP/Salesforce, support, sales...) — skipped
    before opening the job. A term you searched for yourself (e.g. 'sdet') is never excluded."""
    hit = EXCLUDE_TITLE.search(title)
    if hit and hit.group(0).lower() in wanted.lower():
        hit = None
    if not hit and extra:
        hit = next((x for x in extra if x and re.search(rf"\b{re.escape(x)}\b", title, re.I)), None)
    return bool(hit)


def excluded_company(company: str, names: list[str] | None = None, url: str = "") -> str | None:
    """Employers you never want (Infosys, Accenture, ...). Matches the name inside
    'Infosys BPM' / 'Accenture Solutions' and a careers URL. Returns the matched name."""
    blob = f"{company or ''} {url or ''}"
    for name in names or []:
        n = (name or "").strip()
        if n and re.search(rf"(?<![a-z0-9]){re.escape(n)}(?![a-z0-9])", blob, re.I):
            return n
    return None


def is_software_role(title: str) -> bool:
    """'Software Engineer', 'Java Developer', 'Backend Engineer' yes; 'Software Tester', 'Embedded Developer',
    'Data Analyst', plain 'Engineer' no."""
    return bool(SOFTWARE_ROLE.search(title)) and not NOT_SOFTWARE_ROLE.search(title)


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


def _naukri_terms(keywords: str) -> str:
    """Show Naukri's own suggestions for each typed keyword; pick by number (Enter = keep yours)."""
    from src.sites.naukri_suggest import suggest
    chosen = []
    for kw in [k.strip() for k in keywords.split(",") if k.strip()]:
        sugg = suggest(kw)
        if not sugg:
            chosen.append(kw)
            continue
        console.print(f"  Naukri terms for [bold]{escape(kw)}[/]: " + "  ".join(f"[cyan]{i}[/] {escape(x)}" for i, x in enumerate(sugg, 1)))
        raw = console.input(escape("    pick numbers (e.g. 1,2), Enter = keep yours > ")).strip()
        picks = [sugg[int(x) - 1] for x in re.findall(r"\d+", raw) if 0 < int(x) <= len(sugg)]
        chosen += picks or [kw]
    return ", ".join(dict.fromkeys(chosen))


def _ask_recommended(default: bool) -> bool:
    raw = console.input(escape(
        f"  Recommended jobs from the Jobs menu? [{'Y/n' if default else 'y/N'}] > ")).strip().lower()
    return default if not raw else raw.startswith("y")


def use_recommended_only(cfg: dict, ask: bool = False, tabs: list[str] | None = None):
    """Naukri: apply only to Jobs → Recommended jobs (Handpicked for you, and optionally the other tabs)."""
    from src.sites.naukri import REC_TABS
    if ask and tabs is None:
        raw = console.input(escape("  Tabs: Enter = Handpicked for you · a = all 4 (Handpicked, Applies, Preferences, "
                                   "You might like) · or e.g. handpicked,you_might_like > ")).strip().lower()
        tabs = (list(REC_TABS) if raw == "a" else
                [t.strip() for t in raw.split(",") if t.strip() in REC_TABS] or ["handpicked"])
    cfg.update(recommended=True, recommended_only=True, recommended_tabs=tabs or cfg.get("recommended_tabs")
               or ["handpicked"])
    console.print(f"  → Naukri recommended jobs only: {', '.join(REC_TABS[t] for t in cfg['recommended_tabs'])}")


def _ask_ctc(default: int) -> int:
    """Minimum package in LPA. Enter keeps the default; 0 means any salary."""
    raw = console.input(escape(
        f"  Minimum package in LPA (10 = 10 and above, 0 = any) [{default}] > ")).strip()
    return int(raw) if raw.isdigit() else default


def choose_search(site: str, settings: Settings, keywords=None, exp=None, location=None, days=None, ask=True):
    """Ask which stack / experience / location / freshness to search (or take them from CLI flags)
    and rewrite this run's search config. Nothing is saved to search.yaml."""
    cfg = settings.search.setdefault(site, {})
    d = _defaults(site, cfg)
    if ask and keywords is None:
        console.rule(f"[bold]What should I search on {site}?[/] (Enter = keep default)")
        hint = (f"Enter = your {len(cfg['search_urls'])} saved links" if cfg.get("search_urls")
                else ", ".join(d["keywords"]))
        extra_hint = "  ·  h = Handpicked/Recommended jobs only" if site == "naukri" else ""
        raw = console.input(escape(f"  Stack / role (comma for several) — or paste a {site} search link{extra_hint} "
                                   f"[{hint}] > ")).strip()
        if site == "naukri" and raw.lower() in ("h", "handpicked", "recommended", "reco"):
            return use_recommended_only(cfg, ask=True)
        if not raw and cfg.get("search_urls"):
            console.print(f"  → using your {len(cfg['search_urls'])} saved {site} search links (search.yaml)")
            if site == "linkedin":
                easy_only = cfg.get("easy_apply_only", False)
                r2 = console.input(escape(f"  Easy Apply only? (n = also company-site jobs) [{'Y/n' if easy_only else 'y/N'}] > ")).strip().lower()
                cfg["easy_apply_only"] = easy_only if not r2 else r2.startswith("y")
            return
        if raw.startswith("http"):
            cfg["search_urls"], cfg["queries"] = [raw], []
            raw2 = console.input(escape(f"  Posted within days (1, 3, 7, 15, 30) [{d['days']}] > ")).strip()
            days = int(raw2) if raw2.isdigit() else d["days"]
            if site == "naukri":
                cfg["job_age_days"] = days
                cfg["min_ctc_lpa"] = _ask_ctc(d["ctc"])
                cfg["recommended"] = _ask_recommended(cfg.get("recommended", True))
            extra = " · recommended jobs" if site == "naukri" and cfg.get("recommended") else ""
            console.print(f"  → using your link · last {days} day(s) · newest first"
                          + (f" · {cfg.get('min_ctc_lpa')}+ LPA" if site == "naukri" and cfg.get("min_ctc_lpa") else "")
                          + extra)
            return
        keywords = raw or ", ".join(d["keywords"])
        if site == "naukri" and raw:
            keywords = _naukri_terms(keywords)
        raw = console.input(escape(f"  Experience in years [{d['exp']}] > ")).strip()
        exp = int(raw) if raw.isdigit() else d["exp"]
        raw = console.input(escape(f"  Locations (comma; '-' = anywhere) [{', '.join(d['locations']) or 'anywhere'}] > ")).strip()
        location = "" if raw == "-" else (raw or ", ".join(d["locations"]))
        raw = console.input(escape(f"  Posted within days (1, 3, 7, 15, 30) [{d['days']}] > ")).strip()
        days = int(raw) if raw.isdigit() else d["days"]
        raw = console.input(escape(f"  Newest first (sort by date)? [{'Y/n' if d['sort'] else 'y/N'}] > ")).strip().lower()
        sort = d["sort"] if not raw else raw.startswith("y")
        ctc = _ask_ctc(d["ctc"]) if site == "naukri" else d["ctc"]
        if site == "naukri":
            cfg["recommended"] = _ask_recommended(cfg.get("recommended", True))
        if site == "linkedin":
            easy_only = cfg.get("easy_apply_only", False)
            raw = console.input(escape(f"  Easy Apply only? (n = also company-site jobs) [{'Y/n' if easy_only else 'y/N'}] > ")).strip().lower()
            cfg["easy_apply_only"] = easy_only if not raw else raw.startswith("y")
    else:
        sort = d["sort"]
        ctc = d["ctc"]
    kws = [k.strip() for k in (keywords if keywords is not None else ", ".join(d["keywords"])).split(",") if k.strip()]
    exp = d["exp"] if exp is None else exp
    locs = [l.strip() for l in (location if location is not None else ", ".join(d["locations"])).split(",") if l.strip()]
    days = d["days"] if days is None else days

    cfg["queries"] = [{"keywords": k, "locations": locs or [""], "experience": exp} for k in kws]
    cfg["search_urls"] = []
    cfg["sort_by_date"] = sort
    if site == "naukri":
        cfg["job_age_days"] = days
        cfg["min_ctc_lpa"] = ctc
    else:
        cfg["date_posted"] = "day" if days <= 1 else "week" if days <= 7 else "month"
        cfg["date_posted_days"] = days
        cfg["experience"] = exp
        cfg["experience_levels"] = [1, 2] if exp < 2 else [2, 3] if exp < 5 else [3, 4] if exp < 10 else [4, 5]
    console.print(f"  → {', '.join(kws)} · {exp} yrs · {', '.join(locs) or 'anywhere'} · last {days} day(s)"
                  f"{' · newest first' if sort else ''}"
                  + (f" · {ctc}+ LPA" if site == "naukri" and ctc else "")
                  + (" · recommended jobs" if site == "naukri" and cfg.get("recommended", True) else ""))


def login(site: str, settings: Settings):
    adapter = ADAPTERS[site](settings.search, Human(settings.search.get("delays", {})))
    with open_browser() as page:
        adapter.ensure_logged_in(page)


def _daily_cap(limits: dict, site: str) -> int:
    return (limits.get("daily_cap_by_site") or {}).get(site, limits.get("daily_cap", 25))


class _LimitReached(Exception):
    pass


def _external_site_mode(settings: Settings, now: bool, queue_external: bool) -> str:
    """now | queue | ask — how generic/Workday company sites are handled in --auto."""
    if now:
        return "now"
    if queue_external:
        return "queue"
    mode = str((settings.search.get("external") or {}).get("company_sites") or "ask").lower()
    return mode if mode in ("now", "queue", "ask") else "ask"


def run(site: str, settings: Settings, limit: int | None, dry_run: bool, auto: bool = False,
        retry: bool | None = None, now: bool = False, queue_external: bool = False):
    retry = auto if retry is None else retry            # --auto re-checks earlier skips unless --no-retry
    session_start = datetime.now().isoformat(timespec="seconds")
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
    review_cfg = dict(settings.search.get("review", {}))
    if auto:
        review_cfg.update(confirm_before_apply=False, auto_submit_known=True)
        console.print("[bold green]AUTO mode[/]: applies without asking when every answer is known; unknown "
                      "questions still ask you"
                      f"{'; re-checking earlier skipped jobs' if retry else ''}.\n"
                      "  Company sites (Workday / unknown): you choose apply now vs queue "
                      "([cyan]--now[/] / [cyan]--queue-external[/] or config external.company_sites).\n"
                      "  After each apply it asks: message people at that company on LinkedIn?")
    reviewer = Reviewer(review_cfg)
    external_sites = _external_site_mode(settings, now, queue_external)
    if auto and external_sites == "ask":
        pick = reviewer.choose(
            "Company-site jobs that need you — [cyan][Enter/n][/] apply as they appear  "
            "[cyan]q[/] queue until end  [cyan]a[/] ask each job",
            "nqa", default="n",
        )
        external_sites = {"n": "now", "q": "queue", "a": "ask"}[pick]
    elif auto and external_sites == "queue":
        console.print("  [dim]generic/Workday company sites → queued for end of this run "
                      "([cyan]python -m src.main apply --queued[/])[/]")
    elif auto and external_sites == "now":
        console.print("  [dim]generic/Workday company sites → opened immediately[/]")

    remaining = _daily_cap(limits, site) - tracker.applied_today(site)
    if limit:
        remaining = min(remaining, limit)
    if remaining <= 0 and not dry_run:
        console.print("Daily cap reached. Come back tomorrow.")
        return
    min_score = limits.get("min_match_score", 0)
    filters = settings.search.get("filters") or {}
    exclude_extra = filters.get("exclude_titles") or []
    exclude_companies = filters.get("exclude_companies") or []
    preferred = []
    for name in filters.get("preferred_stacks") or []:
        preferred += STACK_ALIASES.get(str(name).strip(".").lower(), [str(name).lower()])
    min_ctc = int((settings.search.get(site) or {}).get("min_ctc_lpa") or 0)
    ask_low = limits.get("ask_low_match_software", True)
    strict = bool((settings.search.get("filters") or {}).get("strict_stack", False))
    applied = 0

    with open_browser() as page:
        adapter.ensure_logged_in(page)
        quit_early = False
        try:
            for query in adapter.queries():
                terms = stack_terms(query.get("keywords", ""))
                found = seen_before = 0
                counts = Counter()
                for job in adapter.search(page, query):
                    found += 1
                    if tracker.seen(job, retry=retry):
                        seen_before += 1
                        continue
                    if excluded_title(job.title, exclude_extra, query.get("keywords", "")):
                        console.print(f"  [dim]not your role: {job.title} — {job.company}[/]")
                        counts["not your role"] += 1
                        if not dry_run:
                            tracker.log(job, "off_stack", note="excluded title")
                        continue
                    blocked = excluded_company(job.company, exclude_companies, job.url)
                    if blocked:
                        console.print(f"  [dim]skipped company ({blocked}): {job.title} — {job.company}[/]")
                        counts["excluded company"] += 1
                        if not dry_run:
                            tracker.log(job, "skipped", note=f"excluded company: {blocked}")
                        continue
                    ceiling = salary_ceiling_lpa(f"{job.salary} {job.snippet}")
                    if min_ctc and ceiling is not None and ceiling < min_ctc:
                        shown = job.salary or f"{ceiling:g} LPA"
                        console.print(f"  [dim]package {shown} < {min_ctc} LPA: {job.title} — {job.company}[/]")
                        counts["low package"] += 1
                        if not dry_run:
                            tracker.log(job, "skipped", note=f"package below {min_ctc} LPA")
                        continue
                    if not is_software_role(job.title):
                        console.print(f"  [dim]not a software role: {job.title} — {job.company}[/]")
                        counts["not your role"] += 1
                        if not dry_run:
                            tracker.log(job, "off_stack", note="not a software role")
                        continue
                    # cards show only ~5 tags -> judge the stack on the card only in strict mode
                    if strict and terms and job.tags and not on_stack(terms, " ".join([job.title, job.snippet, *job.tags])):
                        console.print(f"  [dim]off stack (card): {job.title} — {job.company}  [{', '.join(job.tags[:5])}][/]")
                        counts["off stack"] += 1
                        if not dry_run:
                            tracker.log(job, "off_stack", note="card tags")
                        continue
                    detail = adapter.open_job(page, job)
                    skip = (detail.skip_reason or ("already_applied" if detail.already_applied else
                            "closed" if detail.closed else
                            "external" if detail.external and not external_on else None))
                    if skip:
                        console.print(f"  [dim]{skip}: {job.title} — {job.company}[/]")
                        counts[skip] += 1
                        if not dry_run:
                            tracker.log(job, skip, apply_via="external" if skip == "external" else "")
                        continue
                    ceiling = salary_ceiling_lpa(f"{job.salary}\n{detail.description[:2000]}")
                    if min_ctc and ceiling is not None and ceiling < min_ctc:
                        shown = job.salary or f"{ceiling:g} LPA"
                        console.print(f"  [dim]package {shown} < {min_ctc} LPA: {job.title} — {job.company}[/]")
                        counts["low package"] += 1
                        if not dry_run:
                            tracker.log(job, "skipped", note=f"package below {min_ctc} LPA")
                        continue

                    score, reason = matcher.score(detail)
                    # A named stack (python, java, golang) must actually be in the job.
                    # A generic search ("software developer") must mention one preferred stack.
                    wanted = terms or preferred
                    blob = f"{job.title}\n{' '.join(job.tags)}\n{job.salary}\n{detail.description}"
                    if wanted and not on_stack(wanted, blob):
                        label = "/".join(wanted)
                        console.print(f"  [dim]off stack ({label} not mentioned, match {score}): "
                                      f"{job.title} — {job.company}[/]")
                        counts["off stack"] += 1
                        if not dry_run:
                            tracker.log(job, "off_stack", score, note=f"wanted {label}")
                        continue
                    kind = "external" if detail.external else site
                    low = score < min_score
                    if low and not ask_low:
                        console.print(f"  [dim]low match {score}: {job.title} — {job.company} ({reason})[/]")
                        counts["low match"] += 1
                        if not dry_run:
                            tracker.log(job, "low_match", score, reason)
                        continue
                    if dry_run:
                        console.print(f"  [green]would {'ask (low match)' if low else 'apply'}[/] {score} [{kind}]: "
                                      f"{job.title} — {job.company}")
                        continue

                    if not reviewer.confirm_apply(detail, score, reason, low_match=low):
                        tracker.log(job, "low_match" if low else "skipped", score, reason if low else "")
                        continue

                    session = ApplySession(detail, engine, reviewer, human, settings.resume_path)
                    status, via, ext_url = _apply_one(adapter, page, session, site, external_sites)
                    tracker.log(job, status, score, apply_via=via, external_url=ext_url)
                    console.print(f"  → [bold]{status}[/] via {via}")
                    if status.startswith("applied") and settings.outreach.get("ask_after_apply"):
                        referral(page, settings, job.company, job.title, detail.description, resume, reviewer,
                                 human, tracker, company_hint=detail.company_url, job_url=job.url)
                    if status.startswith("applied"):
                        applied += 1
                        if applied >= remaining:
                            console.print(f"Reached limit ({applied} applied).")
                            raise _LimitReached()
                    human.between_jobs()
                console.print(f"  [bold]search summary:[/] {found} found · {seen_before} already in history"
                              f" · {found - seen_before} checked now"
                              + ("" if retry or not seen_before else
                                 "   (re-check skipped/low-match ones with [cyan]--retry[/])"))
                if counts:
                    console.print("  skipped: " + " · ".join(f"{n} {why}" for why, n in counts.most_common()))
                if found and counts["off stack"] + counts["not your role"] >= 0.8 * max(found - seen_before, 1):
                    console.print("  [yellow]tip:[/] few jobs for this stack. Search several "
                                  "([cyan]python developer, golang developer, java developer[/]) or use 15/30 days.")
        except _LimitReached:
            pass
        except QuitRun:
            console.print("Stopped by you.")
            quit_early = True
        if auto and not dry_run and not quit_early:
            applied += _work_queue(page, settings, engine, human, tracker, since=session_start)
        console.print(f"Applied this run: [bold]{applied}[/]   History CSV: {CSV_PATH}")


def referral(page, settings: Settings, company: str, role: str, job_text: str, resume: str,
             reviewer: Reviewer, human: Human, tracker: Tracker, company_hint: str = "", job_url: str = "",
             ask: bool = True) -> int:
    """Write prep notes, then (after your OK) message people at the company on LinkedIn."""
    cfg = settings.outreach
    if not cfg.get("enabled"):
        return 0
    if is_anonymous_company(company):
        console.print(f"  [dim]referral skipped: company is hidden ({company!r}). If you know it: "
                      f"python -m src.main outreach --company \"<name>\" --role \"{role}\"[/]")
        return 0
    pitch = build_pitch(cfg, role, company, job_text, resume)
    prep = write_prep(pitch, role, company, job_text, job_url)
    console.print(f"  prep notes: {prep}   [dim]skills used: {', '.join(pitch.matched)}[/]")
    if ask:
        if reviewer.choose(
                f"Message people at [bold]{company}[/] on LinkedIn? [cyan]y[/] yes  [cyan][Enter][/] no",
                "yn", default="n") == "n":
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


def _apply_one(adapter, page, session: ApplySession, site: str,
               external_sites: str = "now") -> tuple[str, str, str]:
    """Returns (status, apply_via, external_url). external_sites: now | queue | ask (per-job)."""
    if not session.detail.external:
        try:
            return adapter.apply(page, session), f"{site}_easy" if site == "linkedin" else site, ""
        except SkipJob:
            return "abandoned", site, ""

    ext = adapter.open_external(page)
    if ext is None:
        return "failed", "external", ""
    ext_url = ext.url
    mode = external_sites
    if session.reviewer.auto_submit:
        ext.wait_for_load_state("domcontentloaded")
        ext.wait_for_timeout(2500)
        handler = detect(ext)
        if handler.name in ("generic", "workday"):
            if mode == "ask":
                pick = session.reviewer.choose(
                    f"[{handler.name}] company site needs you — [cyan][Enter/n][/] apply now  "
                    "[cyan]q[/] queue for end  [cyan]s[/] skip",
                    "nqns", default="n",
                )
                if pick == "s":
                    if ext is not page:
                        ext.close()
                        page.bring_to_front()
                    return "skipped", handler.name, ext_url
                mode = "queue" if pick == "q" else "now"
            if mode == "queue":
                ext_url = ext.url
                if ext is not page:
                    ext.close()
                    page.bring_to_front()
                console.print(f"  [yellow]queued[/] ({handler.name} site needs you) — end of run "
                              f"([cyan]python -m src.main apply --queued[/]) or next run with [cyan]--now[/]")
                return "queued", handler.name, ext_url
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
        blocked_names = (settings.search.get("filters") or {}).get("exclude_companies") or []
        for i, url in enumerate(urls, 1):
            console.rule(f"[bold]Link {i}/{len(urls)}[/]")
            host = urlsplit(url).hostname or ""
            blocked = excluded_company(host, blocked_names, url)
            if blocked:
                console.print(f"  [dim]skipped company ({blocked}): {url}[/]")
                continue
            page.goto(url, wait_until="domcontentloaded")
            page.wait_for_timeout(3000)
            header = page.locator("[data-automation-id='jobPostingHeader'], h1").filter(visible=True)
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


def _work_queue(page, settings: Settings, engine, human, tracker, since: str = "") -> int:
    """Company-site jobs queued (unknown sites / Workday logins): fill what's known, you finish,
    the tool learns. since = only rows queued at/after this timestamp (this session). Returns applied."""
    rows = tracker.db.execute(
        "SELECT site, job_id, title, company, url, external_url, location FROM applications "
        "WHERE status='queued' AND updated_at >= ? ORDER BY updated_at", (since,)).fetchall()
    if not rows:
        return 0
    reviewer = Reviewer({**settings.search.get("review", {}), "auto_submit_known": False})
    console.rule(f"[bold magenta]{len(rows)} company-site jobs that need you[/] — s = skip one, q = stop (rest stay queued)")
    applied = 0
    blocked_names = (settings.search.get("filters") or {}).get("exclude_companies") or []
    for i, (site, job_id, title, company, url, ext_url, loc) in enumerate(rows, 1):
        console.rule(f"[bold]{i}/{len(rows)} · {company}[/] — {title}")
        job = Job(site=site, id=job_id, title=title, company=company, url=url, location=loc or "")
        blocked = excluded_company(company, blocked_names, ext_url or url)
        if blocked:
            console.print(f"  [dim]skipped company ({blocked})[/]")
            tracker.log(job, "skipped", note=f"excluded company: {blocked}")
            continue
        page.goto(ext_url or url, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        detail = JobDetail(job, description=page.locator("body").inner_text()[:6000], external=True)
        session = ApplySession(detail, engine, reviewer, human, settings.resume_path)
        try:
            status, via = apply_external(page, session)
        except SkipJob:
            status, via = "skipped", "external"
        except QuitRun:
            console.print("Stopped — remaining jobs stay in the queue (python -m src.main apply --queued).")
            break
        tracker.log(job, status, apply_via=via, external_url=page.url)
        console.print(f"  → [bold]{status}[/] via {via}")
        if status.startswith("applied"):
            applied += 1
            if settings.outreach.get("ask_after_apply"):
                try:
                    referral(page, settings, job.company, job.title, detail.description, engine.resume_text,
                             reviewer, human, tracker, job_url=page.url)
                except QuitRun:
                    console.print("Stopped — remaining jobs stay in the queue (python -m src.main apply --queued).")
                    break
    return applied


def apply_queued(settings: Settings):
    """`apply --queued`: every queued company-site job, any session."""
    tracker = Tracker()
    if not tracker.db.execute("SELECT 1 FROM applications WHERE status='queued'").fetchone():
        console.print("Queue is empty.")
        return
    human = Human(settings.search.get("delays", {}))
    engine = AnswerEngine(settings.profile, load_resume_text(settings.resume_path),
                          llm_mod.from_env(settings.llm_provider))
    with open_browser() as page:
        _work_queue(page, settings, engine, human, tracker)


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
