"""Build job-specific referral messages + interview prep notes."""
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from src.config import DATA_DIR
from src.outreach.skills import find_skills

NOTE_LIMIT = 300            # LinkedIn connection-request note
PREP_DIR = DATA_DIR / "prep"


@dataclass
class Pitch:
    message: str
    note: str
    subject: str
    matched: list[str]      # job skills you have (first 5 go in the message) -> revise these
    to_study: list[str]     # job skills not on your resume


def _join(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1] if items else ""


def _clean_role(role: str) -> str:
    return re.sub(r"\s*\((?:remote|hybrid|on-?site)[^)]*\)|\s+with verification$", "", role, flags=re.I).strip()


def build_pitch(cfg: dict, role: str, company: str, job_text: str, resume_text: str,
                first_name: str = "there") -> Pitch:
    job_skills = find_skills(f"{role}\n{job_text}")
    mine = set(find_skills(resume_text))
    matched = [s for s in job_skills if s in mine]
    to_study = [s for s in job_skills if s not in mine]
    if not matched:                          # generic JD: fall back to your strongest resume skills
        matched = find_skills(resume_text)[:5]

    values = {
        "first_name": first_name or "there", "role": _clean_role(role) or "open", "company": company,
        "my_title": cfg.get("my_title", ""), "my_company": cfg.get("my_company", ""),
        "skills": _join(matched[:5]), "skills_short": ", ".join(matched[:3]),
    }
    message = " ".join(cfg["message"].format(**values).split())
    subject = cfg.get("inmail_subject", "").format(**values)
    for n in range(min(len(matched), 6), 0, -1):          # as many job skills as fit in 300 chars
        values["skills_short"] = _join(matched[:n])
        note = " ".join(cfg["note"].format(**values).split())
        if len(note) <= NOTE_LIMIT - 12:                  # leave room for a longer first name
            break
    while len(note) > NOTE_LIMIT and values["skills_short"].count(","):    # drop skills until it fits
        values["skills_short"] = values["skills_short"].rsplit(",", 1)[0]
        note = " ".join(cfg["note"].format(**values).split())
    if len(note) > NOTE_LIMIT:
        note = note[:NOTE_LIMIT - 1].rsplit(" ", 1)[0] + "…"
    return Pitch(message, note, subject, matched, to_study)


TRIMMABLE = [  # least important first; the referral ask is never removed
    r" and my (?:experience|background) (?:matches|lines up)[^.]*", r"\s*Happy to share my resume\.",
    r" really", r",? working with [^.]*?(?= I applied)", r"\s*Thank you!$",
]


def fit_note(text: str, limit: int = NOTE_LIMIT) -> str:
    """Keep a connection note within LinkedIn's limit by dropping the least important phrases;
    never cuts mid-word, never drops the referral ask."""
    for pattern in TRIMMABLE:
        if len(text) <= limit:
            break
        text = re.sub(pattern, "", text, count=1)
        text = re.sub(r"\s+([.,])", r"\1", " ".join(text.split()))
    return text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0] + "…"


def with_name(text: str, first_name: str) -> str:
    return re.sub(r"^Hi [^,]+,", f"Hi {first_name},", text)


def write_prep(pitch: Pitch, role: str, company: str, job_text: str, url: str = "") -> Path:
    """Markdown notes so you can revise exactly what you claimed before the interview."""
    PREP_DIR.mkdir(parents=True, exist_ok=True)
    slug = re.sub(r"[^a-z0-9]+", "-", f"{company}-{role}".lower()).strip("-")[:70]
    path = PREP_DIR / f"{date.today():%Y%m%d}-{slug}.md"
    reqs = [l.strip(" •-*\t") for l in job_text.splitlines()
            if 25 < len(l.strip()) < 220 and re.search(r"experience|knowledge|proficien|strong|must|should|understanding|familiar", l, re.I)]
    lines = [
        f"# {role} — {company}",
        f"_{date.today():%d %b %Y}_  {url}",
        "",
        "## Skills you mentioned (they WILL ask — revise these)",
        *[f"- [ ] **{s}**" for s in pitch.matched],
        "",
        "## Skills in the JD that are not on your resume (study basics)",
        *([f"- [ ] {s}" for s in pitch.to_study] or ["- none"]),
        "",
        "## Key requirements from the JD",
        *([f"- {r}" for r in reqs[:15]] or ["- (no description captured)"]),
        "",
        "## Message sent",
        f"> {pitch.message}",
        "",
    ]
    path.write_text("\n".join(lines))
    return path
