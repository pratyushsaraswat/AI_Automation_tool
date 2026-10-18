"""Answer memory: every question you answer is stored and reused for similar questions later.

Not model fine-tuning — retrieval:
  * a new question is reduced to its key words (stopwords dropped, synonyms unified, plurals folded)
    and compared with every stored question (word overlap + string similarity);
  * an exact/near-exact match is reused automatically, a close match is proposed for you to confirm;
  * when an LLM is configured, the most similar stored Q&As are sent with the question as examples,
    so it answers new questions the way you did before.

Stored in data/answer_memory.json (human-readable; edit it or use `python -m src.main answers ...`).
"""
import difflib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime

from src.config import DATA_DIR

STOPWORDS = set("""
a an the of in on at to for with and or is are am be been was were do does did have has had you your
yours i me my we our please kindly enter select choose provide mention state specify what which how
many much long whether if this that these those as by from any currently total overall
now future will would can could work days day lakhs lakh inr rs rupees approx approximately
per annum annual pa p.a looking job role position hold hand there
""".split())
GENERIC_EXP = {"year", "experience", "total", "relevant", "professional", "industry", "it"}
SYNONYMS = {
    "yrs": "year", "yr": "year", "years": "year", "exp": "experience", "experienced": "experience",
    "ctc": "salary", "compensation": "salary", "package": "salary", "pay": "salary",
    "lpa": "salary", "remuneration": "salary",
    "golang": "go", "js": "javascript", "reactjs": "react", "nodejs": "node",
    "springboot": "spring", "k8s": "kubernetes",
    "joining": "join", "notice": "notice", "relocation": "relocate", "relocating": "relocate",
    "willing": "willing", "comfortable": "willing", "okay": "willing", "ok": "willing",
    "authorised": "authorized", "authorization": "authorized", "visa": "sponsorship",
    "immediately": "immediate", "joiner": "join", "joinee": "join", "open": "willing", "ready": "willing",
    "qualification": "education", "degree": "education", "graduation": "graduate", "graduated": "graduate",
    "why": "reason", "offers": "offer", "shifts": "shift", "wfo": "office", "onsite": "office",
}
NO_FOLD = {"redis", "jenkins", "kubernetes", "aws", "postgres", "express", "rails", "analytics", "pandas",
           "devops", "sass", "js", "nodejs", "nextjs", "vuejs", "reactjs", "angularjs", "kinesis", "elasticsearch",
           "teams", "jira", "ms", "gcs", "ios", "windows", "mlops", "datadog"}
FILE = DATA_DIR / "answer_memory.json"
LEGACY_FILE = DATA_DIR / "answers_cache.json"


def keywords(text: str) -> frozenset[str]:
    words = re.sub(r"[^a-z0-9+#. ]+", " ", text.lower()).split()
    out = set()
    for w in words:
        w = w.strip(".")
        w = SYNONYMS.get(w, w)
        if len(w) > 3 and w.endswith("s") and not w.endswith("ss") and w not in NO_FOLD:
            w = SYNONYMS.get(w[:-1], w[:-1])
        if w and w not in STOPWORDS:
            out.add(w)
    return frozenset(out)


def similarity(a: str, b: str) -> float:
    ka, kb = keywords(a), keywords(b)
    if not ka or not kb:
        return 0.0
    jaccard = len(ka & kb) / len(ka | kb)
    if (ka - kb) and (kb - ka):      # each has a word the other lacks: "java" vs "go", "current" vs "expected"
        return jaccard
    small, big = (ka, kb) if len(ka) <= len(kb) else (kb, ka)
    if big & {"year", "experience"} and small <= GENERIC_EXP and big - GENERIC_EXP:
        return 0.0                   # "total experience" must not answer "years of Python"
    ratio = difflib.SequenceMatcher(None, " ".join(sorted(ka)), " ".join(sorted(kb))).ratio()
    contained = 0.75 if small < big else 1.0   # "CGPA" inside "graduation percentage / CGPA"
    return max(jaccard, ratio * 0.95, contained)


@dataclass
class Entry:
    question: str
    answer: str
    kind: str = "text"
    options: list[str] = field(default_factory=list)
    source: str = "user"            # user | llm-confirmed | learn | import
    uses: int = 0
    updated: str = ""


class AnswerMemory:
    def __init__(self, path=FILE):
        self.path = path
        self.entries: list[Entry] = []
        if path.exists():
            self.entries = [Entry(**e) for e in json.loads(path.read_text())]
        elif LEGACY_FILE.exists():           # migrate the old flat cache
            for q, a in json.loads(LEGACY_FILE.read_text()).items():
                self.entries.append(Entry(q, a, source="import"))
            self.save()

    def save(self):
        self.path.write_text(json.dumps([asdict(e) for e in self.entries], indent=2, ensure_ascii=False))

    def find(self, question: str) -> tuple[Entry | None, float]:
        best, best_score = None, 0.0
        for e in self.entries:
            s = similarity(question, e.question)
            if s > best_score:
                best, best_score = e, s
        return best, best_score

    def similar(self, question: str, k: int = 12) -> list[Entry]:
        scored = sorted(self.entries, key=lambda e: similarity(question, e.question), reverse=True)
        return [e for e in scored[:k] if similarity(question, e.question) > 0.2]

    def add(self, question: str, answer: str, kind: str = "text", options: list[str] | None = None,
            source: str = "user"):
        now = datetime.now().isoformat(timespec="seconds")
        for e in self.entries:
            if keywords(e.question) == keywords(question):     # same question -> update in place
                e.answer, e.kind, e.source, e.updated = answer, kind, source, now
                e.options = options or e.options
                self.save()
                return
        self.entries.append(Entry(question, answer, kind, options or [], source, 0, now))
        self.save()

    def used(self, entry: Entry):
        entry.uses += 1
        self.save()

    def delete(self, index: int) -> Entry:
        e = self.entries.pop(index)
        self.save()
        return e

    def search(self, text: str = "") -> list[tuple[int, Entry]]:
        rows = list(enumerate(self.entries))
        if text:
            rows = [(i, e) for i, e in rows if text.lower() in e.question.lower() or similarity(text, e.question) > 0.5]
        return rows
