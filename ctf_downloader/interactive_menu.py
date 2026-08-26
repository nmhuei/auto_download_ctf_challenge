import os
import sys
import glob
from typing import Optional

from rich.prompt import Confirm
from rich.text import Text

from .dashboard import CTFDashboard
from .instance_manager import InstanceManager
from .submitter import FlagSubmitter
from .core import CTFDownloader
from .config import DownloaderConfig
from .utils.logger import Logger

from .services.status_service import StatusService
from .storage.global_config import (  # noqa: F401 — re-export để giữ tương thích
    CONFIG_DIR,
    GLOBAL_CONFIG_FILE,
    load_global_config,
    save_global_config,
    update_global_config,
)

# PHOSPHOR FIELD KIT (design-system spec §2/§3) — AppHeader radar + tokens.
from .ui.banner import app_header
from .ui.selection import MENU_CURSOR, fit_cells, selected_row
from .ui.splash import splash
from .ui.theme import ACCENT, FG_BASE, FG_FAINT, FG_MUTED, INFO, WARN, load_theme
from .ui.widgets import AMBER_RAMP, footer_bar, meter

#: Meter dùng chung ramp 3 mốc spec §3.3 (than hồng → hổ phách → vàng nhạt)
#: — ``ui.widgets.AMBER_RAMP`` canonical theo SPEC UI v2 §M1: mỗi ô nhận
#: ĐÚNG một trong ba màu theo vị trí cột, không nội suy trung gian.

#: Độ rộng cột switcher workspace (SPEC UI v2 §S1.2) — cắt theo display
#: width kèm ``…`` (MUST uiv2 #4), không bao giờ để dữ liệu tràn cột.
SWITCHER_TITLE_W = 30
SWITCHER_PLATFORM_W = 8

_MENU_CON = None


def _frame_timestamp():
    """Timestamp faint mép phải AppHeader — đồng bộ cli.py ``_frame_timestamp``."""
    import datetime as _dt
    try:
        now = _dt.datetime.now().astimezone()
        off_h = int(now.utcoffset().total_seconds() // 3600)
        return f"{now:%H:%M} UTC{off_h:+d}"
    except Exception:
        return ""


def _menu_console():
    """Rich console dùng chung cho toàn bộ menu (theme PHOSPHOR, stderr).

    Human-facing output đi stderr theo convention của ``ui.console``; non-TTY
    rich tự strip ANSI nên pipe/smoke test vẫn sạch.
    """
    global _MENU_CON
    if _MENU_CON is None:
        from rich.console import Console
        _MENU_CON = Console(stderr=True, theme=load_theme(None))
    return _MENU_CON


def _section(title: str):
    """Heading mục thống nhất: UPPERCASE faint, không viền/divider."""
    con = _menu_console()
    con.print()
    con.print(f'  {title.upper()}', style=f'bold {FG_FAINT}')


def _option(key: str, label: str):
    """Dòng lựa chọn ``[số] <tên>`` — số accent amber, tên fg.base."""
    t = Text('  ')
    t.append(f'[{key}]', style=ACCENT)
    t.append(f' {label}', style=FG_BASE)
    _menu_console().print(t)


def _prompt(msg: str) -> str:
    """Selection prompt ``❯`` accent amber (stderr, đọc stdin như input())."""
    t = Text(f'{MENU_CURSOR} ', style=ACCENT)
    t.append(msg)
    return _menu_console().input(t).strip()


def _footer(bindings=None):
    """FooterBar spec §4.7: phím amber + nhãn fg.base, phân cách dim."""
    con = _menu_console()
    bindings = bindings or [('?', 'help'), ('q', 'thoát')]
    width = max(40, con.width - 2)
    con.print()
    con.print(f'  {footer_bar(bindings, width)}')


def _workspace_rows(workspaces, active: str):
    """Các dòng workspace cho switcher (SPEC UI v2 §S1.2).

    Workspace đang dùng → ``selected_row(..., selected=True)`` full
    reverse-highlight (thay suffix '❯ đang dùng' cũ); workspace giải xong
    100% liệt kê kèm token ``done`` (strike muted) trên tên.

    MUST uiv2 #4: title/platform cắt theo display width kèm ``…``
    (:func:`ui.selection.fit_cells`) về đúng cột 30/8 cell — platform dài
    không còn tràn dính vào số solved, title dài không cắt cứng ``[:30]``
    mất ellipsis; các trường cách nhau ít nhất 1 space.
    """
    rows = []
    for idx, (p, st) in enumerate(workspaces, 1):
        title = fit_cells(str(st.get('title', os.path.basename(p))),
                          SWITCHER_TITLE_W, pad=True)
        solv = f"{st.get('solved_challenges', 0)}/{st.get('total_challenges', 0)}"
        plat = fit_cells(str(st.get('platform', 'generic')).upper(),
                         SWITCHER_PLATFORM_W, pad=True)
        meta = f"{plat} {solv} solved"
        if os.path.abspath(p) == active:
            rows.append(selected_row(f'[{idx:>2}] {title} {meta}', selected=True))
            continue
        total = st.get('total_challenges', 0)
        done = total > 0 and st.get('solved_challenges', 0) >= total
        row = Text('  ')
        row.append(f'[{idx:>2}]', style=ACCENT)
        row.append(f' {title}', style='done' if done else FG_BASE)
        row.append(f' {meta}', style=FG_MUTED)
        rows.append(row)
    return rows


class CTFInteractiveConsole:
    # §S1: option là hành động gần nhất → ❯ reverse. Default lớp để an toàn
    # với instance dựng qua __new__ (test harness bỏ qua __init__).
    _last_action: Optional[str] = None

    def __init__(self, workspace_path: Optional[str] = None, cookie: Optional[str] = None, token: Optional[str] = None):
        self.config = load_global_config()
        self.workspace_path = self._resolve_initial_workspace(workspace_path)
        self.cookie = cookie
        self.token = token
        self._load_saved_auth()

    def _resolve_initial_workspace(self, user_ws: Optional[str]) -> str:
        if user_ws and os.path.exists(user_ws):
            return os.path.abspath(user_ws)
        cwd = os.getcwd()
        if os.path.exists(os.path.join(cwd, 'challenges.json')) or os.path.exists(os.path.join(cwd, 'SUMMARY.md')):
            return cwd
        def_ws = self.config.get('default_workspace')
        if def_ws and os.path.exists(def_ws):
            return def_ws
        base_ctf = os.path.expanduser('~/Workspace/CTF')
        if os.path.exists(base_ctf):
            for d in os.listdir(base_ctf):
                p = os.path.join(base_ctf, d)
                if os.path.isdir(p) and (os.path.exists(os.path.join(p, 'challenges.json')) or os.path.exists(os.path.join(p, 'metadata.json'))):
                    return p
            return base_ctf
        return cwd

    def _load_saved_auth(self):
        auth_map = self.config.get('auth', {})
        if self.workspace_path in auth_map:
            saved = auth_map[self.workspace_path]
            if not self.cookie and saved.get('cookie'):
                self.cookie = saved.get('cookie')
            if not self.token and saved.get('token'):
                self.token = saved.get('token')

    # ------------------------------------------------------------------
    # Render-only helpers (PHOSPHOR FIELD KIT) — logic wizard không đổi.
    # ------------------------------------------------------------------

    def _print_header(self):
        """AppHeader Phosphor Radar (spec §4.1) — đồng bộ mọi surface framed
        (MUST uiv2 #1: bỏ Banner B half-block gây chia hai nhận diện), sau đó
        khối context WORKSPACE."""
        con = _menu_console()
        con.print(app_header('menu', context=self.workspace_path,
                             timestamp=_frame_timestamp()))

        ws_name = os.path.basename(self.workspace_path)
        dash = CTFDashboard(self.workspace_path)
        stats = dash.get_summary_stats()

        ctx = Text('  WORKSPACE\n', style=f'bold {FG_FAINT}')
        if stats.get('total_challenges', 0) > 0:
            title = stats.get('title', ws_name)
            plat = stats.get('platform', 'generic').upper()
            solved = stats.get('solved_challenges', 0)
            total = stats.get('total_challenges', 0)
            rate = stats.get('completion_rate', 0)
            pts = stats.get('earned_points', 0)
            tot_pts = stats.get('total_points', 0)

            ctx.append('  ')
            ctx.append(str(title), style=f'bold {FG_BASE}')
            ctx.append(f'  ·  {plat}', style=FG_MUTED)
            ctx.append('\n  ')
            ctx.append(self.workspace_path, style=INFO)

            bar_len = 20
            ctx.append('\n  ')
            ctx.append_text(meter(rate, bar_len, AMBER_RAMP))
            ctx.append(f'  {solved}/{total} solved · {rate:.1f}%', style=FG_MUTED)
            ctx.append(f'  ·  {pts}/{tot_pts} pts', style=FG_MUTED)

            if stats.get('user') or stats.get('team'):
                u = stats.get('user', '')
                t = f" (Team: {stats.get('team')})" if stats.get('team') else ''
                ctx.append('\n  ')
                ctx.append(f'{u}{t}' if u else t.strip(), style=FG_MUTED)
        else:
            ctx.append('  ')
            ctx.append(self.workspace_path, style=INFO)
            ctx.append('  (chưa có giải đấu nào ở đây)', style=FG_MUTED)

        ctx.append('\n  ')
        if self.cookie or self.token:
            ctx.append('cookie/token: đã lưu cho giải này', style=FG_MUTED)
        else:
            ctx.append('! cookie/token chưa cấu hình — dùng [9] để lưu',
                       style=WARN)
        con.print(ctx)

    def run(self):
        while True:
            # C12-M1: EOF/Ctrl-D (và Ctrl-C) ở prompt BẤT KỲ — kể cả prompt
            # con trong submenu — phải thoát sạch lịch sự, không nổ
            # EOFError/KeyboardInterrupt traceback ra ngoài run().
            try:
                self._load_saved_auth()
                self._print_header()

                _section('Chức năng')
                for key, label in (
                    ('1', 'Clone / Tải giải đấu CTF mới về máy'),
                    ('2', 'Chọn / Chuyển đổi Workspace giải đấu đang làm việc'),
                    ('3', 'Xem Cây Cấu trúc & Tiến độ bài thi (Tree View)'),
                    ('4', 'Tra cứu & Xem chi tiết đề bài, hints, file đính kèm'),
                    ('5', 'Quản lý Container / Instance động (bật / tắt / gia hạn / trạng thái)'),
                    ('6', 'Nộp flag cho một bài thi cụ thể'),
                    ('7', 'Tự động quét & nộp hàng loạt flag đã giải trong workspace'),
                    ('8', 'Quét & tổng kết toàn bộ các giải đấu trên máy'),
                    ('9', 'Cấu hình & Lưu Cookie / Token cho giải này (nhớ vĩnh viễn)'),
                    ('0', 'Thoát'),
                ):
                    # §S1.1: option là hành động gần nhất → dòng ❯ reverse;
                    # option thường giữ _option() nguyên trạng.
                    if key == self._last_action:
                        _menu_console().print(
                            selected_row(f'[{key}] {label}', selected=True))
                    else:
                        _option(key, label)
                _footer()

                choice = _prompt('Chọn chức năng (0-9): ')

                if choice == '0':
                    _menu_console().print(
                        Text('\nTạm biệt! Chúc bạn thi đấu CTF đạt kết quả cao.\n',
                             style=FG_MUTED))
                    break
                elif choice == '1':
                    self._menu_download_new()
                elif choice == '2':
                    self._menu_switch_workspace()
                elif choice == '3':
                    self._menu_view_tree()
                elif choice == '4':
                    self._menu_view_challenge_detail()
                elif choice == '5':
                    self._menu_container_manager()
                elif choice == '6':
                    self._menu_submit_flag()
                elif choice == '7':
                    self._menu_auto_submit()
                elif choice == '8':
                    self._menu_scan_workspaces()
                elif choice == '9':
                    self._menu_configure_auth()
                else:
                    Logger.warning('Lựa chọn không hợp lệ. Vui lòng chọn số từ 0 đến 9.')
                # Ghi nhớ hành động gần nhất để vòng sau đánh dấu ❯ (§S1.1);
                # input lạ ('x', '99') không được tính là action.
                if len(choice) == 1 and '1' <= choice <= '9':
                    self._last_action = choice
            except (EOFError, KeyboardInterrupt):
                _menu_console().print(
                    Text('\nTạm biệt! Chúc bạn thi đấu CTF đạt kết quả cao.\n',
                         style=FG_MUTED))
                break

    def _menu_download_new(self):
        _section('Tải & khởi tạo giải đấu CTF mới')
        url = _prompt('URL sàn CTF (ví dụ: https://ctf.example.com): ').strip()
        if not url:
            Logger.warning('URL không được để trống.')
            return

        con = _menu_console()
        con.print()
        con.print('  Phương thức xác thực:', style=FG_MUTED)
        _option('1', 'Session Cookie (F12 -> Cookies -> copy session=xxx hoặc GZCTF_Token=xxx)')
        _option('2', 'API Token / Bearer Token')
        _option('3', 'Không cần đăng nhập (sàn public)')
        ach = _prompt('Lựa chọn (1-3) [mặc định 1]: ') or '1'

        cookie = None
        token = None
        if ach == '1':
            cookie = _prompt('Dán Cookie: ').strip()
        elif ach == '2':
            token = _prompt('Dán Token: ').strip()

        out = _prompt('Thư mục lưu (Enter để tự lưu vào ~/Workspace/CTF/<Tên_Giải>): ').strip()
        out_dir = out if out else None

        cfg = DownloaderConfig(
            url=url,
            cookie=cookie,
            token=token,
            output_dir=out_dir,
            download_third_party=True,
            create_solve_template=True,
            threads=4
        )

        try:
            dl = CTFDownloader(cfg)
            if dl.run():
                Logger.success('Tải giải đấu hoàn tất!')
                if dl.output_dir and os.path.exists(dl.output_dir):
                    self.workspace_path = dl.output_dir
                    self.cookie = cookie
                    self.token = token
                    self._save_current_workspace()
        except Exception as e:
            Logger.error(f'Quá trình tải thất bại: {e}')

    def _menu_switch_workspace(self):
        base_ctf = os.path.expanduser('~/Workspace/CTF')
        workspaces = []
        if os.path.exists(base_ctf):
            for d in sorted(os.listdir(base_ctf)):
                p = os.path.join(base_ctf, d)
                if os.path.isdir(p):
                    dash = CTFDashboard(p)
                    st = dash.get_summary_stats()
                    if st.get('total_challenges', 0) > 0:
                        workspaces.append((p, st))

        _section('Chọn workspace giải đấu đang làm việc')
        if not workspaces:
            Logger.warning('Chưa có workspace nào trong ~/Workspace/CTF.')
            custom_p = _prompt('Nhập đường dẫn thư mục giải đấu: ').strip()
            if os.path.exists(custom_p):
                self.workspace_path = os.path.abspath(custom_p)
                self._save_current_workspace()
            return

        con = _menu_console()
        active = os.path.abspath(self.workspace_path)
        for row in _workspace_rows(workspaces, active):
            con.print(row)

        _option('C', 'Nhập đường dẫn thư mục tuỳ chỉnh')
        _option('0', 'Quay lại')
        ch = _prompt(f'Chọn Workspace (1-{len(workspaces)}): ').strip()
        if ch == '0':
            return
        elif ch.upper() == 'C':
            custom_p = _prompt('Nhập đường dẫn: ').strip()
            if os.path.exists(custom_p):
                self.workspace_path = os.path.abspath(custom_p)
                self.cookie = None
                self.token = None
                self._load_saved_auth()
                self._save_current_workspace()
        else:
            try:
                sel_idx = int(ch) - 1
                # C12-M2: '00'/'-0'/'-1' tạo index ÂM — Python chọn lặng lẽ
                # workspace cuối + ghi config. Bắt buộc check biên.
                if not 0 <= sel_idx < len(workspaces):
                    raise IndexError(sel_idx)
                self.workspace_path = workspaces[sel_idx][0]
                self.cookie = None
                self.token = None
                self._load_saved_auth()
                self._save_current_workspace()
                Logger.success(f"Đã chuyển sang workspace: {os.path.basename(self.workspace_path)}")
            except Exception:
                Logger.error('Lựa chọn không hợp lệ.')

    def _save_current_workspace(self):
        """Persist workspace mặc định (+auth nếu có) NGUYÊN TỬ qua khóa
        flock — đọc-mutate-ghi trên state HIỆN HÀNH trên đĩa qua
        ``update_global_config`` (review c18-2, MED).

        Trước đây: ``save_global_config(self.config)`` với self.config là
        snapshot chụp LÚC MỞ MENU — cửa sổ RMW dài nhất repo (menu mở cả
        phiên): register_state/auth do tiến trình khác (vd ``ctf register``)
        ghi giữa chừng bị bản stale đè MẤT. Mutator chỉ chép giá trị của
        phiên này vào dict fresh, không giữ reference cũ; cache nội bộ được
        refresh từ state sau ghi."""
        ws = self.workspace_path
        cookie, token = self.cookie, self.token

        def _mut(fresh):
            fresh['default_workspace'] = ws
            if cookie or token:
                fresh.setdefault('auth', {})[ws] = {
                    'cookie': cookie,
                    'token': token
                }
            return fresh

        try:
            saved_state = update_global_config(_mut)
        except OSError as e:
            # Storage hỏng (PermissionError...) — menu không crash, log rõ.
            Logger.warning(f'Không lưu được config: {e}')
            return
        if saved_state is not None:
            self.config = saved_state

    def _menu_view_tree(self):
        dash = CTFDashboard(self.workspace_path)
        _section('Tuỳ chọn hiển thị cây bài thi')
        _option('1', 'Hiển thị TẤT CẢ bài thi')
        _option('2', 'Chỉ hiển thị các bài CHƯA GIẢI (unsolved)')
        _option('3', 'Chỉ hiển thị các bài ĐÃ GIẢI (solved)')
        _option('4', 'Chỉ hiển thị các bài có DYNAMIC CONTAINER')
        _option('5', 'Lọc theo thể loại (Web, Crypto, Pwn, Rev, Forensics, Misc)')
        fch = _prompt('Lựa chọn (1-5) [mặc định 1]: ') or '1'

        if fch == '2':
            dash.render_tree(only_unsolved=True)
        elif fch == '3':
            dash.render_tree(only_solved=True)
        elif fch == '4':
            dash.render_tree(only_container=True)
        elif fch == '5':
            cat_in = _prompt('Nhập thể loại (ví dụ: Web Crypto): ').strip().split()
            dash.render_tree(filter_cat=cat_in)
        else:
            dash.render_tree()
        _pause()

    def _menu_view_challenge_detail(self):
        dash = CTFDashboard(self.workspace_path)
        challs = dash.local_challenges
        if not challs:
            Logger.warning('Chưa có bài thi nào trong workspace hiện tại.')
            return

        q = _prompt('Nhập ID bài hoặc Tên bài: ').strip()
        if not q:
            return
        target = next((c for c in challs if str(c.get('id')) == q or q.lower() in c.get('name', '').lower()), None)
        if not target:
            Logger.error(f'Không tìm thấy bài thi "{q}".')
            return

        folder = target.get('_folder', '')
        readme_p = os.path.join(folder, 'README.md')

        con = _menu_console()
        con.print()
        # §S1.3: candidate khớp đầu tiên đánh dấu ❯ + reverse highlight.
        head = selected_row(str(target.get('name')), selected=True)
        head.append(f"  ID: {target.get('id')}  ", style=FG_FAINT)
        if target.get('solved_by_me'):
            head.append('✔ ĐÃ GIẢI', style='solved')
        else:
            head.append('· CHƯA GIẢI', style=FG_FAINT)
        con.print(head)

        meta = Text('  Thể loại: ')
        meta.append(str(target.get('category')), style=FG_BASE)
        meta.append('  ·  ', style=FG_FAINT)
        meta.append(f"{target.get('points')} pts", style=FG_MUTED)
        meta.append('  ·  ', style=FG_FAINT)
        meta.append(f"{target.get('solves_count', '-')} giải", style=FG_MUTED)
        con.print(meta)

        loc = Text('  Thư mục local: ')
        loc.append(str(target.get('_rel_folder')), style=INFO)
        con.print(loc)
        if target.get('connection_info'):
            ci = Text('  Kết nối: ')
            ci.append(str(target.get('connection_info')), style=INFO)
            con.print(ci)

        if os.path.exists(readme_p):
            con.print(Text('  Nội dung đề bài (README.md)', style=f'bold {FG_FAINT}'))
            with open(readme_p, 'r', encoding='utf-8') as rf:
                con.print(rf.read()[:2000])
        _pause()

    def _menu_container_manager(self):
        try:
            mgr = InstanceManager(self.workspace_path, cookie=self.cookie, token=self.token)
        except Exception as e:
            Logger.error(f'Không thể khởi tạo Container Manager: {e}')
            return

        containers = mgr.list_containers()
        _section('Quản lý dynamic container / instance')
        if not containers:
            Logger.info('Không có bài nào hỗ trợ Dynamic Container trong workspace này.')
            return

        con = _menu_console()
        for idx, c in enumerate(containers, 1):
            solves = c.get('solves_count', c.get('solves', '-'))
            c_name = str(c.get('name', 'Unknown'))[:30]
            c_id = str(c.get('id', '?'))
            c_cat = c.get('category', 'Misc')
            row = Text('  ')
            row.append(f'[{idx:>2}]', style=ACCENT)
            row.append(f' {c_name:<30}', style=FG_BASE)
            row.append(f'ID {c_id:<4}', style=FG_MUTED)
            row.append(f' {c_cat}', style=FG_MUTED)
            row.append(f'  ·  {solves} giải', style=FG_MUTED)
            con.print(row)

        ch = _prompt(f'Chọn bài để thao tác Container (1-{len(containers)}), hoặc nhập ID [0 để huỷ]: ').strip()
        if ch == '0' or not ch:
            return

        if ch.isdigit() and not ch.isdecimal():
            # C13-MENU1: '²'/'①' trông như số nhưng int() không parse được —
            # từ chối sạch thay vì nổ ValueError ngoài mọi try.
            Logger.warning(f'Chọn bằng số thập phân hoặc nhập ID: {ch}')
            return

        if ch.isdecimal() and 1 <= int(ch) <= len(containers):
            target_chall = containers[int(ch) - 1]
            cid = target_chall.get('id')
        else:
            cid = ch

        con.print()
        con.print(f'  Hành động cho Challenge ID {cid}:', style=FG_MUTED)
        _option('1', 'Bật / Khởi tạo container (lấy IP:Port & netcat command)')
        _option('2', 'Kiểm tra trạng thái & thời gian sống còn lại')
        _option('3', 'Gia hạn thời gian sống (extend countdown)')
        _option('4', 'Tắt / Giải phóng container')
        _option('0', 'Quay lại')
        act = _prompt('Lựa chọn (1-4): ').strip()

        if act == '1':
            mgr.start_instance(cid)
        elif act == '2':
            st = mgr.get_status(cid)
            Logger.info(f'Trạng thái ID {cid}: {st}')
        elif act == '3':
            mgr.extend_instance(cid)
        elif act == '4':
            mgr.stop_instance(cid)
        _pause()

    def _menu_submit_flag(self):
        dash = CTFDashboard(self.workspace_path)
        challs = dash.local_challenges
        _section('Nộp flag cho bài thi')

        q = _prompt('Nhập ID bài hoặc Tên bài: ').strip()
        if not q:
            return

        target = next((c for c in challs if str(c.get('id')) == q or q.lower() in c.get('name', '').lower()), None)
        target_id = target.get('id') if target else (q if q.isdigit() else None)
        target_name = target.get('name') if target else (q if not q.isdigit() else None)

        if target:
            sel = Text('  Đã chọn: ')
            sel.append(str(target.get('name')), style=f'bold {FG_BASE}')
            sel.append(f"  (ID: {target.get('id')}, Thể loại: {target.get('category')})",
                       style=FG_MUTED)
            _menu_console().print(sel)

        flag_str = _prompt('Nhập chuỗi Flag: ').strip()
        if not flag_str:
            Logger.warning('Flag không được để trống.')
            return

        sub = FlagSubmitter(
            workspace_dir=self.workspace_path,
            cookie=self.cookie,
            token=self.token
        )
        sub.submit_single_flag(
            challenge_id=target_id,
            challenge_name=target_name,
            flag_value=flag_str
        )
        _pause()

    def _menu_auto_submit(self):
        _section('Tự động quét & nộp toàn bộ flag đã giải trong workspace')
        confirm = Confirm.ask('Bạn có muốn quét tất cả README.md và nộp tự động các flag đã điền?', default=True)
        if confirm:
            sub = FlagSubmitter(
                workspace_dir=self.workspace_path,
                cookie=self.cookie,
                token=self.token
            )
            sub.auto_submit_all()
        _pause()

    def _menu_scan_workspaces(self):
        base_dir = os.path.expanduser('~/Workspace/CTF')
        # Bản duy nhất của bảng scan nằm ở StatusService.scan_all_workspaces
        # (dùng chung với cli handle_workspaces / manage.py -A)
        StatusService.scan_all_workspaces(base_dir)
        _pause()

    def _menu_configure_auth(self):
        _section(f'Cấu hình xác thực cho: {os.path.basename(self.workspace_path)}')
        con = _menu_console()
        ck_show = self.cookie if self.cookie else '(Chưa có)'
        tk_show = self.token if self.token else '(Chưa có)'
        cur = Text('  Cookie hiện tại: ')
        cur.append(ck_show, style=INFO if self.cookie else FG_FAINT)
        con.print(cur)
        cur2 = Text('  Token hiện tại : ')
        cur2.append(tk_show, style=INFO if self.token else FG_FAINT)
        con.print(cur2)

        con.print()
        _option('1', 'Nhập / Dán Session Cookie mới (session=... hoặc GZCTF_Token=...)')
        _option('2', 'Nhập API Token / Bearer Token')
        _option('3', 'Xoá thông tin xác thực đã lưu')
        _option('0', 'Quay lại')
        ch = _prompt('Lựa chọn (0-3): ').strip()

        if ch == '1':
            c_in = _prompt('Dán Cookie: ').strip()
            if os.path.isfile(c_in):
                with open(c_in, 'r', encoding='utf-8') as f:
                    self.cookie = f.read().strip()
            else:
                self.cookie = c_in
            self._save_current_workspace()
            Logger.success('Đã lưu Cookie thành công vĩnh viễn cho giải này!')
        elif ch == '2':
            self.token = _prompt('Dán API/Bearer Token: ').strip()
            self._save_current_workspace()
            Logger.success('Đã lưu Token thành công vĩnh viễn cho giải này!')
        elif ch == '3':
            self.cookie = None
            self.token = None
            self._save_current_workspace()
            Logger.info('Đã xoá thông tin xác thực.')


def _pause():
    _prompt('Nhấn Enter để quay lại...')


def launch_interactive_menu(workspace_path: Optional[str] = None, cookie: Optional[str] = None, token: Optional[str] = None):
    # Splash logo dual-tier (DECISION_LOGO.md §4): cand_1 big ≥80 cols,
    # cand_6 pagga <80 — in ĐÚNG MỘT LẦN khi vào menu, TRƯỚC radar AppHeader
    # đầu tiên của vòng lặp; các lệnh framed không đổi (vẫn radar 4 dòng).
    _menu_console().print(splash())
    # Banner PHOSPHOR FIELD KIT phương án B (spec §2) — human-facing → stderr.
    app = CTFInteractiveConsole(workspace_path=workspace_path, cookie=cookie, token=token)
    app.run()
