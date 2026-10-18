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


@dataclass
class JobDetail:
    job: Job
    description: str
    external: bool = False         # applies on the company's own site (Greenhouse, Lever, ...)
    already_applied: bool = False
    closed: bool = False           # no longer accepting applications
    company_url: str = ""          # LinkedIn company page, when known (used for referral outreach)


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
