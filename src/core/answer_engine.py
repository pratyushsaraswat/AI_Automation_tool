"""Resolve a form question: profile.yaml -> answer memory -> LLM (with memory examples) -> you."""
import difflib
import re

import yaml

from src.core.llm import LLM, LLMUnavailable
from src.core.memory import GENERIC_EXP, AnswerMemory, keywords
from src.core.models import Answer, Field, JobDetail

SYSTEM = """You fill job application form questions on behalf of a candidate.
Rules:
- Answer ONLY from the candidate profile and resume provided. Never invent skills, employers or years.
- If options are given, "value" must be exactly one of the options (for checkbox kind: comma-separated options).
- Numeric questions (years, CTC, notice days): return just the number.
- Keep text answers short (one line) unless the question asks for a description.
- PAST ANSWERS are the candidate's own answers to earlier forms: follow them for the same or similar
  questions (same facts, same style). They override guesses.
- If the information is not in the profile/resume/past answers, give the most conservative honest
  answer and confidence below 0.5.
Return JSON: {"value": "<answer>", "confidence": <0..1>}"""


def _too_generic(key: str, label: str) -> bool:
    """'years of experience' must not answer 'Years of Rust experience'."""
    lk, kk = keywords(label), keywords(key)
    if not lk & {"year", "experience"}:
        return False
    return kk <= GENERIC_EXP and bool(lk - GENERIC_EXP - kk)


def _words(text: str) -> set[str]:
    return set(re.sub(r"[^a-z0-9 ]+", " ", text.lower()).split())


def _numeric_option(value: str, options: list[str]) -> str | None:
    """'2' -> '1-3 years' / '2+ years' / 'Less than 3'; '30' -> '15-30 days'."""
    try:
        v = float(value.strip().split()[0])
    except (ValueError, IndexError):
        return None
    best = None
    for opt in options:
        nums = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", opt)]
        low = opt.lower()
        if len(nums) >= 2 and nums[0] <= v <= nums[1]:
            return opt
        if len(nums) == 1:
            n = nums[0]
            if ("+" in opt or "more" in low or "above" in low or "over" in low) and v >= n:
                best = opt
            elif ("less" in low or "under" in low or "below" in low or "<" in opt) and v < n:
                best = best or opt
            elif v == n:
                return opt
    return best


def _closest_option(value: str, options: list[str]) -> str | None:
    if not options:
        return value
    for opt in options:
        if opt.strip().lower() == value.strip().lower():
            return opt
    yes = next((o for o in options if o.strip().lower() == "yes"), None)
    no = next((o for o in options if o.strip().lower() == "no"), None)
    v = value.strip().lower().rstrip(".!")
    if yes and v in {"y", "yeah", "yep", "sure", "ok", "okay", "both", "true", "agree", "i agree", "of course"}:
        return yes
    if no and v in {"n", "nope", "false", "never", "not really", "na", "n/a"}:
        return no
    numeric = _numeric_option(value, options)
    if numeric:
        return numeric
    match = difflib.get_close_matches(value, options, n=1, cutoff=0.6)
    if match:
        return match[0]
    vw = {w[:6] for w in re.findall(r"[a-z]{4,}", value.lower())}   # "Bachelor of Engineering" ~ "Bachelor's Degree"
    hits = [o for o in options if vw & {w[:6] for w in re.findall(r"[a-z]{4,}", o.lower())}]
    return hits[0] if len(hits) == 1 else None


class AnswerEngine:
    def __init__(self, profile: dict, resume_text: str, llm: LLM | None):
        self.profile = profile
        self.resume_text = resume_text[:12000]
        self._resume_lc = resume_text.lower()
        self.llm = llm
        self.memory = AnswerMemory()

    def resolve(self, f: Field, detail: JobDetail, use_llm: bool = True) -> Answer | None:
        mem = self._from_memory(f)
        if mem and mem.confidence >= 0.9:
            return mem              # your latest answer to this exact question wins
        ans = self._from_profile(f) or self._from_skill_years(f)
        if ans:
            return ans
        llm = self._from_llm(f, detail) if use_llm else None
        if mem and (not llm or llm.confidence < mem.confidence):
            return mem              # close (not exact) memory match: proposed, you confirm
        return llm

    def profile_answer(self, f: Field) -> Answer | None:
        return self._from_profile(f)

    def remember(self, f: Field, value: str, source: str = "user"):
        self.memory.add(f.label, value, f.kind, f.options, source)

    def _builtin_answers(self) -> dict:
        """Standard contact fields derived from profile.personal / experience."""
        p = self.profile.get("personal") or {}
        e = self.profile.get("experience") or {}
        name = (p.get("full_name") or "").split()
        return {
            "name": p.get("full_name"), "full name": p.get("full_name"),
            "first name": name[0] if name else "", "last name": name[-1] if len(name) > 1 else "",
            "email": p.get("email"), "phone": p.get("phone"), "mobile": p.get("phone"),
            "phone number": p.get("phone"), "mobile phone number": p.get("phone"),
            "linkedin": p.get("linkedin_url"), "linkedin profile": p.get("linkedin_url"),
            "github": p.get("github_url"), "website": p.get("github_url"), "portfolio": p.get("github_url"),
            "city": p.get("current_city"), "location": p.get("current_city"),
            "current company": e.get("current_company"), "company": e.get("current_company"),
            "current employer": e.get("current_company"), "organization": e.get("current_company"),
            "current title": e.get("current_title"), "job title": e.get("current_title"),
        }

    def _from_profile(self, f: Field) -> Answer | None:
        label_words = _words(f.label)
        best = None
        answers = {**self._builtin_answers(), **(self.profile.get("answers") or {})}
        for key, value in answers.items():
            if value in ("", None):
                continue
            rank = (len(_words(key)), len(key))  # most specific key wins
            if (_words(key) <= label_words and not _too_generic(key, f.label)
                    and (best is None or rank > best[0])):
                best = (rank, str(value))
        if not best:
            return None
        value = _closest_option(best[1], f.options)
        return Answer(value, 0.95, "profile") if value else None

    def _from_skill_years(self, f: Field) -> Answer | None:
        """'Years of experience with X?' -> experience.skill_years when X is on the resume / skills list.
        'Do you have experience with X?' (Yes/No) -> Yes for the same skills."""
        exp = self.profile.get("experience") or {}
        years = exp.get("skill_years")
        lk = keywords(f.label)
        if years is None or "experience" not in lk and "year" not in lk:
            return None
        skills = lk - GENERIC_EXP - {"hands", "handson", "exposure", "using", "knowledge", "working", "worked"}
        if not skills:
            return None                     # "total years of experience" -> profile / memory
        known = {w.lower() for s in self.profile.get("skills") or [] for w in re.findall(r"[a-z0-9+#]+", s.lower())}
        on_resume = [w for w in skills if w in known or re.search(rf"(?<![a-z]){re.escape(w)}(?![a-z])", self._resume_lc)]
        yes = next((o for o in f.options if o.strip().lower() == "yes"), None)
        if on_resume:
            value = yes if yes and "year" not in lk else _closest_option(str(years), f.options)
            return Answer(value, 0.95, "profile") if value else None
        unknown = exp.get("unknown_skill_years", "ask")
        if str(unknown).lower() == "ask":
            return None
        no = next((o for o in f.options if o.strip().lower() == "no"), None)
        value = no if no and "year" not in lk else _closest_option(str(unknown), f.options)
        return Answer(value, 0.9, "profile") if value else None

    def _from_memory(self, f: Field) -> Answer | None:
        entry, score = self.memory.find(f.label)
        if not entry or score < 0.65:
            return None
        value = _closest_option(entry.answer, f.options) if f.kind != "checkbox" else entry.answer
        if not value:
            return None
        self.memory.used(entry)
        # exact match -> auto-fill; close match -> shown to you with the remembered question
        return Answer(value, 0.9 if score >= 0.9 else round(0.5 + score * 0.3, 2),
                      "memory" if score >= 0.9 else f"memory~ “{entry.question[:50]}”")

    def _from_llm(self, f: Field, detail: JobDetail) -> Answer | None:
        if not self.llm:
            return None
        profile = {k: v for k, v in self.profile.items() if k != "answers"}
        past = "\n".join(f"Q: {e.question}\nA: {e.answer}" for e in self.memory.similar(f.label))
        user = (
            f"CANDIDATE PROFILE:\n{yaml.safe_dump(profile, sort_keys=False)}\n"
            f"RESUME:\n{self.resume_text}\n\n"
            f"PAST ANSWERS:\n{past or '(none yet)'}\n\n"
            f"JOB: {detail.job.title} at {detail.job.company}\n\n"
            f"QUESTION: {f.label}\nKIND: {f.kind}\nOPTIONS: {f.options or 'free text'}"
        )
        try:
            data = self.llm.json(SYSTEM, user)
        except LLMUnavailable:
            return None
        except Exception as e:
            print(f"  [llm error] {e}")
            return None
        value = str(data.get("value", "")).strip()
        confidence = float(data.get("confidence", 0) or 0)
        if f.kind != "checkbox":
            mapped = _closest_option(value, f.options)
            if mapped is None:
                return Answer(value, 0.0, "llm")
            value = mapped
        return Answer(value, confidence, "llm") if value else None
