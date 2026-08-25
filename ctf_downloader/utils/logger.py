import sys
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

# PHOSPHOR FIELD KIT: một accent amber duy nhất, semantic chỉ đi kèm glyph
# (spec §3). Logger dùng chung theme nguồn sự thật của toàn CLI thay cho
# custom_theme cyan/vàng/xanh legacy; highlight=False tắt ReprHighlighter
# (rich không được tự do tô màu ngoài token).
from ..ui.theme import load_theme

console = Console(theme=load_theme(None), highlight=False)

class Logger:
    @staticmethod
    def info(msg: str):
        # [*] là chrome điều hướng → amber tắt đèn (ACCENT_DEEP), không cyan.
        console.print(f"[accent.deep][*][/accent.deep] {msg}")

    @staticmethod
    def success(msg: str):
        # [+] là chrome thuần → amber lead; green chỉ dành cho ngữ nghĩa
        # solve/✔ theo spec §3. Tag lồng để từng tên token resolve qua theme.
        console.print(f"[bold][accent][+][/][/] {msg}")

    @staticmethod
    def warning(msg: str):
        # ! warn → warn amber #EAC54F (token spec, không vàng legacy).
        console.print(f"[bold][warn][!][/][/] {msg}")

    @staticmethod
    def error(msg: str):
        # ✗/- error → đỏ semantic token.
        console.print(f"[bold][error][-][/][/] {msg}")

    @staticmethod
    def step(step_num: int, total_steps: int, msg: str):
        console.print(f"[bold][accent][{step_num}/{total_steps}][/][/] {msg}")

    @staticmethod
    def banner():
        """Banner PHOSPHOR FIELD KIT — phương án B half-block (spec §2)."""
        from rich.console import Group

        from ..ui.banner import banner_b, tagline_text
        console.print(Group(banner_b(), tagline_text()))

    @staticmethod
    def print_table(title: str, columns: list, rows: list):
        table = Table(title=title, show_header=True, header_style="title")
        for col in columns:
            table.add_column(col)
        for row in rows:
            # Cell là dữ liệu (thường lấy từ SERVER: solver_names, tên
            # challenge/category) → escape markup để tên chứa '[...]' hiện
            # NGUYÊN VĂN như text thường, không inject style/làm vỡ bảng
            # (hunter cycle-10). Non-str passthrough (RenderableType).
            table.add_row(*[
                escape(cell) if isinstance(cell, str) else cell
                for cell in row])
        console.print(table)
