"""Edge tests cho lệnh ``ctf config`` (spec-gap S3 — event-window §4).

Phủ biên ngoài test_f3 (hunter c7):
  - key lạ / giá trị lạ bị chặn: exit 2 + message tiếng Việt nói rõ lựa chọn.
  - persist thật qua vòng load/save trên global config JSON (tmp_path),
    KHÔNG mất dữ liệu khác đang có (workspaces/auth/…).
  - ``ctf config`` không key liệt kê các key biết được.
  - ``ctf config <key>`` xem giá trị đã lưu (không phải mặc định).

Chạy: python3 -m pytest test_config_cmd.py -q
"""
import json

import pytest

from ctf_downloader.cli import build_unified_parser
from ctf_downloader.cli_commands import handle_config


def _patch_global_cfg(monkeypatch, tmp_path):
    """Trỏ global config về tmp_path (cả CONFIG_DIR cho makedirs lẫn file)."""
    from ctf_downloader.storage import global_config as gc
    cfg_dir = tmp_path / "cfg"
    cfg_file = cfg_dir / "config.json"
    monkeypatch.setattr(gc, "CONFIG_DIR", str(cfg_dir))
    monkeypatch.setattr(gc, "GLOBAL_CONFIG_FILE", str(cfg_file))
    return cfg_file


def _run(argv):
    ns = build_unified_parser().parse_args(argv)
    handle_config(ns)


def test_key_la_bi_chan_exit_2_va_goi_y_key_biet(capsys):
    with pytest.raises(SystemExit) as ei:
        _run(["config", "khong-phai-key"])
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "khong-phai-key" in out
    assert "auto-sync" in out            # message liệt kê key biết được


def test_gia_tri_la_bi_chan_exit_2_khong_ghi_file(tmp_path, monkeypatch, capsys):
    cfg_file = _patch_global_cfg(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as ei:
        _run(["config", "auto-sync", "maybe"])
    assert ei.value.code == 2
    out = capsys.readouterr().out
    assert "on|off" in out               # message nêu rõ giá trị hợp lệ
    assert not cfg_file.exists(), "giá trị lạ không được phép ghi xuống đĩa"


def test_persist_that_qua_vong_load_save_giu_du_lieu_khac(tmp_path, monkeypatch,
                                                          capsys):
    cfg_file = _patch_global_cfg(monkeypatch, tmp_path)
    # Dữ liệu có sẵn (auth/workspaces) phải được giữ nguyên sau khi đặt.
    cfg_file.parent.mkdir()
    cfg_file.write_text(json.dumps({
        "workspaces": {"/w1": {"path": "/w1"}},
        "default_workspace": "/w1",
        "auth": {"/w1": {"cookie": "CK"}},
    }), encoding="utf-8")

    _run(["config", "auto-sync", "off"])          # đặt
    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["auto_sync"] == {"enabled": False}
    assert saved["auth"]["/w1"] == {"cookie": "CK"}      # dữ liệu cũ còn
    assert saved["default_workspace"] == "/w1"

    # Vòng load/save thứ hai: đọc lại bằng chính lệnh xem -> off.
    _run(["config", "auto-sync"])
    out = capsys.readouterr().out
    assert "off" in out

    # Đổi ý lần nữa: on ghi đè (không tạo key trùng/lạ).
    _run(["config", "auto-sync", "ON"])           # hoa/thường vẫn nhận
    saved2 = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved2["auto_sync"] == {"enabled": True}


def test_khong_key_liet_ke_cac_key_biet_duoc(capsys):
    _run(["config"])
    out = capsys.readouterr().out
    # Logger đi qua rich Console: khi non-tty (pytest capsys) rich soft-wrap
    # ở width 80 theo ranh giới từ — điểm wrap phụ thuộc ĐỘ DÀI desc, không
    # phải hành vi liệt kê (R6/e798f82 kéo dài desc khiến 'ctf watch' bị
    # ngắt thành 'ctf \nwatch'). Gộp whitespace trước khi assert để test
    # không dính vào vị trí wrap.
    flat = " ".join(out.split())
    assert "auto-sync" in flat           # liệt kê đúng tên key CLI
    assert "ctf watch" in flat           # kèm mô tả ngữ nghĩa
    assert "workspace .ctf/config.json override" in flat   # R6: precedence


def test_view_key_chua_dat_hien_mac_dinh(tmp_path, monkeypatch, capsys):
    _patch_global_cfg(monkeypatch, tmp_path)     # file chưa tồn tại
    _run(["config", "auto-sync"])
    out = capsys.readouterr().out
    assert "on" in out and "(mặc định)" in out


def test_view_khong_con_chrome_logger_legacy(tmp_path, monkeypatch, capsys):
    """synthesis-v6 MF3: chế độ xem render PHOSPHOR — không còn chrome
    '[*]' legacy của Logger.info."""
    _patch_global_cfg(monkeypatch, tmp_path)     # config trống → giá trị mặc định
    _run(["config"])
    out = capsys.readouterr().out
    assert "[*]" not in out
    flat = " ".join(out.split())
    assert "auto-sync" in flat                    # hàng key vẫn đủ
    assert "on" in flat and "(mặc định)" in flat  # giá trị + nhãn default


def test_view_wrap_continuation_thut_dung_cot(capsys):
    """synthesis-v6 MF3: hàng config dài wrap qua _emit_wrapped — continuation
    thụt đúng cột giá trị (15 spaces sau ``{name:<12} = ``), không gãy về cột 1
    kiểu soft-wrap của Logger."""
    _run(["config"])
    out = capsys.readouterr().out
    lines = [ln for ln in out.splitlines() if ln.strip()]
    conts = [ln for ln in lines if ln.startswith(" " * 15) and ln.strip()]
    assert conts, f"hàng config dài phải wrap thụt đúng cột giá trị:\n{out}"


def test_workspace_root_persist_and_drives_cli_defaults(tmp_path, monkeypatch, capsys):
    cfg_file = _patch_global_cfg(monkeypatch, tmp_path)
    root = tmp_path / "ctf-root"

    _run(["config", "workspace-root", str(root)])

    saved = json.loads(cfg_file.read_text(encoding="utf-8"))
    assert saved["workspace_root"] == str(root.resolve())

    from ctf_downloader.storage.global_config import resolve_workspace_root
    assert resolve_workspace_root() == str(root.resolve())

    parser = build_unified_parser()
    ws = parser.parse_args(["workspaces"])
    storage = parser.parse_args(["storage"])
    git_init = parser.parse_args(["git", "init", "--no-push"])
    assert ws.dir == str(root.resolve())
    assert storage.base_dir == str(root.resolve())
    assert git_init.dir == str(root.resolve())

    _run(["config", "workspace-root"])
    flat = " ".join(capsys.readouterr().out.split())
    assert str(root.resolve()) in flat


def test_workspace_root_rejects_empty_value(tmp_path, monkeypatch, capsys):
    _patch_global_cfg(monkeypatch, tmp_path)
    with pytest.raises(SystemExit) as ei:
        _run(["config", "workspace-root", "   "])
    assert ei.value.code == 2
    assert "không được để trống" in capsys.readouterr().out
