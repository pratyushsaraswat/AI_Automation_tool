from abc import ABC, abstractmethod
from typing import Iterator

from src.core.human import Human
from src.core.models import Job, JobDetail
from src.core.session import ApplySession


class SiteAdapter(ABC):
    """Contract each job site implements. The runner never knows which site it drives."""

    name: str

    def __init__(self, search_cfg: dict, human: Human):
        self.cfg = search_cfg.get(self.name) or {}
        self.human = human

    @abstractmethod
    def ensure_logged_in(self, page) -> None:
        """Open the site; if logged out, wait for the user to log in manually."""

    @abstractmethod
    def queries(self) -> Iterator[dict]:
        """Expand search config into individual searches."""

    @abstractmethod
    def search(self, page, query: dict) -> Iterator[Job]:
        ...

    @abstractmethod
    def open_job(self, page, job: Job) -> JobDetail:
        ...

    @abstractmethod
    def apply(self, page, session: ApplySession) -> str:
        """Run the apply flow. Return status: applied | failed | unknown.
        Raise SkipJob / QuitRun (via session) when the user says so."""
