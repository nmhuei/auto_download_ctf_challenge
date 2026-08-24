import sys
from rich.console import Console
from rich.theme import Theme
from rich.panel import Panel
from rich.table import Table

custom_theme = Theme({
    "info": "cyan",
    "warning": "yellow",
    "error": "bold red",
    "success": "bold green",
    "highlight": "bold magenta",
    "dim": "dim white",
})

console = Console(theme=custom_theme)

class Logger:
    @staticmethod
    def info(msg: str):
        console.print(f"[bold cyan][*][/bold cyan] {msg}")

    @staticmethod
    def success(msg: str):
        console.print(f"[bold green][+][/bold green] {msg}")

    @staticmethod
    def warning(msg: str):
        console.print(f"[bold yellow][!][/bold yellow] {msg}")

    @staticmethod
    def error(msg: str):
        console.print(f"[bold red][-][/bold red] {msg}")

    @staticmethod
    def step(step_num: int, total_steps: int, msg: str):
        console.print(f"[bold magenta][{step_num}/{total_steps}][/bold magenta] {msg}")

    @staticmethod
    def banner():
        """Banner PHOSPHOR FIELD KIT — phương án B half-block (spec §2)."""
        from rich.console import Group

        from ..ui.banner import banner_b, tagline_text
        console.print(Group(banner_b(), tagline_text()))

    @staticmethod
    def print_table(title: str, columns: list, rows: list):
        table = Table(title=title, show_header=True, header_style="bold magenta")
        for col in columns:
            table.add_column(col)
        for row in rows:
            table.add_row(*row)
        console.print(table)
