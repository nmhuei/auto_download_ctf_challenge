"""Atomic file I/O helpers with optional advisory locking (fcntl)."""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Union

PathLike = Union[str, Path]


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def atomic_write_text(path: PathLike, text: str) -> None:
    """Ghi text một cách nguyên tử: ghi `<name>.tmp` rồi os.replace."""
    p = Path(path)
    tmp = _tmp_path(p)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, p)


def atomic_write_json(path: PathLike, obj) -> None:
    """Ghi JSON một cách nguyên tử (indent=2, ensure_ascii=False)."""
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False))


def locked_update_json(path: PathLike, mutator: Callable[[dict], Union[dict, None]]) -> dict:
    """
    Đọc-mutate-ghi JSON dưới khóa độc quyền fcntl.flock(LOCK_EX).

    Lock được giữ trên LOCKFILE RIÊNG `<name>.lock` (không lock file đích),
    vì os.replace thay thế inode của file đích — flock trên file đích sẽ
    mất tác dụng sau lần replace đầu tiên. Tmp file được tạo với tên unique
    (tempfile.mkstemp trong cùng thư mục) để các process không ghi đè tmp
    của nhau.

    - File hỏng (JSON không parse được): nội dung cũ được copy sang
      `<name>.bak` trước khi ghi đè, và state hiện tại coi như `{}`.
    - File TỒN TẠI nhưng KHÔNG ĐỌC ĐƯỢC (PermissionError / OSError):
      ABORT — raise OSError lên caller, KHÔNG ghi đè (không có gì để
      backup an toàn, ghi đè sẽ phá dữ liệu gốc vĩnh viễn).
    - BOM UTF-8 ở đầu file được tự động bỏ qua (utf-8-sig).
    - Gọi mutator(state); nếu mutator trả None thì giữ nguyên state.
    - Ghi lại bằng atomic write (trong phạm vi lock), trả về dict cuối cùng.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    lock_path = p.with_name(p.name + ".lock")

    with open(lock_path, "w") as lock_f:
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            raw = ""
            if p.exists():
                try:
                    raw = p.read_text(encoding="utf-8-sig")
                except OSError as exc:
                    # Không đọc được -> không có nội dung nào để backup an toàn.
                    # Abort mutation thay vì coi file là rỗng rồi ghi đè.
                    raise OSError(
                        f"locked_update_json: không đọc được {p} "
                        f"({exc.__class__.__name__}) — abort để tránh mất dữ liệu"
                    ) from exc

            data: dict = {}
            if raw.strip():
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict):
                        data = parsed
                except (json.JSONDecodeError, ValueError):
                    # File hỏng: backup nội dung cũ sang .bak trước khi ghi đè
                    try:
                        p.with_name(p.name + ".bak").write_text(raw, encoding="utf-8")
                    except OSError:
                        pass
                    data = {}

            result = mutator(data)
            if result is not None and isinstance(result, dict):
                data = result

            # Atomic write trong phạm vi lock, tmp UNIQUE trong cùng thư mục.
            fd, tmp_name = tempfile.mkstemp(
                dir=str(p.parent), prefix=p.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    tf.write(json.dumps(data, indent=2, ensure_ascii=False))
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp_name, p)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

            return data
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
