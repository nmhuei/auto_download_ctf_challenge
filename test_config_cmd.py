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
    assert "auto-sync" in out             # liệt kê đúng tên key CLI
    assert "ctf watch" in out             # kèm mô tả ngữ nghĩa


def test_view_key_chua_dat_hien_mac_dinh(tmp_path, monkeypatch, capsys):
    _patch_global_cfg(monkeypatch, tmp_path)     # file chưa tồn tại
    _run(["config", "auto-sync"])
    out = capsys.readouterr().out
    assert "on" in out and "(mặc định)" in out
