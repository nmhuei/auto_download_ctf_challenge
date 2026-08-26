"""Selection-state component (SPEC UI v2 §S1) — visual only.

KHÔNG raw-mode / termios: điều hướng vẫn là phím số + ``input()``;
``❯`` chỉ là dấu trạng thái của item mặc định / đang dùng, không phải
con trỏ đọc bàn phím.

Mọi hàm trả :class:`rich.text.Text` thuần — không tự print — để caller
ghép vào Group và test assert trực tiếp.
"""

from __future__ import annotations

from rich.cells import cell_len
from rich.text import Text

from .theme import FG_BASE

#: Cursor selection — chỉ render trên dòng đang được đánh dấu selected.
MENU_CURSOR = "❯"


def selected_row(
    label: str,
    *,
    selected: bool = False,
    done: bool = False,
    width: int | None = None,
) -> Text:
    """Một dòng trong list chọn theo token §S1.

    - ``selected`` → prefix ``❯ `` và toàn bộ nội dung mang token ``sel``
      (reverse ``#14100A on #FFB000``); ``width`` đệm space để nền reverse
      phủ đủ số cột (đo bằng ``cell_len``, an toàn East-Asian width).
    - không selected → prefix đúng 2 space, KHÔNG glyph; label fg.base.
    - ``done`` → label mang token ``done`` (strike muted) cho item đã
      giải hết; selected ưu tiên hơn done khi cả hai cùng đặt.
    """
    t = Text()
    if selected:
        t.append(f"{MENU_CURSOR} ", style="sel")
        body = label
        if width is not None:
            body += " " * max(0, width - cell_len(label))
        t.append(body, style="sel")
    else:
        t.append("  ")
        t.append(label, style="done" if done else FG_BASE)
    return t


__all__ = ["MENU_CURSOR", "selected_row"]
