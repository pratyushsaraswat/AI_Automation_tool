"""Terminal review: you approve every job and every uncertain answer."""
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from src.core.models import Answer, Field, JobDetail, QuitRun, SkipJob

console = Console()


class Reviewer:
    def __init__(self, review_cfg: dict):
        self.confirm_before_apply = review_cfg.get("confirm_before_apply", True)
        self.auto_accept_known = review_cfg.get("auto_accept_known", True)
        self.threshold = float(review_cfg.get("auto_accept_confidence", 0.85))
        self.auto_submit = review_cfg.get("auto_submit_known", False)   # --auto: submit when nothing is unknown

    def confirm_apply(self, detail: JobDetail, score: int, reason: str, low_match: bool = False) -> bool:
        """low_match: score is under min_match_score but the title is a software role — always ask,
        and default to skip so a stray Enter doesn't apply."""
        job = detail.job
        where = "[magenta]external — company site[/]" if detail.external else f"[green]{job.site}[/]"
        color = "yellow" if low_match else "bold"
        console.print(Panel(
            f"[bold]{job.title}[/] — {job.company}   ({where})\n"
            f"{job.location} · {job.experience}\n"
            f"Match: [{color}]{score}[/]/100 — {reason}\n{job.url}",
            title="Job — [yellow]LOW MATCH[/]" if low_match else "Job",
            border_style="yellow" if low_match else "cyan",
        ))
        if low_match:
            choice = console.input("Low match — apply anyway? [cyan]y[/] apply  [cyan][Enter][/] skip  "
                                   "[cyan]q[/] quit > ").strip().lower()
            if choice == "q":
                raise QuitRun()
            return choice == "y"
        if not self.confirm_before_apply:
            return True
        choice = console.input("[cyan][Enter][/] apply  [cyan]s[/] skip  [cyan]q[/] quit > ").strip().lower()
        if choice == "q":
            raise QuitRun()
        return choice != "s"

    def choose(self, prompt: str, choices: str, default: str = "") -> str:
        """Single-letter prompt, e.g. choose('Finished?', 'dsq'). q always quits.
        Enter returns default when that letter is one of the choices."""
        while True:
            raw = console.input(f"{prompt} > ").strip().lower()[:1]
            if raw == "q":
                raise QuitRun()
            if not raw and default in choices:
                return default
            if raw and raw in choices:
                return raw

    def confirm_answer(self, f: Field, ans: Answer | None, force: bool = False) -> str:
        if (not force and ans and self.auto_accept_known and ans.source in ("profile", "memory")
                and ans.confidence >= self.threshold):
            console.print(f"  [green]✓[/] {f.label} → [bold]{ans.value}[/] [dim]({ans.source})[/]")
            return ans.value

        table = Table(show_header=False, box=None)
        table.add_row("Question", f"[bold]{f.label}[/]")
        if f.options:
            table.add_row("Options", "\n".join(f"{i}. {o}" for i, o in enumerate(f.options, 1)))
        if ans:
            color = "green" if ans.confidence >= self.threshold else "yellow" if ans.confidence >= 0.5 else "red"
            table.add_row("Proposed", f"[{color}]{ans.value}[/] [dim]({ans.source}, {ans.confidence:.2f})[/]")
        else:
            table.add_row("Proposed", "[red]no answer found — type one[/]")
        console.print(Panel(table, title="Check answer", border_style="yellow"))

        while True:
            hint = "[Enter] accept  " if ans else ""
            raw = console.input(f"{hint}type new value / option number  s skip job  q quit > ").strip()
            if raw.lower() == "q":
                raise QuitRun()
            if raw.lower() == "s":
                raise SkipJob()
            if not raw and ans:
                return ans.value
            if raw.isdigit() and f.options and 1 <= int(raw) <= len(f.options):
                return f.options[int(raw) - 1]
            if raw:
                return raw
            console.print("  [yellow]No answer yet — type it" + (" or an option number" if f.options else "")
                          + ", or s to skip this job.[/]")
