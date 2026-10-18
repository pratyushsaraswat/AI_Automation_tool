"""`python -m src.main answers ...` — view, teach and clean the answer memory."""
from rich.console import Console
from rich.table import Table

from src.core.memory import AnswerMemory

console = Console()

# Asked once by `answers learn`; skipped if memory/profile already covers them.
COMMON_QUESTIONS = [
    "What is your current CTC (in LPA)?",
    "What is your expected CTC (in LPA)?",
    "What is your notice period (in days)?",
    "If serving notice, what is your last working day?",
    "Are you an immediate joiner?",
    "Can you join within 30 days?",
    "Is your notice period negotiable / can you buy out?",
    "Are you willing to relocate?",
    "Which locations do you prefer?",
    "Are you comfortable working from office / hybrid?",
    "Are you comfortable with rotational / night shifts?",
    "Are you legally authorized to work in India?",
    "Will you now or in the future require visa sponsorship?",
    "Do you have a valid passport?",
    "How many years of total work experience do you have?",
    "How many years of experience do you have with Java?",
    "How many years of experience do you have with Spring Boot?",
    "How many years of experience do you have with Go?",
    "How many years of experience do you have with React?",
    "How many years of experience do you have with Microservices?",
    "How many years of experience do you have with Kafka?",
    "How many years of experience do you have with AWS?",
    "How many years of experience do you have with Docker / Kubernetes?",
    "How many years of experience do you have with SQL?",
    "What is your highest qualification?",
    "What was your graduation percentage / CGPA?",
    "In which year did you graduate?",
    "Have you previously worked for this company?",
    "Do you have any offers in hand? If yes, what CTC?",
    "Why are you looking for a change?",
    "How did you hear about this job?",
    "What is your gender?",
    "Do you have a disability?",
    "Are you a veteran?",
]


def list_answers(search: str = ""):
    mem = AnswerMemory()
    rows = mem.search(search)
    table = Table(title=f"Answer memory ({len(mem.entries)} saved){f' — matching {search!r}' if search else ''}")
    for col in ("#", "Question", "Answer", "From", "Used"):
        table.add_column(col)
    for i, e in rows:
        table.add_row(str(i + 1), e.question[:70], e.answer[:40], e.source, str(e.uses))
    console.print(table)


def set_answer(question: str, answer: str):
    AnswerMemory().add(question, answer, source="user")
    console.print(f"Saved: {question!r} → {answer!r}")


def delete_answer(number: int):
    mem = AnswerMemory()
    if not 1 <= number <= len(mem.entries):
        console.print(f"No answer #{number}. See `answers list`.")
        return
    e = mem.delete(number - 1)
    console.print(f"Deleted: {e.question!r} → {e.answer!r}")


def learn():
    """One-time questionnaire so most forms fill without stopping."""
    mem = AnswerMemory()
    console.print("[bold]Teach the tool your standard answers.[/] Enter = skip, q = stop.\n")
    taught = 0
    for q in COMMON_QUESTIONS:
        entry, score = mem.find(q)
        known = f" [dim](saved: {entry.answer})[/]" if entry and score >= 0.9 else ""
        if known:
            raw = console.input(f"{q}{known}\n  new answer or Enter to keep > ").strip()
        else:
            raw = console.input(f"{q}\n  > ").strip()
        if raw.lower() == "q":
            break
        if raw:
            mem.add(q, raw, source="learn")
            taught += 1
    console.print(f"\nSaved {taught} answers. Memory now has {len(mem.entries)}.")
