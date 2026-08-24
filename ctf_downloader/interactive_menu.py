import os
import sys
import glob
from typing import Optional
from rich.prompt import Prompt, Confirm
from rich.text import Text
from .dashboard import CTFDashboard
from .instance_manager import InstanceManager
from .submitter import FlagSubmitter
from .core import CTFDownloader
from .config import DownloaderConfig
from .utils.logger import Logger, console

from .services.status_service import StatusService
from .storage.global_config import (  # noqa: F401 — re-export để giữ tương thích
    CONFIG_DIR,
    GLOBAL_CONFIG_FILE,
    load_global_config,
    save_global_config,
)

class CTFInteractiveConsole:
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

    def run(self):
        while True:
            self._load_saved_auth()
            self._print_header()
            print('  [1] 📥 Clone / Tải giải đấu CTF mới về máy')
            print('  [2] 📂 Chọn / Chuyển đổi Workspace giải đấu đang làm việc')
            print('  [3] 📊 Xem Cây Cấu trúc & Tiến độ Bài thi (Tree View)')
            print('  [4] 🔍 Tra cứu & Xem Chi tiết Đề bài, Hints, File đính kèm')
            print('  [5] 🐳 Quản lý Container / Instance Động (Bật / Tắt / Gia hạn / Trạng thái)')
            print('  [6] 🚩 Nộp Flag cho 1 Bài thi cụ thể')
            print('  [7] ⚡ Tự động Quét & Nộp Hàng loạt Flag đã giải trong Workspace')
            print('  [8] 🌐 Quét & Tổng kết Toàn bộ các Giải đấu trên máy')
            print('  [9] ⚙️  Cấu hình & Lưu Cookie / Token cho Giải này (Nhớ vĩnh viễn)')
            print('  [0] 🚪 Thoát')
            print('=' * 80)

            choice = input('👉 Chọn chức năng (0-9): ').strip()

            if choice == '0':
                print('\n👋 Tạm biệt! Chúc bạn thi đấu CTF đạt kết quả cao!\n')
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

    def _print_header(self):
        print('\n' + '=' * 80)
        print('       ⚡ CTF TOOLKIT - UNIFIED INTERACTIVE CONSOLE v2.0 ⚡')
        print('=' * 80)
        
        ws_name = os.path.basename(self.workspace_path)
        dash = CTFDashboard(self.workspace_path)
        stats = dash.get_summary_stats()
        
        if stats.get('total_challenges', 0) > 0:
            title = stats.get('title', ws_name)
            plat = stats.get('platform', 'generic').upper()
            solved = stats.get('solved_challenges', 0)
            total = stats.get('total_challenges', 0)
            rate = stats.get('completion_rate', 0)
            pts = stats.get('earned_points', 0)
            tot_pts = stats.get('total_points', 0)
            
            bar_len = 20
            filled = int(bar_len * rate // 100)
            bar = '█' * filled + '░' * (bar_len - filled)
            
            print(f' 🎯 Active Workspace: [{title}] ({plat})')
            print(f' 📂 Đường dẫn: {self.workspace_path}')
            print(f' 📊 Tiến độ: {solved}/{total} Solved ({rate:.1f}%) | [{bar}] | Điểm: {pts}/{tot_pts} pts')
            if stats.get('user') or stats.get('team'):
                u = stats.get('user', '')
                t = f" (Team: {stats.get('team')})" if stats.get('team') else ''
                print(f' 👤 Tài khoản: {u}{t}')
        else:
            print(f' 📂 Thư mục: {self.workspace_path} (Chưa có giải đấu nào ở đây)')
        
        auth_status = '✅ Đã lưu cấu hình tự động' if (self.cookie or self.token) else '⚠️ Chưa cấu hình (Dùng [9] để lưu Cookie/Token)'
        print(f' 🔑 Cookie/Token: {auth_status}')
        print('-' * 80)

    def _menu_download_new(self):
        print('\n' + '-' * 60)
        print('📥 TẢI & KHỞI TẠO GIẢI ĐẤU CTF MỚI')
        print('-' * 60)
        url = input('Nhập URL sàn CTF (ví dụ: https://ctf.example.com): ').strip()
        if not url:
            Logger.warning('URL không được để trống.')
            return

        print('\nChọn phương thức xác thực:')
        print('  [1] Session Cookie (F12 -> Cookies -> copy session=xxx hoặc GZCTF_Token=xxx)')
        print('  [2] API Token / Bearer Token')
        print('  [3] Không cần đăng nhập (Sàn Public)')
        ach = input('Lựa chọn (1-3) [mặc định 1]: ').strip() or '1'
        
        cookie = None
        token = None
        if ach == '1':
            cookie = input('Dán Cookie: ').strip()
        elif ach == '2':
            token = input('Dán Token: ').strip()

        out = input('Thư mục lưu (Nhấn Enter để tự động lưu vào ~/Workspace/CTF/<Tên_Giải>): ').strip()
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

        print('\n' + '-' * 60)
        print('📂 CHỌN WORKSPACE GIẢI ĐẤU ĐANG LÀM VIỆC')
        print('-' * 60)
        if not workspaces:
            Logger.warning('Chưa có workspace nào trong ~/Workspace/CTF.')
            custom_p = input('Nhập đường dẫn thư mục giải đấu: ').strip()
            if os.path.exists(custom_p):
                self.workspace_path = os.path.abspath(custom_p)
                self._save_current_workspace()
            return

        for idx, (p, st) in enumerate(workspaces, 1):
            cur = ' ⭐ [ACTIVE]' if os.path.abspath(p) == os.path.abspath(self.workspace_path) else ''
            title = st.get('title', os.path.basename(p))[:30]
            solv = f"{st.get('solved_challenges', 0)}/{st.get('total_challenges', 0)} Solved"
            plat_str = st.get('platform', 'generic').upper()
            print(f'  [{idx:>2}] {title:<30} | {plat_str:<8} | {solv:<16}{cur}')

        print('  [C] Nhập đường dẫn thư mục tùy chỉnh')
        print('  [0] Quay lại')
        ch = input(f'\nChọn Workspace (1-{len(workspaces)}): ').strip()
        if ch == '0':
            return
        elif ch.upper() == 'C':
            custom_p = input('Nhập đường dẫn: ').strip()
            if os.path.exists(custom_p):
                self.workspace_path = os.path.abspath(custom_p)
                self.cookie = None
                self.token = None
                self._load_saved_auth()
                self._save_current_workspace()
        else:
            try:
                sel_idx = int(ch) - 1
                self.workspace_path = workspaces[sel_idx][0]
                self.cookie = None
                self.token = None
                self._load_saved_auth()
                self._save_current_workspace()
                Logger.success(f"Đã chuyển sang workspace: {os.path.basename(self.workspace_path)}")
            except Exception:
                Logger.error('Lựa chọn không hợp lệ.')

    def _save_current_workspace(self):
        self.config['default_workspace'] = self.workspace_path
        if self.cookie or self.token:
            if 'auth' not in self.config:
                self.config['auth'] = {}
            self.config['auth'][self.workspace_path] = {
                'cookie': self.cookie,
                'token': self.token
            }
        save_global_config(self.config)

    def _menu_view_tree(self):
        dash = CTFDashboard(self.workspace_path)
        print('\nTuỳ chọn hiển thị cây bài thi:')
        print('  [1] Hiển thị TẤT CẢ bài thi')
        print('  [2] Chỉ hiển thị các bài CHƯA GIẢI (Unsolved)')
        print('  [3] Chỉ hiển thị các bài ĐÃ GIẢI (Solved)')
        print('  [4] Chỉ hiển thị các bài có DYNAMIC CONTAINER (🐳)')
        print('  [5] Lọc theo Thể loại (Web, Crypto, Pwn, Rev, Forensics, Misc)')
        fch = input('Lựa chọn (1-5) [mặc định 1]: ').strip() or '1'
        
        if fch == '2':
            dash.render_tree(only_unsolved=True)
        elif fch == '3':
            dash.render_tree(only_solved=True)
        elif fch == '4':
            dash.render_tree(only_container=True)
        elif fch == '5':
            cat_in = input('Nhập thể loại (ví dụ: Web Crypto): ').strip().split()
            dash.render_tree(filter_cat=cat_in)
        else:
            dash.render_tree()
        input('\nNhấn Enter để quay lại menu...')

    def _menu_view_challenge_detail(self):
        dash = CTFDashboard(self.workspace_path)
        challs = dash.local_challenges
        if not challs:
            Logger.warning('Chưa có bài thi nào trong workspace hiện tại.')
            return

        q = input('\nNhập ID bài hoặc Tên bài: ').strip()
        if not q:
            return
        target = next((c for c in challs if str(c.get('id')) == q or q.lower() in c.get('name', '').lower()), None)
        if not target:
            Logger.error(f'Không tìm thấy bài thi "{q}".')
            return

        folder = target.get('_folder', '')
        readme_p = os.path.join(folder, 'README.md')
        
        print('\n' + '=' * 75)
        status_str = '✅ ĐÃ GIẢI (SOLVED)' if target.get('solved_by_me') else '⏳ CHƯA GIẢI (UNSOLVED)'
        print(f"📌 {target.get('name')} (ID: {target.get('id')}) - [{status_str}]")
        print(f"Thể loại: {target.get('category')} | Điểm: {target.get('points')} | Số lượt giải: {target.get('solves_count', '-')}")
        print(f"Thư mục local: {target.get('_rel_folder')}")
        if target.get('connection_info'):
            print(f"🔌 Kết nối: {target.get('connection_info')}")
        
        if os.path.exists(readme_p):
            print('\n--- 📝 Nội dung đề bài (README.md) ---')
            with open(readme_p, 'r', encoding='utf-8') as rf:
                print(rf.read()[:2000])
        print('=' * 75)
        input('\nNhấn Enter để quay lại...')

    def _menu_container_manager(self):
        try:
            mgr = InstanceManager(self.workspace_path, cookie=self.cookie, token=self.token)
        except Exception as e:
            Logger.error(f'Không thể khởi tạo Container Manager: {e}')
            return

        containers = mgr.list_containers()
        print('\n' + '-' * 60)
        print('🐳 QUẢN LÝ DYNAMIC CONTAINER / INSTANCE')
        print('-' * 60)
        if not containers:
            Logger.info('Không có bài nào hỗ trợ Dynamic Container trong workspace này.')
            return

        for idx, c in enumerate(containers, 1):
            solves = c.get('solves_count', c.get('solves', '-'))
            c_name = c.get('name', 'Unknown')[:30]
            c_id = str(c.get('id', '?'))
            c_cat = c.get('category', 'Misc')
            print(f'  [{idx:>2}] {c_name:<30} (ID: {c_id:<4}, {c_cat}) - {solves} solves')

        ch = input(f'\nChọn bài để thao tác Container (1-{len(containers)}), hoặc nhập ID [0 để huỷ]: ').strip()
        if ch == '0' or not ch:
            return
        
        if ch.isdigit() and 1 <= int(ch) <= len(containers):
            target_chall = containers[int(ch) - 1]
            cid = target_chall.get('id')
        else:
            cid = ch

        print(f'\n⚡ Chọn hành động cho Challenge ID {cid}:')
        print('  [1] 🚀 Bật / Khởi tạo Container (Lấy IP:Port & Netcat Command)')
        print('  [2] 🔍 Kiểm tra trạng thái & Thời gian sống còn lại')
        print('  [3] ⏳ Gia hạn thời gian sống (Extend Countdown)')
        print('  [4] 🛑 Tắt / Giải phóng Container')
        print('  [0] Quay lại')
        act = input('Lựa chọn (1-4): ').strip()
        
        if act == '1':
            mgr.start_instance(cid)
        elif act == '2':
            st = mgr.get_status(cid)
            Logger.info(f'Trạng thái ID {cid}: {st}')
        elif act == '3':
            mgr.extend_instance(cid)
        elif act == '4':
            mgr.stop_instance(cid)
        input('\nNhấn Enter để quay lại...')

    def _menu_submit_flag(self):
        dash = CTFDashboard(self.workspace_path)
        challs = dash.local_challenges
        print('\n' + '-' * 60)
        print('🚩 NỘP FLAG CHO BÀI THI')
        print('-' * 60)

        q = input('Nhập ID bài hoặc Tên bài: ').strip()
        if not q:
            return
        
        target = next((c for c in challs if str(c.get('id')) == q or q.lower() in c.get('name', '').lower()), None)
        target_id = target.get('id') if target else (q if q.isdigit() else None)
        target_name = target.get('name') if target else (q if not q.isdigit() else None)

        if target:
            print(f"Đã chọn: {target.get('name')} (ID: {target.get('id')}, Thể loại: {target.get('category')})")

        flag_str = input('Nhập chuỗi Flag: ').strip()
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
        input('\nNhấn Enter để quay lại...')

    def _menu_auto_submit(self):
        print('\n' + '-' * 60)
        print('⚡ TỰ ĐỘNG QUÉT & NỘP TOÀN BỘ FLAG ĐÃ GIẢI TRONG WORKSPACE')
        print('-' * 60)
        confirm = Confirm.ask('Bạn có muốn quét tất cả README.md và nộp tự động các flag đã điền?', default=True)
        if confirm:
            sub = FlagSubmitter(
                workspace_dir=self.workspace_path,
                cookie=self.cookie,
                token=self.token
            )
            sub.auto_submit_all()
        input('\nNhấn Enter để quay lại...')

    def _menu_scan_workspaces(self):
        base_dir = os.path.expanduser('~/Workspace/CTF')
        # Bản duy nhất của bảng scan nằm ở StatusService.scan_all_workspaces
        # (dùng chung với cli handle_workspaces / manage.py -A)
        StatusService.scan_all_workspaces(base_dir)
        input('\nNhấn Enter để quay lại...')

    def _menu_configure_auth(self):
        print('\n' + '-' * 60)
        print(f'⚙️  CẤU HÌNH XÁC THỰC CHO: {os.path.basename(self.workspace_path)}')
        print('-' * 60)
        ck_show = self.cookie if self.cookie else '(Chưa có)'
        tk_show = self.token if self.token else '(Chưa có)'
        print(f'Cookie hiện tại: {ck_show}')
        print(f'Token hiện tại : {tk_show}')
        print('\nTuỳ chọn:')
        print('  [1] Nhập / Dán Session Cookie mới (session=... hoặc GZCTF_Token=...)')
        print('  [2] Nhập API Token / Bearer Token')
        print('  [3] Xoá thông tin xác thực đã lưu')
        print('  [0] Quay lại')
        ch = input('Lựa chọn (0-3): ').strip()
        
        if ch == '1':
            c_in = input('Dán Cookie: ').strip()
            if os.path.isfile(c_in):
                with open(c_in, 'r', encoding='utf-8') as f:
                    self.cookie = f.read().strip()
            else:
                self.cookie = c_in
            self._save_current_workspace()
            Logger.success('Đã lưu Cookie thành công vĩnh viễn cho giải này!')
        elif ch == '2':
            self.token = input('Dán API/Bearer Token: ').strip()
            self._save_current_workspace()
            Logger.success('Đã lưu Token thành công vĩnh viễn cho giải này!')
        elif ch == '3':
            self.cookie = None
            self.token = None
            self._save_current_workspace()
            Logger.info('Đã xoá thông tin xác thực.')

def launch_interactive_menu(workspace_path: Optional[str] = None, cookie: Optional[str] = None, token: Optional[str] = None):
    # Banner PHOSPHOR FIELD KIT phương án B (spec §2) — human-facing → stderr.
    from rich.console import Group

    from .ui.banner import banner_b, tagline_text
    from .ui.console import err_console
    err_console.print(Group(banner_b(), tagline_text(), Text("")))
    app = CTFInteractiveConsole(workspace_path=workspace_path, cookie=cookie, token=token)
    app.run()
