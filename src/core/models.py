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
    external: bool = False         # "Apply on company site" — we skip these
    already_applied: bool = False


@dataclass
class Field:
    label: str
    kind: str                      # text | radio | checkbox | chips | select
    options: list[str] = field(default_factory=list)


@dataclass
class Answer:
    value: str
    confidence: float
    source: str                    # profile | cache | llm | user


class SkipJob(Exception):
    pass


class QuitRun(Exception):
    pass
