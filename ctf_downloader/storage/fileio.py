"""Atomic file I/O helpers with optional advisory locking (fcntl)."""

from __future__ import annotations

import fcntl
import json
import os
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

    - Mở file ở chế độ "a+" (tạo nếu chưa có), flock LOCK_EX.
    - File hỏng (JSON không parse được): nội dung cũ được copy sang
      `<name>.bak` trước khi ghi đè, và state hiện tại coi như `{}`.
    - Gọi mutator(state); nếu mutator trả None thì giữ nguyên state.
    - Ghi lại bằng atomic write (trong phạm vi lock), trả về dict cuối cùng.
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    with open(p, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            f.seek(0)
            raw = f.read()

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

            # Atomic write trong phạm vi lock: ghi tmp rồi os.replace.
            # Lưu ý: os.replace trên file đang mở "a+" là an toàn trên Linux;
            # descriptor cũ vẫn trỏ tới inode đã bị thay thế và ta đóng ngay sau đó.
            tmp = _tmp_path(p)
            with open(tmp, "w", encoding="utf-8") as tf:
                tf.write(json.dumps(data, indent=2, ensure_ascii=False))
                tf.flush()
                os.fsync(tf.fileno())
            os.replace(tmp, p)

            return data
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
