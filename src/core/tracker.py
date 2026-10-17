import sqlite3
from datetime import date, datetime

from src.config import DATA_DIR
from src.core.models import Job

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications (
    site TEXT NOT NULL,
    job_id TEXT NOT NULL,
    title TEXT,
    company TEXT,
    url TEXT,
    status TEXT,          -- applied | skipped | low_match | external | already_applied | failed | abandoned
    score INTEGER,
    note TEXT,
    updated_at TEXT,
    PRIMARY KEY (site, job_id)
)
"""


class Tracker:
    def __init__(self, path=DATA_DIR / "applications.db"):
        self.db = sqlite3.connect(path)
        self.db.execute(SCHEMA)

    def seen(self, job: Job) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM applications WHERE site=? AND job_id=?", (job.site, job.id)
        ).fetchone()
        return row is not None

    def log(self, job: Job, status: str, score: int | None = None, note: str = ""):
        self.db.execute(
            "INSERT OR REPLACE INTO applications VALUES (?,?,?,?,?,?,?,?,?)",
            (job.site, job.id, job.title, job.company, job.url, status, score, note,
             datetime.now().isoformat(timespec="seconds")),
        )
        self.db.commit()

    def applied_today(self, site: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM applications WHERE site=? AND status='applied' AND updated_at LIKE ?",
            (site, f"{date.today().isoformat()}%"),
        ).fetchone()[0]

    def summary(self):
        return self.db.execute(
            "SELECT site, status, COUNT(*) FROM applications GROUP BY site, status ORDER BY site, status"
        ).fetchall()

    def recent(self, limit=20):
        return self.db.execute(
            "SELECT updated_at, site, status, score, company, title FROM applications "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
