from dataclasses import dataclass, field


@dataclass
class Job:
    site: str
    id: str
    title: str
    company: str
    url: str
    location: str = ""
    experience: str = ""
    tags: list[str] = field(default_factory=list)   # skills shown on the search card (Naukri key skills)
    snippet: str = ""                               # short description shown on the search card
    salary: str = ""                                # posted package, when the card shows one ("10-18 Lacs PA")


@dataclass
class JobDetail:
    job: Job
    description: str
    external: bool = False         # applies on the company's own site (Greenhouse, Lever, ...)
    already_applied: bool = False
    closed: bool = False           # no longer accepting applications
    company_url: str = ""          # LinkedIn company page, when known (used for referral outreach)
    skip_reason: str = ""          # adapter says skip (e.g. "too_senior"), logged with this status


@dataclass
class Field:
    label: str
    kind: str                      # text | textarea | radio | checkbox | single_checkbox | chips | select
    options: list[str] = field(default_factory=list)
    required: bool = False


@dataclass
class Answer:
    value: str
    confidence: float
    source: str                    # profile | cache | llm | user


class SkipJob(Exception):
    pass


class QuitRun(Exception):
    pass
