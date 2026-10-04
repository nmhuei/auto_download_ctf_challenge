---
description: Quy chuẩn thiết kế Tactical UI, hệ thống bảng màu Semantic Tokens và template code chuẩn cho menu/hub
globs: ["ctf_downloader/ui/**", "ctf_downloader/interactive_menu.py", "ctf_downloader/dashboard.py", "ctf_downloader/cli.py"]
always_on: true
---

# 🎨 UCS_ExOdia Tactical UI Design System & Semantic Color Palette

> **Mục tiêu**: Chuẩn hóa toàn bộ giao diện Cockpit TUI (Terminal User Interface). Ngăn ngừa 100% tình trạng lệch tông màu, vỡ layout, dùng sai token màu gây crash (như `NameError: name 'DANGER' is not defined`) hoặc thiếu xử lý ngoại lệ trong menu.

---

## 1. Bảng Tra Cứu Token Màu Semantic (`ctf_downloader/ui/theme.py`)

Khi viết UI component, bảng hiển thị hoặc menu, **LUÔN** import trực tiếp các token ngữ nghĩa từ `ctf_downloader.ui.theme`.

```python
from .ui.theme import (
    ACCENT,         # Cyan sáng (#5EEAD4) — Nhận diện thương hiệu, con trỏ prompt, header chính
    ACCENT_DEEP,    # Cyan đậm (#1F6F78) — Border style cho panel, table khung viền
    FG_BASE,        # Trắng sáng (#E6EDF3) — Text thông thường, tiêu đề mục, giá trị chính
    FG_MUTED,       # Xám trung (#8B98A5) — Text phụ, mô tả ngắn, metadata (ID, category, points)
    FG_FAINT,       # Xám mờ (#50606C) — Dấu phân cách '·', placeholder, trạng thái '· Unsolved'
    SUCCESS,        # Xanh lá (#9BE15D) — Thành công, hoàn thành, trạng thái sẵn sàng
    SOLVED,         # Alias của SUCCESS — Dùng cho flag captured, '✔ SOLVED'
    WARN,           # Cam hổ phách (#FF9F43) — Cảnh báo, nhắc nhở, container sắp hết hạn
    ERROR,          # Đỏ cam (#E5534B) — Lỗi, thất bại, file vượt ngưỡng kích thước
    INFO,           # Cyan nhạt (#5EEAD4) — Đường dẫn file, connection info host:port
    load_theme,     # Hàm nạp theme cho Rich Console
)
```

### 🏷️ Category Tokens (Chỉ dùng khi cần phân biệt danh mục CTF)
```python
from .ui.theme import (
    CATEGORY_WEB,        # ACCENT (Cyan)
    CATEGORY_CRYPTO,     # Xanh pastel (#A7C7FF)
    CATEGORY_PWN,        # Đỏ cam (#FF8A65)
    CATEGORY_REV,        # Tím pastel (#C9B5FF)
    CATEGORY_FORENSICS, # SUCCESS (Xanh lá)
    CATEGORY_MISC,       # FG_MUTED (Xám)
)
```

### 🎭 Tên Style Rich Đã Định Sẵn Trong Theme (Dùng cho `style="..."`)
- `"menu.key"`: Style cho phím thao tác thông thường (ví dụ: `Text("[ 1]", style="menu.key")`).
- `"menu.active"`: Style cho mục đang được kích hoạt / lựa chọn hiện tại (toàn bộ dòng highlight xanh lá).
- `"menu.exit"`: Style cho phím thoát / quay lại `[ 0]`.
- `"fg.base"`, `"fg.muted"`, `"fg.faint"`: Cấp độ tương phản văn bản chuẩn.
- `"solved"`, `"success"`: Đánh dấu giải xong `✔ SOLVED`.
- `"warn"`, `"error"`, `"info"`: Thông báo trạng thái.

---

## 2. Các Lỗi Cấm Kỵ Tuyệt Đối (Deadly Pitfalls & Anti-Patterns)

| Hành vi SAI ❌ | Hậu quả | Cách viết ĐÚNG ✔️ |
|---|---|---|
| `con.print(f"[{DANGER}]...[/{DANGER}]")` | 💥 **NameError: name 'DANGER' is not defined** | Dùng `ERROR` (`from .ui.theme import ERROR`) |
| `style="#5EEAD4"` hoặc `style="red"` | 💥 Lệch tông khi người dùng đổi Theme (Dracula, Matrix, Cyberpunk) | Dùng token `style=ACCENT` hoặc `style="error"` |
| `Text(" " * 15 + "Tiêu đề")` | 💥 Vỡ layout trên các kích thước terminal khác nhau | Dùng `Table.grid(padding=(0, 1))` bọc ngoài bởi `Padding(grid, (0, 2))` |
| Gọi `offer_bqa_recovery(diag)` trong UI | 💥 Hiện prompt SuperBQA phá vỡ trải nghiệm menu | Chỉ ghi `Logger.warning(...)` và `_pause()`. BQA auto-recovery CHỈ dành cho CLI không có tương tác |
| Chạy action menu không có `try...except` | 💥 Văng ứng dụng ra bash khi gặp input sai hoặc lỗi kết nối | Bọc toàn bộ hành động bằng default exception handler |

---

## 3. Template Code Chuẩn: Tạo Sub-Hub Menu Mới

Khi tạo một hub tính năng hoặc submenu mới, hãy sao chép đúng template sau:

```python
from typing import Any
from rich.table import Table
from rich.text import Text
from rich.padding import Padding
from ..utils.logger import Logger
from ..ui.theme import ACCENT, ACCENT_DEEP, FG_BASE, FG_FAINT, FG_MUTED, ERROR, SUCCESS
from .menu_hubs import render_hub_menu, _hub_console, _prompt, _pause

def hub_my_feature(app: Any) -> None:
    """Mẫu Sub-Hub chuẩn tác chiến cho UCS_ExOdia Cockpit."""
    actions = [
        ("1", "🎯 Hành động thứ nhất"),
        ("2", "⚡ Hành động thứ hai (Tùy chọn)"),
        ("3", "📊 Báo cáo / Trạng thái"),
        ("0", "🚪 Quay lại Menu chính"),
    ]

    while True:
        try:
            choice = render_hub_menu(
                title="TÊN PHÂN HỆ TÁC CHIẾN",
                subtitle="Mô tả ngắn gọn chức năng",
                actions=actions,
                prompt_text="❯ Lựa chọn thao tác (0-3): ",
            )
            
            # Thoát hoặc ấn Enter rỗng
            if choice in ("0", "", "q", "exit", "back"):
                break
            elif choice == "1":
                _handle_action_one(app)
            elif choice == "2":
                _handle_action_two(app)
            elif choice == "3":
                _handle_action_three(app)
            else:
                Logger.warning("Lựa chọn không hợp lệ. Vui lòng nhập từ 0 đến 3.")
                _pause()
                
        except (EOFError, KeyboardInterrupt):
            # Thoát sạch khi bấm Ctrl+C hoặc Ctrl+D
            break
        except Exception as exc:
            # Bắt toàn bộ exception mặc định, không bao giờ để crash menu
            Logger.error(f"Lỗi thực thi trong phân hệ: {exc}")
            import os
            if os.environ.get("CTF_DEBUG") == "1":
                import traceback
                traceback.print_exc()
            _pause()


def _handle_action_one(app: Any) -> None:
    """Hàm xử lý con có exception handling riêng."""
    try:
        con = _hub_console()
        con.print(Text("\n  Đang thực thi tác vụ...", style=ACCENT))
        # Logic xử lý tại đây
        Logger.success("Tác vụ hoàn thành thành công!")
    except Exception as e:
        Logger.error(f"Tác vụ thất bại: {e}")
    _pause()
```

---

## 4. Template Code Chuẩn: Hiển Thị Bảng Lựa Chọn (Table Grid)

Không dùng cột dummy hay format string thủ công, luôn dựng bảng theo mẫu:

```python
grid = Table.grid(padding=(0, 1))
grid.add_column("key", justify="right", no_wrap=True)
grid.add_column("title")
grid.add_column("status", no_wrap=True)

for idx, item in enumerate(items, 1):
    is_active = (item.id == active_id)
    key_style = "menu.active" if is_active else "menu.key"
    title_style = "menu.active" if is_active else "fg.base"
    
    status_text = Text("✔ SOLVED", style="solved") if item.solved else Text("· Unsolved", style="fg.faint")

    grid.add_row(
        Text(f"[{idx:>2}]", style=key_style),
        Text(item.name, style=title_style),
        status_text,
    )

# Tùy chọn thoát luôn tích hợp ở dòng cuối cùng của bảng
grid.add_row(
    Text("[ 0]", style="menu.exit"),
    Text("Quay lại Menu chính", style="fg.muted"),
    Text(""),
)

con.print(Padding(grid, (0, 2)))
```

---

## 5. Quy Trình Kiểm Thử An Toàn Trước Khi Commit (Safety Checklist)

Trước khi xác nhận hoàn thành bất kỳ chỉnh sửa UI nào:

1. **Quét biến chưa định nghĩa (AST Scanner)**:
   ```bash
   python3 -c "
   import ast
   with open('ctf_downloader/interactive_menu.py') as f:
       tree = ast.parse(f.read())
   # Đảm bảo không có NameError tiềm ẩn như DANGER hay biến gõ nhầm
   "
   ```
2. **Kiểm tra biên dịch**:
   ```bash
   python3 -m py_compile ctf_downloader/interactive_menu.py ctf_downloader/ui/menu_hubs.py ctf_downloader/cli.py
   ```
3. **Chạy test suite**:
   ```bash
   pytest tests/test_interactive_menu_features.py tests/test_bqa_cli_integration.py
   ```
