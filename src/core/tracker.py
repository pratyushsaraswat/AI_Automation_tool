import csv
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
    status TEXT,          -- applied | applied_manual | skipped | low_match | external | already_applied
                          -- | closed | failed | abandoned | unknown
    score INTEGER,
    note TEXT,
    updated_at TEXT,
    PRIMARY KEY (site, job_id)
)
"""
OUTREACH_SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach (
    company TEXT, role TEXT, person TEXT, profile_url TEXT, kind TEXT, status TEXT, sent_at TEXT
)
"""
OUTREACH_CSV = DATA_DIR / "outreach.csv"
# added later; migrated onto existing databases
EXTRA_COLUMNS = {"apply_via": "TEXT", "external_url": "TEXT", "location": "TEXT"}
CSV_PATH = DATA_DIR / "applications.csv"
CSV_COLUMNS = ["updated_at", "site", "company", "title", "location", "status", "apply_via",
               "score", "url", "external_url", "note"]
APPLIED = ("applied", "applied_manual")


class Tracker:
    def __init__(self, path=DATA_DIR / "applications.db"):
        self.db = sqlite3.connect(path)
        self.db.execute(SCHEMA)
        self.db.execute(OUTREACH_SCHEMA)
        have = {row[1] for row in self.db.execute("PRAGMA table_info(applications)")}
        for col, typ in EXTRA_COLUMNS.items():
            if col not in have:
                self.db.execute(f"ALTER TABLE applications ADD COLUMN {col} {typ}")
        self.db.commit()

    def seen(self, job: Job) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM applications WHERE site=? AND job_id=?", (job.site, job.id)
        ).fetchone()
        return row is not None

    def log(self, job: Job, status: str, score: int | None = None, note: str = "",
            apply_via: str = "", external_url: str = ""):
        self.db.execute(
            "INSERT OR REPLACE INTO applications (site, job_id, title, company, url, status, score, note, "
            "updated_at, apply_via, external_url, location) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (job.site, job.id, job.title, job.company, job.url, status, score, note,
             datetime.now().isoformat(timespec="seconds"), apply_via, external_url, job.location),
        )
        self.db.commit()
        self.export_csv()

    def applied_today(self, site: str) -> int:
        return self.db.execute(
            f"SELECT COUNT(*) FROM applications WHERE site=? AND status IN {APPLIED} AND updated_at LIKE ?",
            (site, f"{date.today().isoformat()}%"),
        ).fetchone()[0]

    def export_csv(self, path=CSV_PATH, applied_only: bool = False) -> int:
        """Rewrites the CSV from the DB (newest first). Open it in Excel / Google Sheets."""
        where = f"WHERE status IN {APPLIED}" if applied_only else ""
        rows = self.db.execute(
            f"SELECT {', '.join(CSV_COLUMNS)} FROM applications {where} ORDER BY updated_at DESC"
        ).fetchall()
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(CSV_COLUMNS)
            w.writerows(rows)
        return len(rows)

    # --- referral outreach ---

    def contacted(self, profile_url: str) -> bool:
        return self.db.execute("SELECT 1 FROM outreach WHERE profile_url=? AND status IN ('sent','sent?','already_pending')",
                               (profile_url,)).fetchone() is not None

    def outreach_today(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outreach WHERE status LIKE 'sent%' AND sent_at LIKE ?",
                               (f"{date.today().isoformat()}%",)).fetchone()[0]

    def log_outreach(self, company, role, person, profile_url, kind, status):
        self.db.execute("INSERT INTO outreach VALUES (?,?,?,?,?,?,?)",
                        (company, role, person, profile_url, kind, status,
                         datetime.now().isoformat(timespec="seconds")))
        self.db.commit()
        rows = self.db.execute("SELECT sent_at, company, role, person, kind, status, profile_url FROM outreach "
                               "ORDER BY sent_at DESC").fetchall()
        with open(OUTREACH_CSV, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(["sent_at", "company", "role", "person", "kind", "status", "profile_url"])
            w.writerows(rows)

    def pending_outreach(self, days: int = 7):
        """Applied jobs (last N days) at companies nobody was contacted at yet."""
        return self.db.execute(
            f"SELECT company, title, url, site FROM applications WHERE status IN {APPLIED} "
            "AND updated_at >= date('now', ?) AND company NOT IN (SELECT DISTINCT company FROM outreach WHERE status LIKE 'sent%') "
            "GROUP BY company ORDER BY updated_at DESC", (f"-{days} days",)).fetchall()

    def summary(self):
        return self.db.execute(
            "SELECT site, status, COUNT(*) FROM applications GROUP BY site, status ORDER BY site, status"
        ).fetchall()

    def recent(self, limit=20):
        return self.db.execute(
            "SELECT updated_at, site, status, apply_via, score, company, title FROM applications "
            "ORDER BY updated_at DESC LIMIT ?", (limit,)
        ).fetchall()
