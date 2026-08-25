"""Unified CLI: định nghĩa argparse + dispatch. Logic command nằm ở cli_commands
(lớp mỏng gọi services); script legacy nằm ở cli_legacy."""
import argparse
import os
import sys

from .cli_commands import (  # noqa: F401 — re-export cho script legacy/test cũ
    get_auth_for_workspace,
    handle_config,
    handle_doctor,
    handle_export_pack,
    handle_hoard,
    handle_history,
    handle_instance,
    handle_note,
    handle_open,
    handle_pull,
    handle_rank,
    handle_register,
    handle_serve,
    handle_sniper,
    handle_status,
    handle_storage,
    handle_submit,
    handle_sync,
    handle_tag,
    handle_watch,
    handle_workspaces,
)
from .interactive_menu import launch_interactive_menu


class _PhosphorHelpParser(argparse.ArgumentParser):
    """Parser gốc ``ctf``: ``--help`` render theo HelpScreen spec §4.8
    (PHOSPHOR FIELD KIT) thay vì usage/options mặc định của argparse.

    Subparser kế thừa class này qua ``parser_class``, nên chỉ parser gốc
    (``prog == 'ctf'``) được render riêng — help của lệnh con
    (``ctf status --help``…) giữ nguyên cơ chế argparse.
    """

    def print_help(self, file=None):
        if self.prog != 'ctf':
            super().print_help(file)
            return
        self._render_phosphor_help(file or sys.stdout)

    @staticmethod
    def _render_phosphor_help(out):
        from rich.console import Console, Group
        from rich.text import Text

        from .ui.banner import TAGLINE, app_header, banner_b, tagline_text
        from .ui.theme import FG_BASE, FG_FAINT, FG_MUTED, INFO, load_theme
        from .ui.widgets import footer_bar

        # LỆNH — mỗi lệnh 1 dòng, cột lệnh pad cố định 12, KHÔNG liệt kê alias.
        COMMANDS = [
            ('pull', 'Tải đề + attachment từ platform, dựng workspace'),
            ('status', 'Bảng tổng quan workspace hiện tại'),
            ('workspaces', 'Quét mọi workspace CTF trên máy'),
            ('sync', 'Đồng bộ metadata động workspace ↔ platform'),
            ('instance', 'Quản lý container động của challenge'),
            ('submit', 'Gửi flag lên platform và ghi nhật ký'),
            ('hoard', 'Lưu flag tìm được vào kho local (chưa nộp)'),
            ('note', 'Ghi/xoá note cho một challenge'),
            ('tag', 'Thêm/xoá label cho một challenge'),
            ('rank', 'Bảng xếp hạng và thống kê giải'),
            ('watch', 'Auto-sync trong event window của giải'),
            ('sniper', 'Nộp flag tự động đúng giờ G'),
            ('register', 'Tự tạo tài khoản trên platform'),
            ('doctor', 'Health-check platform trước giờ giải'),
            ('storage', 'Báo cáo dung lượng workspace + archive'),
            ('export-pack', 'Đóng gói writeup đã solve thành pack zip'),
            ('history', 'Lịch sử submit flag của workspace'),
            ('serve', 'Dashboard web read-only cho workspace'),
            ('open', 'Mở thư mục challenge trong file manager'),
            ('config', 'Xem/đặt cấu hình toàn cục (auto-sync…)'),
            ('menu', 'Console interactive đầy đủ'),
        ]

        console = Console(file=out, theme=load_theme(None))

        listing = Text()
        for name, desc in COMMANDS:
            listing.append(f'  {name:<12}', style=f'bold {FG_BASE}')
            listing.append(f'{desc}\n', style=FG_MUTED)

        syntax = Text('  ctf <lệnh> [tuỳ chọn]', style=INFO)

        footer = footer_bar([('↑↓', 'di chuyển'), ('?', 'help'),
                             ('q', 'thoát')], width=max(40, console.width))

        # Nhịp theo spec §4.8: AppHeader (codex-r3 #2 — help là lệnh thường
        # duy nhất còn thiếu) → 1 dòng trống → banner → 1 dòng trống (banner
        # tự kết thúc bằng '\n') → tagline → 1 → CÚ PHÁP → syntax → 1 →
        # LỆNH → bảng lệnh → footer.
        console.print(Group(
            app_header('help', timestamp=_frame_timestamp()),
            Text(),
            banner_b(),
            tagline_text(),
            Text(),
            Text('CÚ PHÁP', style=f'bold {FG_FAINT}'),
            syntax,
            Text(),
            Text('LỆNH', style=f'bold {FG_FAINT}'),
            listing,
            footer,
        ))


def build_unified_parser():
    parser = _PhosphorHelpParser(
        prog='ctf',
        description='CTF Toolkit: Unified CTF Downloader, Submitter, Container Manager & Dashboard',
    )
    from . import __version__ as _pkg_version
    parser.add_argument('-v', '--version', action='version', version=f'ctf-toolkit {_pkg_version}')
    parser.add_argument('-i', '--interactive', action='store_true', help='Launch full interactive CTF console')
    parser.add_argument('-w', '--workspace', default=None, help='CTF workspace directory')

    subparsers = parser.add_subparsers(dest='subcommand', title='Core Commands', help='Command to execute')

    # 1. PULL / DOWNLOAD / CLONE
    pull_parser = subparsers.add_parser('pull', aliases=['download', 'clone'], help='Download challenges, files & build workspace')
    pull_parser.add_argument('-u', '--url', type=str, help='Target CTF platform URL (e.g. https://ctf.example.com)')
    pull_parser.add_argument('-c', '--cookie', type=str, help='Cookie string or path to cookie file')
    pull_parser.add_argument('-t', '--token', type=str, help='API token or Bearer token')
    pull_parser.add_argument('-o', '--output', type=str, default=None, help='Output directory path')
    pull_parser.add_argument('-j', '--threads', type=int, default=4, help='Number of download threads (default: 4)')
    pull_parser.add_argument('-C', '--category', nargs='+', help='Only download specific categories (e.g. -C Web Pwn)')
    pull_parser.add_argument('-E', '--exclude', nargs='+', help='Exclude specific categories')
    pull_parser.add_argument('--no-third-party', action='store_true', help='Disable downloading 3rd party links')
    pull_parser.add_argument('--no-template', action='store_true', help='Disable generating solve.py templates')
    pull_parser.add_argument('-f', '--force', action='store_true', help='Force re-download existing files')
    pull_parser.add_argument('--update', action='store_true',
                             help='Pull tăng dần: chỉ tải challenge MỚI, cập nhật metadata (points/solves/solved/connection) challenge đã có')
    pull_parser.add_argument('--refresh-meta', action='store_true',
                             help='Như --update, nhưng cho phép tải lại attachment khi file thiếu trên đĩa')
    pull_parser.add_argument('--timeout', type=int, default=30, help='Request timeout in seconds (default: 30)')
    pull_parser.add_argument('-i', '--interactive', action='store_true', help='Launch interactive download wizard')

    # 2. STATUS / TREE / LS / DASHBOARD
    status_parser = subparsers.add_parser('status', aliases=['tree', 'ls', 'dashboard'], help='Display challenge structure, points, and solve progress')
    status_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    status_parser.add_argument('-u', '--unsolved', action='store_true', help='Show only unsolved challenges')
    status_parser.add_argument('-s', '--solved', action='store_true', help='Show only solved challenges')
    status_parser.add_argument('-C', '--category', nargs='+', help='Filter specific categories (e.g. -C Web Crypto)')
    status_parser.add_argument('--container', action='store_true', help='Filter only dynamic container challenges')
    status_parser.add_argument('--label', action='append', default=None, dest='labels',
                               help='Chỉ hiện challenge mang TẤT CẢ label này (lặp lại --label để AND, vd: --label hard --label todo)')
    status_parser.add_argument('--search', default=None,
                               help='Tìm từ khoá trong tên + note của challenge')

    # 2b. NOTE / TAG — memory của người chơi ("đã thử SSTI, bị chặn")
    note_parser = subparsers.add_parser('note', aliases=['ghi-chu'],
                                        help='Ghi/xoá note cho một challenge (lưu vào metadata.status.notes)')
    note_parser.add_argument('target', help='Challenge ID hoặc Name')
    note_parser.add_argument('content', nargs='*', help='Nội dung note (bỏ trống để nhập multi-line, kết thúc bằng dòng trống)')
    note_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory')
    note_parser.add_argument('--remove', action='store_true', help='Xoá note của challenge')

    tag_parser = subparsers.add_parser('tag', aliases=['tags'],
                                       help='Thêm/xoá label cho một challenge ([a-z0-9-], tối đa 24 ký tự)')
    tag_parser.add_argument('target', help='Challenge ID hoặc Name')
    tag_parser.add_argument('tags', nargs='+', help='Một hoặc nhiều tag')
    tag_parser.add_argument('-r', '--remove', action='store_true', help='Xoá các tag khỏi challenge thay vì thêm')
    tag_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory')

    # 3. WORKSPACES / SCAN
    ws_parser = subparsers.add_parser('workspaces', aliases=['scan'], help='Scan and list all local CTF workspaces')
    ws_parser.add_argument('-d', '--dir', default=os.path.expanduser('~/Workspace/CTF'), help='Base CTF directory to scan')

    # 4. INSTANCE / CONTAINER
    inst_parser = subparsers.add_parser('instance', aliases=['container', 'spawn'], help='Manage dynamic container instances from terminal')
    inst_parser.add_argument('action', nargs='?', choices=['start', 'stop', 'extend', 'status', 'list'], default=None, help='Container action')
    inst_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory')
    inst_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    inst_parser.add_argument('-t', '--token', help='API token or Bearer token')
    inst_parser.add_argument('--id', help='Target challenge ID')
    inst_parser.add_argument('-n', '--name', help='Target challenge name')
    inst_parser.add_argument('-l', '--list', action='store_true', help='List all container challenges')
    inst_parser.add_argument('-i', '--interactive', action='store_true', help='Interactive container wizard')
    inst_parser.add_argument('--auto-extend', action='store_true',
                             help='Giữ sống container được chọn (--id/-n): tự extend trong cửa sổ cuối, auto-restart theo R-A')
    inst_parser.add_argument('--auto-extend-all', action='store_true',
                             help='Giữ sống MỌI container running của workspace')
    inst_parser.add_argument('-y', '--yes', action='store_true',
                             help='Xác nhận tự động cho thao tác phá vỡ kết nối (vd restart ĐỔI FLAG theo ràng buộc R-A)')

    # 5. SUBMIT / FLAG
    sub_parser = subparsers.add_parser('submit', aliases=['flag'], help='Submit flag to CTF platform and update local documentation')
    sub_parser.add_argument('target', nargs='?', help='Target challenge ID or Name')
    sub_parser.add_argument('flag_val', nargs='?', help='Flag string to submit')
    sub_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory')
    sub_parser.add_argument('-u', '--url', help='Platform URL (optional if workspace provided)')
    sub_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    sub_parser.add_argument('-t', '--token', help='API token or Bearer token')
    sub_parser.add_argument('--id', help='Target challenge ID')
    sub_parser.add_argument('-n', '--name', help='Target challenge name')
    sub_parser.add_argument('-f', '--flag', help='Flag string to submit')
    sub_parser.add_argument('--auto', action='store_true', help='Auto-scan workspace for filled flags and submit')
    sub_parser.add_argument('--flag-format', dest='flag_format', help='Regex định dạng flag của giải (vd: "^PTITCTF\\{.+\\}$")')
    sub_parser.add_argument('--force', action='store_true', help='Vượt blacklist flag sai để vẫn submit')
    sub_parser.add_argument('-i', '--interactive', action='store_true', help='Interactive submission wizard')

    # 5b. HOARD / FLAG-STASH — lưu flag local, KHÔNG submit
    #     (tên `flag` đã là alias của `submit` nên lệnh mới đặt `hoard`)
    hoard_parser = subparsers.add_parser('hoard', aliases=['flag-stash'], help='Lưu flag tìm được vào kho local (metadata.json) mà KHÔNG submit lên platform')
    hoard_parser.add_argument('target', nargs='?', help='Target challenge ID or Name')
    hoard_parser.add_argument('flag_val', nargs='?', help='Flag string to hoard (bỏ qua khi --list/--remove)')
    hoard_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory')
    hoard_parser.add_argument('--id', help='Target challenge ID')
    hoard_parser.add_argument('-n', '--name', help='Target challenge name')
    hoard_parser.add_argument('-f', '--flag', help='Flag string to hoard')
    hoard_parser.add_argument('--list', action='store_true',
                              help='Bảng mọi flag đang giữ (hoarded/found_unverified) chờ submit — sort theo điểm giảm dần')
    hoard_parser.add_argument('--all', dest='show_all', action='store_true',
                              help='Với --list: hiện flag đầy đủ (mặc định chỉ 4 ký tự đầu + ***)')
    hoard_parser.add_argument('--remove', action='store_true',
                              help='Gỡ flag khỏi kho cho challenge chỉ định (state về none, xoá value)')

    # 6. RANK / SCOREBOARD / LEADERBOARD
    rank_parser = subparsers.add_parser('rank', aliases=['scoreboard', 'leaderboard'], help='Display live scoreboard standings and update ranking docs')
    rank_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    rank_parser.add_argument('-u', '--url', help='Platform base URL')
    rank_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    rank_parser.add_argument('-t', '--token', help='API token or Bearer token')
    rank_parser.add_argument('-n', '--top', type=int, default=15, help='Number of top teams to display (default: 15)')
    rank_parser.add_argument('--no-docs', action='store_true', help='Do not write/update RANKING.md or SUMMARY.md')

    # 7. WATCH / EVENT WINDOW — auto-sync trong window giải + keep-alive
    #    (alias 'sync' đã nhường cho lệnh `ctf sync` — sync metadata 2 chiều)
    watch_parser = subparsers.add_parser('watch', help='Auto-sync challenges/scoreboard/notices trong event window (+ keep-alive instance)')
    watch_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    watch_parser.add_argument('--once', action='store_true', help='Chạy đúng 1 vòng rồi exit (entrypoint cho cron/systemd bọc ngoài)')
    watch_parser.add_argument('--no-scoreboard', action='store_true', help='Tắt tick scoreboard')
    watch_parser.add_argument('--start', help='Bắt đầu giải (ISO-8601 hoặc epoch) — override nguồn tự nhận diện')
    watch_parser.add_argument('--end', help='Kết thúc giải (ISO-8601 hoặc epoch) — override nguồn tự nhận diện')
    watch_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    watch_parser.add_argument('-t', '--token', help='API token or Bearer token')

    # 9. REGISTER / AUTO-REGISTER — tạo 1 tài khoản trên platform
    reg_parser = subparsers.add_parser('register', aliases=['reg'],
                                       help='Tự tạo ĐÚNG 1 tài khoản trên platform (GZCTF/CTFd) + lưu auth map')
    reg_parser.add_argument('-u', '--url', help='URL platform (vd https://ctf.example.com)')
    reg_parser.add_argument('--email', help='Email dùng để đăng ký (bỏ qua nếu dùng --tempmail)')
    reg_parser.add_argument('--tempmail', action='store_true',
                            help='Ép dùng mailbox tạm mail.tm (cần khi platform bắt verify email)')
    reg_parser.add_argument('--username', dest='username_prefix', default='player',
                            help="Prefix username (mặc định 'player' + 6 ký tự random)")
    reg_parser.add_argument('--password', help='Mật khẩu muốn đặt (mặc định sinh random mạnh 16 ký tự)')
    reg_parser.add_argument('-w', '--workspace', default=None,
                            help='Workspace để gắn credentials trong auth map (mặc định key=URL)')

    # 7b. DOCTOR / HEALTH-CHECK — kiểm tra platform trước giờ giải
    doctor_parser = subparsers.add_parser('doctor', aliases=['health', 'checkup'],
                                          help='Health-check platform: URL/auth/capabilities/event-window/flag-format')
    doctor_parser.add_argument('-u', '--url', help='Platform URL (vd https://ctf.example.com)')
    doctor_parser.add_argument('-w', '--workspace', default=None,
                               help='Workspace để lấy auth từ auth map (nếu không truyền -c/-t)')
    doctor_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    doctor_parser.add_argument('-t', '--token', help='API token or Bearer token')

    # 8. MENU / UI / INTERACTIVE
    menu_parser = subparsers.add_parser('menu', aliases=['ui', 'console'], help='Launch full interactive CTF suite dashboard')
    menu_parser.add_argument('-w', '--workspace', default=None, help='CTF workspace directory')
    menu_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    menu_parser.add_argument('-t', '--token', help='API token or Bearer token')

    # 10. STORAGE / DU / ARCHIVE — báo cáo dung lượng + archive workspace
    storage_parser = subparsers.add_parser('storage', aliases=['du', 'archive'],
                                           help='Kiểm soát dung lượng workspace: báo cáo usage, gợi ý dọn dẹp, archive tar.gz (+ git push)')
    storage_parser.add_argument('-d', '--base-dir', default=os.path.expanduser('~/Workspace/CTF'),
                                help='Thư mục gốc chứa các workspace (default: ~/Workspace/CTF)')
    storage_parser.add_argument('--threshold-mb', type=int, default=1024,
                                help='Ngưỡng cảnh báo dung lượng mỗi workspace, tính MiB (default: 1024)')
    storage_sub = storage_parser.add_subparsers(dest='storage_command')
    storage_arch = storage_sub.add_parser('archive', help='Đóng gói một workspace thành tar.gz (tuỳ chọn push git remote)')
    storage_arch.add_argument('workspace_name', help='Tên workspace con trong --base-dir')
    storage_arch.add_argument('--git-remote', help='Git remote URL để commit + push archive (không tự tạo remote)')
    storage_arch.add_argument('--out', help='Thư mục lưu archive (default: <base-dir>/_archives)')
    storage_arch.add_argument('-y', '--yes', action='store_true',
                              help='Bỏ qua confirm archive (bắt buộc khi non-interactive); xoá workspace gốc vẫn cần xác nhận riêng')

    # 11. SYNC — đồng bộ metadata 2 chiều workspace <-> platform (P2-1)
    sync_parser = subparsers.add_parser('sync', aliases=['resync'],
                                        help='Đồng bộ metadata động (points/solves/connection) workspace ↔ platform; không đụng status/flag/file')
    sync_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    sync_parser.add_argument('--verify', action='store_true',
                             help='Chạy thêm verify: liệt kê challenge solved trên server nhưng local chưa (drift)')

    # 12. EXPORT-PACK — đóng gói writeup thành pack zip (P2-3)
    exp_parser = subparsers.add_parser('export-pack',
                                       help='Đóng gói writeup các challenge đã solve thành pack markdown + zip')
    exp_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    exp_parser.add_argument('--out', default='.', help='Thư mục lưu pack zip (default: thư mục hiện tại)')

    # 13. HISTORY — lịch sử submit từ submit_history.json
    hist_parser = subparsers.add_parser('history', aliases=['log'],
                                        help='Xem lịch sử submit flag của workspace (flag bị che mặc định)')
    hist_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    hist_parser.add_argument('--all', dest='show_all', action='store_true',
                             help='Hiện flag đầy đủ (mặc định chỉ 4 ký tự đầu + ***)')
    hist_parser.add_argument('--tail', '--limit', dest='tail', type=int,
                             metavar='N', default=100,
                             help='Chỉ hiện N entry MỚI NHẤT (default: 100; '
                                  'dùng <=0 hoặc --all để in toàn bộ)')

    # 14. SNIPER — preload flag, nộp tự động đúng giờ G (P2-6)
    sniper_parser = subparsers.add_parser('sniper',
                                          help='Preload flag và nộp tự động ngay giây đầu window mở (first-blood race)')
    sniper_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    sniper_parser.add_argument('--start-at', dest='start_at',
                               help='Thời điểm mở giải ISO-8601/epoch — bắt buộc nếu challenges.json thiếu event_window.start')
    sniper_parser.add_argument('--retry-wrong', dest='retry_wrong', action='store_true',
                               help='Cho phép thử lại target sai (tối đa 3 lần/target, qua gate force)')
    sniper_parser.add_argument('--poll', type=int, default=10,
                               help='Chu kỳ poll khi chờ giờ G / backoff, giây (default: 10)')

    # 15. SERVE — dashboard web read-only
    serve_parser = subparsers.add_parser('serve', aliases=['web'],
                                         help='Chạy dashboard web read-only cho workspace (bind 127.0.0.1)')
    serve_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    serve_parser.add_argument('--port', type=int, default=8689, help='Port HTTP (default: 8689)')

    # 16. OPEN — mở thư mục challenge trong file manager
    open_parser = subparsers.add_parser('open',
                                        help='Mở thư mục challenge trong file manager/terminal (xdg-open)')
    open_parser.add_argument('target', help='Challenge ID hoặc Name')
    open_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory')

    # 17. CONFIG — xem/đặt cấu hình toàn cục (spec event-window §4:
    #     "Đổi ý: ctf config auto-sync off")
    config_parser = subparsers.add_parser('config',
                                          help='Xem/đặt cấu hình toàn cục (vd: ctf config auto-sync off)')
    config_parser.add_argument('key', nargs='?',
                               help='Tên key (vd auto-sync). Bỏ trống để liệt kê mọi key')
    config_parser.add_argument('value', nargs='?',
                               help="Giá trị mới (auto-sync: on|off). Bỏ trống để chỉ xem giá trị hiện tại")

    return parser


def _frame_console():
    """Rich console cho AppHeader/FooterBar (theme PHOSPHOR, stdout).

    Non-TTY: rich tự strip ANSI → fallback plain (không màu) nhưng vẫn giữ
    đúng nội dung 1 dòng header / footer.
    """
    from rich.console import Console

    from .ui.theme import load_theme
    return Console(theme=load_theme(None))


def _frame_timestamp():
    """Timestamp faint mép phải AppHeader — giờ local + offset UTC."""
    import datetime as _dt
    try:
        now = _dt.datetime.now().astimezone()
        off_h = int(now.utcoffset().total_seconds() // 3600)
        return f"{now:%H:%M} UTC{off_h:+d}"
    except Exception:
        return ""


#: FooterBar chuẩn cho lệnh thường (spec §4.7: phím amber · nhãn fg.base).
_FRAME_FOOTER = [('↑↓', 'di chuyển'), ('?', 'help'), ('q', 'thoát')]


def _print_app_header(label, context=""):
    from .ui.banner import app_header
    _frame_console().print(
        app_header(label, context=context, timestamp=_frame_timestamp()))


def _print_footer_bar():
    from .ui.widgets import footer_bar
    con = _frame_console()
    con.print(footer_bar(_FRAME_FOOTER, width=max(40, con.width)))


def _run_framed(handler, args, label, ctx_attr='workspace'):
    """Bọc handler lệnh thường bằng AppHeader (đầu) + FooterBar (cuối).

    Handler sys.exit() giữa chừng (lỗi) → không in footer (nhịp kết thúc chỉ
    dành cho output thành công)."""
    _print_app_header(label, str(getattr(args, ctx_attr, '') or ''))
    handler(args)
    _print_footer_bar()


def main():
    if len(sys.argv) == 1:
        launch_interactive_menu()
        return

    if len(sys.argv) == 2 and sys.argv[1] in ['-i', '--interactive', 'menu', 'ui', 'console']:
        launch_interactive_menu()
        return

    parser = build_unified_parser()
    args = parser.parse_args()

    if args.interactive:
        launch_interactive_menu(workspace_path=args.workspace)
        return

    cmd = args.subcommand
    if cmd in ['pull', 'download', 'clone']:
        handle_pull(args)
    elif cmd in ['status', 'tree', 'ls', 'dashboard']:
        _run_framed(handle_status, args, 'status')
    elif cmd in ['workspaces', 'scan']:
        _run_framed(handle_workspaces, args, 'workspaces', ctx_attr='dir')
    elif cmd in ['instance', 'container', 'spawn']:
        handle_instance(args)
    elif cmd in ['submit', 'flag']:
        handle_submit(args)
    elif cmd in ['hoard', 'flag-stash']:
        # --list là surface xem → có chrome AppHeader/FooterBar như
        # status/workspaces (synthesis-v6 MF2); nhánh ghi/remove giữ nhịp
        # action trần như submit.
        if getattr(args, 'list', False):
            _run_framed(handle_hoard, args, 'hoard')
        else:
            handle_hoard(args)
    elif cmd in ['note', 'ghi-chu']:
        handle_note(args)
    elif cmd in ['tag', 'tags']:
        handle_tag(args)
    elif cmd in ['rank', 'scoreboard', 'leaderboard']:
        handle_rank(args)
    elif cmd == 'watch':
        handle_watch(args)
    elif cmd in ['doctor', 'health', 'checkup']:
        handle_doctor(args)
    elif cmd in ['register', 'reg']:
        handle_register(args)
    elif cmd in ['storage', 'du', 'archive']:
        _run_framed(handle_storage, args, 'storage', ctx_attr='base_dir')
    elif cmd in ['sync', 'resync']:
        _run_framed(handle_sync, args, 'sync')
    elif cmd == 'export-pack':
        _run_framed(handle_export_pack, args, 'export-pack')
    elif cmd in ['history', 'log']:
        _run_framed(handle_history, args, 'history')
    elif cmd == 'sniper':
        handle_sniper(args)
    elif cmd in ['serve', 'web']:
        handle_serve(args)
    elif cmd == 'open':
        handle_open(args)
    elif cmd == 'config':
        # Chế độ xem là surface → có chrome (synthesis-v6 MF3); chế độ đặt
        # giá trị là action ghi file, giữ nhịp Logger như submit/sync set.
        if getattr(args, 'value', None) is None:
            _run_framed(handle_config, args, 'config')
        else:
            handle_config(args)
    elif cmd in ['menu', 'ui', 'console']:
        launch_interactive_menu(workspace_path=args.workspace, cookie=args.cookie, token=args.token)
    else:
        launch_interactive_menu()


if __name__ == '__main__':
    main()
