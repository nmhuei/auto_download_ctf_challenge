from rich.console import Console
from rich.markup import escape
from rich.table import Table

# PHOSPHOR FIELD KIT: một accent amber duy nhất, semantic chỉ đi kèm glyph
# (spec §3). Logger dùng chung theme nguồn sự thật của toàn CLI thay cho
# custom_theme cyan/vàng/xanh legacy; highlight=False tắt ReprHighlighter
# (rich không được tự do tô màu ngoài token).
# stderr=True: log/diagnostic đi stderr theo convention CLI tool — stdout
# giữ cho dữ liệu pipe-able; cùng precedent với interactive_menu._MENU_CON.
from ..ui.theme import load_theme

console = Console(theme=load_theme(None), highlight=False)


def _safe_body(msg: str, markup: bool) -> str:
    """C11-04: msg của Logger hầu như luôn là DỮ LIỆU (tên challenge,
    notice title/body, solver, path... lấy từ server) → mặc định escape để
    '[/]' lạc loài không văng MarkupError CRASH cả lệnh, '[link=…]' không
    inject OSC-8 hyperlink và tag lạ không bị rich nuốt mất chữ. Call-site
    CHỦ Ý trang trí bằng rich markup của chính tool truyền ``markup=True``;
    chrome nội bộ ([*], [+], [!], [-]) nằm trong template f-string nên
    không bao giờ bị đụng tới."""
    if markup:
        return msg
    return escape(msg if isinstance(msg, str) else str(msg))


class Logger:
    @staticmethod
    def info(msg: str, markup: bool = False):
        # [*] là chrome điều hướng → amber tắt đèn (ACCENT_DEEP), không cyan.
        console.print(f"[accent.deep][*][/accent.deep] {_safe_body(msg, markup)}")

    @staticmethod
    def success(msg: str, markup: bool = False):
        # [+] là chrome thuần → amber lead; green chỉ dành cho ngữ nghĩa
        # solve/✔ theo spec §3. Tag lồng để từng tên token resolve qua theme.
        console.print(f"[bold][accent][+][/][/] {_safe_body(msg, markup)}")

    @staticmethod
    def warning(msg: str, markup: bool = False):
        # ! warn → warn amber #EAC54F (token spec, không vàng legacy).
        console.print(f"[bold][warn][!][/][/] {_safe_body(msg, markup)}")

    @staticmethod
    def error(msg: str, markup: bool = False):
        # ✗/- error → đỏ semantic token.
        console.print(f"[bold][error][-][/][/] {_safe_body(msg, markup)}")

    @staticmethod
    def step(step_num: int, total_steps: int, msg: str, markup: bool = False):
        console.print(
            f"[bold][accent][{step_num}/{total_steps}][/][/] {_safe_body(msg, markup)}")

    @staticmethod
    def banner():
        """Banner PHOSPHOR FIELD KIT — phương án B half-block (spec §2)."""
        from rich.console import Group

        from ..ui.banner import banner_b, tagline_text
        console.print(Group(banner_b(), tagline_text()))

    @staticmethod
    def print_table(title: str, columns: list, rows: list):
        # Title/columns cũng là dữ liệu (mode label dựng từ chuỗi server,
        # tên cột cấu hình) — rich Table parse markup trên chúng theo mặc
        # định → escape như cell (hunt-c20 LOW): '[...]' hiện nguyên văn,
        # không inject style/crash bảng. Non-str passthrough.
        table = Table(
            title=escape(title) if isinstance(title, str) else title,
            show_header=True, header_style="title")
        for col in columns:
            table.add_column(escape(col) if isinstance(col, str) else col)
        for row in rows:
            # Cell là dữ liệu (thường lấy từ SERVER: solver_names, tên
            # challenge/category) → escape markup để tên chứa '[...]' hiện
            # NGUYÊN VĂN như text thường, không inject style/làm vỡ bảng
            # (hunter cycle-10). Non-str passthrough (RenderableType).
            table.add_row(*[
                escape(cell) if isinstance(cell, str) else cell
                for cell in row])
        console.print(table)
