"""Splash logo dual-tier cho ``ctf menu`` — DECISION_LOGO.md §4 (chốt 08/2026).

Hai candidate đã chốt, nhúng NGUYÊN VĂN dạng hằng text (KHÔNG gọi pyfiglet lúc
runtime — không thêm dependency động; render đã duyệt rồi):

- ``_SPLASH_BIG``    — cand_1 ``ctf-toolkit_big_boxframe``: font ``big`` tên đầy
  đủ CTF-TOOLKIT trong khung box-drawing + scanline ``░▒▓``, 78 cột × 13 dòng,
  dùng cho terminal ≥ :data:`WIDE_THRESHOLD` (80) cột.
- ``_SPLASH_NARROW`` — cand_6 ``ctf-toolkit_pagga_compact``: cùng tên, texture
  pagga HUD rail ``▍``, 46 cột × 6 dòng, dùng cho terminal < 80 cột.

Art thuần text: KHÔNG nhúng màu/ANSI vào chuỗi. Muốn accent, áp style token
(:mod:`ctf_downloader.ui.theme`) ở lớp render — ví dụ
``con.print(splash(), style=ACCENT)`` — không trộn vào art string.

Splash chỉ in ĐÚNG MỘT LẦN khi vào menu (:func:`interactive_menu.launch_interactive_menu`),
trước radar AppHeader đầu tiên của vòng lặp (pattern P2 — chi phí 13 dòng hợp lý
vì xuất hiện một lần/phiên); các lệnh framed giữ nguyên radar 4 dòng.
"""
from __future__ import annotations

import shutil

from rich.text import Text

#: Ngưỡng cột chọn tier (DECISION_LOGO.md §4): ≥ 80 → cand_1 big, < 80 → cand_6.
WIDE_THRESHOLD = 80

#: cand_1 — nguồn chuẩn: uiv3_logo/designs/cand_1_ctf-toolkit_big_boxframe.txt
_SPLASH_BIG = r"""┌────────────────────────────────────────────────────────────────────────────╮
│                                                                            │
│    _____ _______ ______   _______ ____   ____  _      _  _______ _______   │
│   / ____|__   __|  ____| |__   __/ __ \ / __ \| |    | |/ /_   _|__   __|  │
│  | |       | |  | |__ ______| | | |  | | |  | | |    | ' /  | |    | |     │
│  | |       | |  |  __|______| | | |  | | |  | | |    |  <   | |    | |     │
│  | |____   | |  | |         | | | |__| | |__| | |____| . \ _| |_   | |     │
│   \_____|  |_|  |_|         |_|  \____/ \____/|______|_|\_\_____|  |_|     │
│                                                                            │
│                  v3◢ ── bộ kit tác chiến capture-the-flag                  │
│                                                                            │
└────────────────────────────────────────────────────────────────────────────╯
░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓
"""

#: cand_6 — nguồn chuẩn: uiv3_logo/designs/cand_6_ctf-toolkit_pagga_compact.txt
_SPLASH_NARROW = r"""▍ ░█▀▀░▀█▀░█▀▀░░░░░▀█▀░█▀█░█▀█░█░░░█░█░▀█▀░▀█▀
▍ ░█░░░░█░░█▀▀░▄▄▄░░█░░█░█░█░█░█░░░█▀▄░░█░░░█░
▍ ░▀▀▀░░▀░░▀░░░░░░░░▀░░▀▀▀░▀▀▀░▀▀▀░▀░▀░▀▀▀░░▀░
▍
▍   v3◢ ── bộ kit tác chiến capture-the-flag
▍ ░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒▓░▒
"""


def _tier_art(width: int) -> str:
    """Chọn hằng art theo tier chiều rộng (thuần tra bảng, không I/O)."""
    return _SPLASH_BIG if width >= WIDE_THRESHOLD else _SPLASH_NARROW


def splash(width: int | None = None) -> Text:
    """Splash logo theo tier chiều rộng terminal (DECISION_LOGO.md §4).

    Trả về :class:`rich.text.Text` thuần text KHÔNG style — caller in qua
    console theme và tự quyết accent. ``width=None`` đo terminal qua
    :func:`shutil.get_terminal_size`; non-TTY trả fallback an toàn nên vẫn
    render plain không crash. ≥ 80 cột → cand_1 (78×13), < 80 → cand_6 (46×6).
    """
    try:
        cols = int(width or shutil.get_terminal_size().columns)
    except Exception:  # pragma: no cover — môi trường không đo được kích thước
        cols = int(width or WIDE_THRESHOLD)
    out = Text()
    for i, line in enumerate(_tier_art(cols).splitlines()):
        if i:
            out.append("\n")
        out.append(line)
    return out


__all__ = ["splash", "WIDE_THRESHOLD"]
