"""External company-site (ATS) handlers. Entry point: apply_external(page, session)."""
from src.ats.generic import GenericHandler
from src.ats.greenhouse import GreenhouseHandler
from src.ats.lever import LeverHandler
from src.ats.workday import WorkdayHandler
from src.core.session import ApplySession

HANDLERS = [WorkdayHandler, GreenhouseHandler, LeverHandler]


def detect(page):
    for cls in HANDLERS:
        if cls.matches(page):
            return cls()
    return GenericHandler()


def apply_external(page, session: ApplySession) -> tuple[str, str]:
    """page = the company-site tab. Returns (status, via)."""
    page.wait_for_load_state("domcontentloaded")
    page.wait_for_timeout(2500)          # redirects (LinkedIn -> tracking -> ATS) settle
    handler = detect(page)
    print(f"  external site: {page.url[:90]}  → handler: {handler.name}")
    return handler.apply(page, session), handler.name


__all__ = ["apply_external", "detect"]
