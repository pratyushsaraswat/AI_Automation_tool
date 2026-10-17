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

    def confirm_apply(self, detail: JobDetail, score: int, reason: str) -> bool:
        job = detail.job
        console.print(Panel(
            f"[bold]{job.title}[/] — {job.company}\n"
            f"{job.location} · {job.experience}\n"
            f"Match: [bold]{score}[/]/100 — {reason}\n{job.url}",
            title="Job", border_style="cyan",
        ))
        if not self.confirm_before_apply:
            return True
        choice = console.input("[cyan][Enter][/] apply  [cyan]s[/] skip  [cyan]q[/] quit > ").strip().lower()
        if choice == "q":
            raise QuitRun()
        return choice != "s"

    def confirm_answer(self, f: Field, ans: Answer | None) -> str:
        if (ans and self.auto_accept_known and ans.source in ("profile", "cache")
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
