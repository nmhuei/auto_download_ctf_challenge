"""Atomic file I/O helpers with optional advisory locking (fcntl)."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import tempfile
from pathlib import Path
from typing import Callable, Iterator, Union

PathLike = Union[str, Path]


class _SkipWrite:
    """Sentinel singleton: mutator của :func:`locked_update_json` trả về
    đối tượng này để BỎ qua lần ghi (state giữ nguyên trên đĩa)."""


SKIP_WRITE = _SkipWrite()


def _tmp_path(path: Path) -> Path:
    return path.with_name(path.name + ".tmp")


def atomic_write_text(path: PathLike, text: str) -> None:
    """Ghi text một cách nguyên tử: ghi `<name>.tmp` rồi os.replace.

    Nếu đích là symlink: ghi vào ĐÍCH THẬT (resolve) thay vì os.replace lên
    path symlink — nếu không, replace sẽ âm thầm thay symlink bằng file thường
    và target gốc không bao giờ được cập nhật.
    """
    p = Path(path)
    if p.is_symlink():
        p = p.resolve()
    tmp = _tmp_path(p)
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    except BaseException:
        # Ghi/replace thất bại (vd ENOSPC): dọn tmp để không để lại rác
        # <name>.tmp (nhất quán với locked_update_json), rồi raise lại.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: PathLike, obj) -> None:
    """Ghi JSON một cách nguyên tử (indent=2, ensure_ascii=False)."""
    atomic_write_text(path, json.dumps(obj, indent=2, ensure_ascii=False))


def locked_write_text(path: PathLike, text: str) -> bool:
    """Ghi text nguyên tử dưới khóa độc quyền fcntl.flock(LOCK_EX).

    Cùng giao thức khóa với locked_update_json (dành cho state JSON):
    - Khóa trên LOCKFILE RIÊNG ``<name>.lock`` — không khóa đích, vì
      os.replace thay thế inode của file nên flock trên đích mất tác dụng
      sau lần replace đầu tiên.
    - Re-validate inode lockfile sau khi grant (chống vùng găng trên inode
      mồ côi khi holder trước vừa unlink).
    - Ghi qua tmp UNIQUE trong cùng thư mục + fsync rồi os.replace.
    - Ghi THÀNH CÔNG: unlink lockfile TRƯỚC khi unlock. THẤT BẠT: giữ lại
      lockfile và dọn tmp.
    - Symlink: resolve sang ĐÍCH THẬT trước khi tính lock path và ghi
      (nhất quán atomic_write_text / locked_update_json).

    Trả về True khi đã ghi, False khi BỎ QUA vì thư mục cha không còn tồn
    tại (BUG-C16-1: sync giữ snapshot của challenge bị xoá giữa chừng —
    mkdir(parents=True) cũ HỒI SINH thư mục và sinh state zombie; giờ bỏ
    ghi thay vì dựng lại. Lần tạo ĐẦU TIÊN hợp lệ do caller tự đảm nhiệm
    việc tạo thư mục — vd WorkspaceBuilder tự os.makedirs trước khi gọi)."""
    p = Path(path)
    if p.is_symlink():
        p = p.resolve()
    if not p.parent.is_dir():
        return False   # không hồi sinh thư mục đã bị xoá
    lock_path = p.with_name(p.name + ".lock")

    while True:
        lock_f = open(lock_path, "w")
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            try:
                st_path = os.stat(lock_path)
                st_mine = os.fstat(lock_f.fileno())
                live = (st_path.st_dev == st_mine.st_dev
                        and st_path.st_ino == st_mine.st_ino)
            except FileNotFoundError:
                live = False
            if not live:
                continue

            # Re-check sau khi chờ khóa: thư mục có thể vừa bị xoa trong lúc
            # ta xếp hàng — skip thay vì ghi vào dir mới dựng lại.
            if not p.parent.is_dir():
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                return False

            fd, tmp_name = tempfile.mkstemp(
                dir=str(p.parent), prefix=p.name + ".", suffix=".tmp"
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as tf:
                    tf.write(text)
                    tf.flush()
                    os.fsync(tf.fileno())
                os.replace(tmp_name, p)
            except BaseException:
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass
                raise

            # Ghi thành công: dọn lockfile TRONG LÚC CÒN GIỮ KHÓA (unlink
            # trước unlock) — process chờ sẽ thấy re-validate thất bại và
            # tự mở lại lockfile hiện hành (nhất quán locked_update_json).
            try:
                os.unlink(lock_path)
            except OSError:
                pass
            return True
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            lock_f.close()


@contextlib.contextmanager
def locked_path(path: PathLike) -> Iterator[Path]:
    """Context manager GIỮ khóa độc quyền trên lockfile ``<name>.lock`` của
    ``path``, yield đường đích (đã resolve symlink) cho caller tự đọc/ghi
    TRONG phạm vi ``with``.

    Dùng cho các giao thức đọc-sửa-viết mà helper đóng gói sẵn
    (locked_write_text / locked_update_json / locked_update_json-text)
    không phủ hết — vd SUMMARY.md: patcher đọc-mutate rồi ghi qua
    atomic_write_text, regenerate ghi đè toàn bộ; cả hai phải chia sẻ
    CÙNG một lock key thì lost update mới không thể xảy ra.

    Giao thức khóa giống hệt locked_write_text / locked_update_json:
    - Khóa trên LOCKFILE RIÊNG ``<name>.lock`` (os.replace thay inode đích
      nên flock trên đích mất tác dụng sau lần replace đầu tiên).
    - Re-validate inode lockfile sau khi grant (chống vùng găng trên inode
      mồ côi khi holder trước vừa unlink).
    - Caller ghi THÀNH CÔNG: lockfile được unlink TRƯỚC khi unlock. Caller
      raise: giữ lại lockfile (nhất quán locked_update_json).

    BUG-C16-1: KHÔNG còn mkdir(parents=True) — nếu thư mục cha không tồn
    tại thì yield mà KHÔNG khóa (không có gì để bảo vệ): mọi ghi của caller
    vào đường đích sẽ fail LOÁ (FileNotFoundError từ writer, không tự tạo
    dir) thay vì hồi sinh thư mục đã bị xoá giữa chừng để sinh file zombie.
    """
    p = Path(path)
    if p.is_symlink():
        p = p.resolve()
    if not p.parent.is_dir():
        yield p   # thư mục cha đã biến mất: không khóa, ghi sẽ fail loud
        return
    lock_path = p.with_name(p.name + ".lock")

    while True:
        lock_f = open(lock_path, "w")
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            try:
                st_path = os.stat(lock_path)
                st_mine = os.fstat(lock_f.fileno())
                live = (st_path.st_dev == st_mine.st_dev
                        and st_path.st_ino == st_mine.st_ino)
            except FileNotFoundError:
                live = False
            if not live:
                continue
            try:
                yield p
            except BaseException:
                # Thất BẠT: giữ lại lockfile như các helper khác.
                raise
            # Thành công: dọn lockfile TRONG LÚC CÒN GIỮ KHÓA (unlink trước
            # unlock) — process chờ sẽ thấy re-validate thất bại và tự mở
            # lại lockfile hiện hành.
            try:
                os.unlink(lock_path)
            except OSError:
                pass
            return
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            lock_f.close()


def locked_update_json(path: PathLike, mutator: Callable[[dict], Union[dict, None]]) -> "dict | None":
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
    - Gọi mutator(state); nếu mutator trả None thì giữ nguyên state; nếu
      trả ``SKIP_WRITE`` thì BỎ QUA toàn bộ lần ghi (state trên đĩa giữ
      nguyên byte-in-byte) và trả None.
    - Ghi lại bằng atomic write (trong phạm vi lock), trả về dict cuối cùng.
    - Ghi THÀNH CÔNG: lockfile `<name>.lock` được unlink (lỗi unlink bỏ qua).
      Unlink thực hiện TRƯỚC khi unlock, kèm re-validate inode sau mỗi lần
      grant: process chờ dính inode mồ côi tự mở lại lockfile hiện hành thay
      vì lọt vào vùng găng song song với process khác (tránh lost update).
      Ghi THẤT BẠT: lockfile được giữ lại.
    - Nếu đích là symlink: resolve sang ĐÍCH THẬT trước khi tính lock path và
      ghi (nhất quán với atomic_write_text) — os.replace lên path symlink sẽ
      thay thế link bằng file thường, target gốc không bao giờ được cập nhật.
      Resolve trước cũng đảm bảo mọi caller qua symlink khác nhau dùng CÙNG
      một lock file (lock của target thật).

    Trả về dict state cuối cùng SAU KHI GHI; None khi bị SKIP — gồm hai
    trường hợp (BUG-C16-1): thư mục cha không còn tồn tại tại lúc ghi
    (không mkdir hồi sinh thư mục challenge bị xoá giữa chừng -> không sinh
    metadata.json zombie), hoặc mutator trả SKIP_WRITE. Lần tạo file ĐẦU
    TIÊN trong thư mục CÓ SẴN vẫn hoạt động như cũ.
    """
    p = Path(path)
    if p.is_symlink():
        p = p.resolve()
    if not p.parent.is_dir():
        return None   # không hồi sinh thư mục đã bị xoá
    lock_path = p.with_name(p.name + ".lock")

    while True:
        lock_f = open(lock_path, "w")
        fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
        try:
            # Re-validate: holder trước có thể vừa unlink lockfile (dọn cuối
            # phiên) ngay trước khi ta được grant — khi đó flock của ta đang
            # trên inode MỒ CÔI, không còn loại trừ lẫn nhau với process mở
            # inode mới. Nếu lock path không trúng inode ta giữ -> bỏ lượt,
            # mở lại theo inode hiện hành.
            try:
                st_path = os.stat(lock_path)
                st_mine = os.fstat(lock_f.fileno())
                live = (st_path.st_dev == st_mine.st_dev
                        and st_path.st_ino == st_mine.st_ino)
            except FileNotFoundError:
                live = False
            if not live:
                continue

            # Re-check sau khi chờ khóa: thư mục có thể vừa bị xoá trong lúc
            # ta xếp hàng — skip thay vì dựng lại dir rồi ghi zombie.
            if not p.parent.is_dir():
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                return None

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
            if result is SKIP_WRITE:
                # Mutator quyết định không đổi gì: bỏ qua ghi, dọn lockfile
                # như nhánh thành công (không để lại rác .lock).
                try:
                    os.unlink(lock_path)
                except OSError:
                    pass
                return None
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

            # Ghi thành công: dọn lockfile TRONG LÚC CÒN GIỮ KHÓA (unlink
            # trước unlock). Process bị chặn trên inode này sẽ được grant
            # sau khi ta unlock, thấy re-validate inode thất bại và tự mở
            # lại lockfile hiện hành — nhờ vậy không có hai process nào cùng
            # ở trong vùng găng trên hai inode khác nhau.
            try:
                os.unlink(lock_path)
            except OSError:
                pass
            return data
        finally:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
            lock_f.close()
