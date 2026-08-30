"""Persistent global configuration (~/.config/ctf_toolkit/config.json).

Chuyển nguyên văn từ ctf_downloader/interactive_menu.py (Phase 3 rebuild);
interactive_menu re-export lại để giữ tương thích.

Hunt-c18 BUG-2: mọi GHI đi qua storage/fileio (tmp unique + fsync +
os.replace + flock trên lockfile riêng) thay vì open(path,'w') thô —
crash giữa chừng từng truncate vĩnh viễn config, hai process ghi đồng thời
từng lost-update lẫn nhau. :func:`update_global_config` cung cấp đường
đọc-mutate-ghi TRONG CÙNG khóa cho các caller cần re-validate state mới
nhất ngay trước khi ghi (vd register rate-limit anti-TOCTOU).
"""
import copy
import os
import json
from typing import Any, Callable, Dict, Optional

from .fileio import locked_update_json, locked_write_text
from ..utils.logger import Logger

CONFIG_DIR = os.path.expanduser('~/.config/ctf_toolkit')
GLOBAL_CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')
DEFAULT_WORKSPACE_ROOT = os.path.expanduser('~/Workspace/CTF')

#: Defaults seed khi file thiếu key (file mới / hỏng được backup rồi reset).
_DEFAULT_CONFIG: Dict[str, Any] = {
    'workspaces': {},
    'default_workspace': None,
    'workspace_root': DEFAULT_WORKSPACE_ROOT,
    'auth': {},
}


def load_global_config() -> Dict[str, Any]:
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    # Literal mới mỗi lần gọi (không trả alias của _DEFAULT_CONFIG —
    # caller mutate dict trả về không được làm bẩn defaults dùng chung).
    return copy.deepcopy(_DEFAULT_CONFIG)


def resolve_workspace_root() -> str:
    """Root mặc định cho mọi workspace CTF, có thể đổi bằng ``ctf config``.

    Default ``~/Workspace/CTF`` được expand tại THỜI ĐIỂM GỌI thay vì dùng
    path đã đóng băng lúc import module. Điều này giữ đúng semantics khi HOME
    thay đổi trong container/test/chroot; một workspace_root custom trong
    config vẫn được tôn trọng nguyên vẹn.
    """
    cfg = load_global_config()
    raw = cfg.get('workspace_root')
    if not raw or str(raw) == str(DEFAULT_WORKSPACE_ROOT):
        raw = '~/Workspace/CTF'
    return os.path.abspath(os.path.expanduser(str(raw)))


def save_global_config(cfg: Dict[str, Any]) -> bool:
    """Ghi config NGUYÊN TỬ dưới khóa flock (hunt-c18 BUG-2).

    Tái sử dụng :func:`fileio.locked_write_text`: tmp UNIQUE trong cùng
    thư mục + fsync + os.replace, khóa trên lockfile ``config.json.lock``
    — CÙNG họ khóa với :func:`update_global_config` nên hai đường ghi
    loại trừ lẫn nhau (không còn lost update đa process).

    Giữ contract cũ: lỗi ghi KHÔNG raise — log warning (menu/CLI không vì
    ENOSPC mà crash giữa chừng). Trả True khi đã ghi, False khi bỏ qua/lỗi.
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        return bool(locked_write_text(
            GLOBAL_CONFIG_FILE,
            json.dumps(cfg, indent=2, ensure_ascii=False)))
    except Exception as e:
        Logger.warning(f'Không lưu được config: {e}')
        return False


def update_global_config(
    mutator: Callable[[Dict[str, Any]], Optional[Dict[str, Any]]]
) -> Optional[Dict[str, Any]]:
    """Đọc-mutate-ghi global config trong CÙNG một khóa flock.

    ``mutator(state)`` nhận state HIỆN HÀNH trên đĩa (đã seed defaults cho
    key thiếu) và trả về:
      - dict mới  -> ghi nguyên tử trong phạm vi khóa;
      - None      -> giữ state hiện hành (vẫn ghi lại để persist seeds);
      - ``fileio.SKIP_WRITE`` -> BỎ qua toàn bộ lần ghi (state trên đĩa
        giữ nguyên byte-in-byte) — dùng khi caller thua cuộc TOCTOU.

    Trả về state sau khi ghi; None khi bị SKIP hoặc thư mục chứa file đã
    biến mất. File hỏng: nội dung cũ được backup sang ``.bak`` trước khi
    khởi tạo lại từ defaults (hành vi locked_update_json).
    """
    os.makedirs(CONFIG_DIR, exist_ok=True)

    def _seeded(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        # deepcopy: setdefault với object lồng nhau DÙNG CHUNG của
        # _DEFAULT_CONFIG sẽ để mutator làm bẩn defaults cấp module và
        # "nhớ" dữ liệu của lần gọi trước sang config khác.
        for key, value in _DEFAULT_CONFIG.items():
            data.setdefault(key, copy.deepcopy(value))
        return mutator(data)

    return locked_update_json(GLOBAL_CONFIG_FILE, _seeded)
