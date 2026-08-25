"""synthesis-v6 NICE batch — workspaces surface (N1 + N2).

Chạy: python3 -m pytest test_workspaces_polish.py -q
Không mạng, không fixture đĩa thật — rows của scan_all_workspaces được
monkeypatch. Các case:

- N1: title trùng giữa 2 workspace → hàng gắn thêm dirname faint,
  title duy nhất KHÔNG bị gắn suffix.
- N2: PLATFORM không lộ key nội bộ (``custom_res``) — render qua
  ``display_label`` (spec.label), key lạ giữ nguyên literal.
"""
import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from ctf_downloader.cli_commands import handle_workspaces
from ctf_downloader.platforms.registry import display_label


def fake_row(title, platform, dirname, solved=0, total=3):
    return {
        'title': title,
        'platform': platform,
        'total_challenges': total,
        'solved_challenges': solved,
        'completion_rate': (solved / total * 100) if total else 0,
        '_ended': False,
        '_dir': dirname,
    }


def render_rows(rows) -> str:
    buf = io.StringIO()
    args = SimpleNamespace(dir='/tmp/does-not-matter')
    with patch('ctf_downloader.services.status_service.StatusService.'
               'scan_all_workspaces', return_value=list(rows)):
        with contextlib.redirect_stdout(buf), \
                contextlib.redirect_stderr(io.StringIO()):
            handle_workspaces(args)
    return buf.getvalue()


class TestDisplayLabel(unittest.TestCase):
    """N2 — nhãn platform an toàn cho UI."""

    def test_known_keys_use_registry_labels(self):
        self.assertEqual(display_label('ctfd'), 'CTFd')
        self.assertEqual(display_label('gzctf'), 'GZ::CTF')
        self.assertEqual(display_label('rctf'), 'rCTF')

    def test_custom_rest_no_longer_truncated_to_internal_key(self):
        # Lỗi cũ: str(key)[:10] → 'custom_res' lộ key nội bộ.
        label = display_label('custom_rest')
        self.assertNotIn('custom_res', label)
        self.assertTrue(label.startswith('Custom'))

    def test_long_label_cut_at_word_boundary(self):
        # 'Custom REST / Next.js CTF' (25 ký tự) → cắt tại biên từ trong
        # max_len mặc định 10, không gãy giữa token.
        self.assertEqual(display_label('custom_rest', max_len=10), 'Custom')
        self.assertLessEqual(len(display_label('generic_html')), 10)

    def test_unknown_key_passthrough_untouched(self):
        # Key lạ giữ NGUYÊN (kể cả > max_len) — cắt giữa token sẽ tái tạo
        # đúng bệnh 'custom_res': mẩu key không tra cứu được.
        self.assertEqual(display_label('mystery_box'), 'mystery_box')
        self.assertEqual(display_label('a' * 40), 'a' * 40)

    def test_output_never_contains_raw_key_when_registered(self):
        for key in ('ctfd', 'gzctf', 'rctf', 'custom_rest', 'generic_html'):
            self.assertNotIn(str(key)[:10], display_label(key),
                             f"label của {key} vẩn còn chứa key thô")


class TestWorkspacesDupTitle(unittest.TestCase):
    """N1 — hai workspace cùng title phải phân biệt được qua dirname."""

    def test_duplicate_titles_get_dirname_suffix(self):
        out = render_rows([
            fake_row('CTF Competition', 'rctf', 'CTF_Competition'),
            fake_row('CTF Competition', 'rctf', 'Z0d1ak_CTF'),
        ])
        self.assertIn('CTF Competition · CTF_Competition', out)
        self.assertIn('CTF Competition · Z0d1ak_CTF', out)

    def test_unique_title_stays_clean(self):
        out = render_rows([
            fake_row('Vòng loại PTIT CTF 2026', 'gzctf', 'PTIT_CTF_2026'),
            fake_row('CTF Competition', 'ctfd', 'Z0d1ak_CTF'),
        ])
        # Title duy nhất không bị gắn suffix; platform ra label chuẩn.
        ptit = [ln for ln in out.splitlines()
                if ln.strip().startswith('Vòng loại PTIT CTF 2026')]
        self.assertEqual(len(ptit), 1)
        self.assertNotIn(' · ', ptit[0])
        self.assertIn('GZ::CTF', out)
        self.assertIn('CTFd', out)

    def test_platform_cell_never_leaks_custom_res(self):
        out = render_rows([
            fake_row('$N1PH€RSxTCTF', 'custom_rest', 'N1PH_RSxTCTF'),
        ])
        self.assertNotIn('custom_res', out)
        self.assertIn('Custom', out)


if __name__ == '__main__':
    unittest.main()
