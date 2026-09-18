import os
import sys
import time
import glob
from typing import Optional

from rich.prompt import Confirm
from rich.live import Live
from rich.text import Text

from .dashboard import CTFDashboard
from .instance_manager import InstanceManager
from .submitter import FlagSubmitter
from .core import CTFDownloader
from .config import DownloaderConfig
from .utils.logger import Logger
from .utils.sanitize import sanitize_cookie_input
from .services.auth_service import AuthService

from .services.status_service import StatusService
from .storage.global_config import (  # noqa: F401 — re-export để giữ tương thích
    CONFIG_DIR,
    GLOBAL_CONFIG_FILE,
    load_global_config,
    resolve_workspace_root,
    save_global_config,
    update_global_config,
)

# PHOSPHOR FIELD KIT (design-system spec §2/§3) — AppHeader radar + tokens.
from .ui.banner import app_header
from .ui.selection import MENU_CURSOR, fit_cells, selected_row
from .ui.splash import splash
from .ui.theme import ACCENT, FG_BASE, FG_FAINT, FG_MUTED, INFO, WARN, load_theme
from .ui.widgets import SOLVE_RAMP, meter

#: Meter dùng chung ramp 3 mốc spec §3.3 (than hồng → hổ phách → vàng nhạt)
#: — ``ui.widgets.AMBER_RAMP`` canonical theo SPEC UI v2 §M1: mỗi ô nhận
#: ĐÚNG một trong ba màu theo vị trí cột, không nội suy trung gian.

#: Độ rộng cột switcher workspace (SPEC UI v2 §S1.2) — cắt theo display
#: width kèm ``…`` (MUST uiv2 #4), không bao giờ để dữ liệu tràn cột.
SWITCHER_TITLE_W = 30
SWITCHER_PLATFORM_W = 8

_MAIN_ACTIONS_FULL = (
    ('1', 'Clone / Download new CTF challenge files'),
    ('2', 'Select / Switch active competition workspace'),
    ('3', 'View challenge tree & progress (Tree View)'),
    ('4', 'Lookup & view challenge description, hints, files'),
    ('5', 'Manage dynamic container / instance (start / stop / renew)'),
    ('6', 'Submit flag for a specific challenge'),
    ('7', 'Auto scan & submit hoarded flags in workspace'),
    ('8', 'Scan & summarize all CTF workspaces on machine'),
    ('9', 'Configure & save Cookie / Token for this event'),
    ('S', 'SUPERBQA EATING'),
    ('0', 'Exit'),
)

_MAIN_ACTIONS_COMPACT = (
    ('1', 'Clone / Download CTF event'),
    ('2', 'Select / Switch workspace'),
    ('3', 'Challenge tree & progress'),
    ('4', 'View challenge / hints / files'),
    ('5', 'Container / Instance: status & renew'),
    ('6', 'Submit flag'),
    ('7', 'Auto-submit hoarded flags'),
    ('8', 'Summarize local workspaces'),
    ('9', 'Configure Cookie / Token'),
    ('S', 'SUPERBQA EATING'),
    ('0', 'Exit'),
)


def _main_menu_actions(width: int):
    """Return one-line action labels appropriate for the terminal width."""
    return _MAIN_ACTIONS_COMPACT if width < 72 else _MAIN_ACTIONS_FULL


_MENU_CON = None


def _run_live_radar(service, con):
    """Display the solver status as a continuously refreshed live radar."""
    from .cli_commands import _solver_table

    con.print("\n  [bold cyan]Live Radar[/bold cyan] — Press Ctrl+C to detach.\n")
    try:
        with Live(_solver_table(service, show_worker_count=True), console=con, refresh_per_second=4) as live:
            while True:
                time.sleep(0.25)
                live.update(_solver_table(service, show_worker_count=True))
    except KeyboardInterrupt:
        con.print("\n  [dim]Detached from Live Radar.[/dim]")


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


def _resolve_challenge_selection(challs: list[dict], query: str) -> tuple[Optional[dict], Optional[str]]:
    """Resolve a challenge selection query from interactive menus.

    Returns:
        (challenge_dict, None) on success
        (None, None) if cancelled (empty string or '0')
        (None, error_message) if resolution failed or was ambiguous
    """
    if not isinstance(query, str):
        return None, None
    q = query.strip()
    if not q or q == '0':
        return None, None

    valid_challs = [c for c in challs if isinstance(c, dict)]
    if not valid_challs:
        return None, "No challenges available to select."

    # 1. Explicit syntax: @<index> for positional index
    if q.startswith("@"):
        idx_str = q[1:].strip()
        if idx_str.isdecimal():
            idx = int(idx_str)
            if 1 <= idx <= len(valid_challs):
                return valid_challs[idx - 1], None
            return None, f"Row index @{idx_str} is out of range (1-{len(valid_challs)})."
        return None, f"Invalid row index: {q}"

    # 2. Explicit ID prefixes (#<id> or id:<id>)
    if q.startswith("#") or q.lower().startswith("id:"):
        target_id = q[1:].strip() if q.startswith("#") else q[3:].strip()
        matched = [c for c in valid_challs if c.get('id') is not None and str(c['id']).strip() == target_id]
        if len(matched) == 1:
            return matched[0], None
        if len(matched) > 1:
            names = ", ".join(f"'{c.get('name')}'" for c in matched[:3])
            return None, f'Multiple ({len(matched)}) challenges share ID "{target_id}" ({names}). Please use challenge name (name:...) to select.'
        return None, f'No challenge found with ID {q}.'

    # 3. Explicit Name prefix (name:<name>)
    if q.lower().startswith("name:"):
        raw_name = q[5:].strip().casefold()
        exact = [c for c in valid_challs if str(c.get('name') or '').strip().casefold() == raw_name]
        if len(exact) == 1:
            return exact[0], None
        if len(exact) > 1:
            cids = ", ".join(str(c.get('id', '?')) for c in exact)
            return None, f'Multiple ({len(exact)}) challenges share exact name "{raw_name}" (IDs: {cids}). Please use #<ID> to select.'
        substr = [c for c in valid_challs if raw_name in str(c.get('name') or '').casefold()]
        if len(substr) == 1:
            return substr[0], None
        if len(substr) > 1:
            cands = ", ".join(f"[{c.get('id')}] {c.get('name')}" for c in substr[:4])
            return None, f'Found {len(substr)} challenges matching "{raw_name}": {cands}. Please be more specific or use #<ID>.'
        return None, f'No challenge found matching name "{raw_name}".'

    # 4. Standard query resolution:
    q_norm = q.casefold()
    exact_names = [
        c for c in valid_challs
        if str(c.get('name') or '').strip().casefold() == q_norm
    ]

    id_matches = [
        c for c in valid_challs
        if c.get('id') is not None and str(c['id']).strip() == q
    ]

    index_match = None
    if q.isdecimal():
        idx = int(q)
        if 1 <= idx <= len(valid_challs):
            index_match = valid_challs[idx - 1]

    # Check for cross-tier ambiguity between exact name, ID, and row index
    distinct_candidates = set()
    for c in exact_names:
        distinct_candidates.add(id(c))
    for c in id_matches:
        distinct_candidates.add(id(c))
    if index_match is not None:
        distinct_candidates.add(id(index_match))

    if len(distinct_candidates) > 1:
        id_desc = f"challenge with ID \"{q}\" ('{id_matches[0].get('name')}')" if id_matches else None
        idx_desc = f"row [{q}] ('{index_match.get('name')}')" if index_match else None
        name_desc = f"challenge named '{q}' (ID #{exact_names[0].get('id')})" if exact_names else None
        parts = [p for p in (id_desc, idx_desc, name_desc) if p]
        return None, (
            f'Selection "{q}" is ambiguous between {", ".join(parts)}.\n'
            f'  -> Please use @{q} for row index, #{q} for ID, or name:{q} for name.'
        )

    if len(exact_names) == 1:
        return exact_names[0], None
    if len(exact_names) > 1:
        cids = ", ".join(str(c.get('id', '?')) for c in exact_names)
        return None, f'Multiple ({len(exact_names)}) challenges share exact name "{q}" (IDs: {cids}). Please use #<ID> to select.'

    if len(id_matches) == 1:
        return id_matches[0], None
    if len(id_matches) > 1:
        names = ", ".join(f"'{c.get('name')}'" for c in id_matches[:3])
        return None, f'Multiple ({len(id_matches)}) challenges share ID "{q}" ({names}). Please use @<row> or name:... to select.'

    if index_match is not None:
        return index_match, None

    # 5. Substring name match
    sub_names = [
        c for c in valid_challs
        if q_norm in str(c.get('name') or '').casefold()
    ]
    if len(sub_names) == 1:
        return sub_names[0], None
    if len(sub_names) > 1:
        cands = ", ".join(f"[{c.get('id')}] {c.get('name')}" for c in sub_names[:4])
        if len(sub_names) > 4:
            cands += f" ... (+{len(sub_names) - 4} more)"
        return None, f'Found {len(sub_names)} challenges matching "{q}": {cands}. Please be more specific or use #<ID>.'

    return None, f'No challenge found matching "{q}".'


class CTFInteractiveConsole:
    # §S1: option là hành động gần nhất → ❯ reverse. Default lớp để an toàn
    # với instance dựng qua __new__ (test harness bỏ qua __init__).
    _last_action: Optional[str] = None

    def __init__(self, workspace_path: Optional[str] = None, cookie: Optional[str] = None, token: Optional[str] = None):
        self.config = load_global_config()
        self.workspace_path = self._resolve_initial_workspace(workspace_path)
        self.cookie = cookie
        self.token = token
        self._suppress_next_brand = False
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
        base_ctf = resolve_workspace_root()
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
        """Render one compact menu identity + workspace state block.

        The full splash owns branding on the first frame, so the immediate
        duplicate AppHeader is suppressed once. Routine auth success, full
        paths and user/team IDs stay hidden; only actionable warnings remain.
        """
        con = _menu_console()
        if bool(getattr(self, "_suppress_next_brand", False)):
            self._suppress_next_brand = False
        else:
            con.print(
                app_header(
                    "menu",
                    context=os.path.basename(self.workspace_path),
                    timestamp=_frame_timestamp(),
                    width=con.width,
                )
            )

        ws_name = os.path.basename(self.workspace_path)
        stats = CTFDashboard(self.workspace_path).get_summary_stats()
        total = stats.get("total_challenges", 0)

        ctx = Text()
        if total > 0:
            title = stats.get("title", ws_name)
            plat = str(stats.get("platform", "generic")).upper()
            solved = stats.get("solved_challenges", 0)
            rate = stats.get("completion_rate", 0)
            pts = stats.get("earned_points", 0)
            tot_pts = stats.get("total_points", 0)

            ctx.append("  ")
            ctx.append(str(title), style=f"bold {FG_BASE}")
            ctx.append(f" · {plat}", style=FG_MUTED)
            if str(title) != ws_name:
                ctx.append(f" · {ws_name}", style=FG_MUTED)

            ctx.append("\n  ")
            ctx.append_text(meter(rate, 18, SOLVE_RAMP))
            ctx.append(f"  {solved}/{total} · {rate:.1f}%", style=FG_MUTED)
            ctx.append(f" · {pts}/{tot_pts} pts", style=FG_MUTED)
        else:
            ctx.append("  ")
            ctx.append(ws_name or self.workspace_path, style=INFO)
            ctx.append(" · no challenges", style=FG_MUTED)

        if not (self.cookie or self.token):
            ctx.append("\n  ")
            ctx.append("! auth not configured · use [9]", style=WARN)
        con.print(ctx)

    def run(self):
        while True:
            # C12-M1: EOF/Ctrl-D (và Ctrl-C) ở prompt BẤT KỲ — kể cả prompt
            # con trong submenu — phải thoát sạch lịch sự, không nổ
            # EOFError/KeyboardInterrupt traceback ra ngoài run().
            try:
                self._load_saved_auth()
                self._print_header()

                _section('Actions')
                for key, label in _main_menu_actions(_menu_console().width):
                    # §S1.1: option là hành động gần nhất → dòng ❯ reverse;
                    # option thường giữ _option() nguyên trạng.
                    if key == self._last_action:
                        _menu_console().print(
                            selected_row(f'[{key}] {label}', selected=True))
                    else:
                        _option(key, label)

                prompt_msg = 'Select action (0-9, S): '
                if self._last_action:
                    prompt_msg = f'Select action (0-9, S) [default {self._last_action}]: '
                raw_choice = _prompt(prompt_msg).strip()
                choice = raw_choice.strip(" []().")
                if not choice and self._last_action:
                    choice = self._last_action

                # Map alias/shortcuts to canonical action keys
                choice_lower = choice.lower()
                key_map = {
                    '0': '0', 'q': '0', 'quit': '0', 'exit': '0', 'thoat': '0',
                    '1': '1', 'clone': '1', 'pull': '1', 'download': '1',
                    '2': '2', 'switch': '2', 'workspace': '2', 'ws': '2',
                    '3': '3', 'tree': '3', 'ls': '3', 'status': '3',
                    '4': '4', 'view': '4', 'detail': '4', 'cat': '4', 'info': '4',
                    '5': '5', 'container': '5', 'instance': '5', 'docker': '5',
                    '6': '6', 'submit': '6', 'flag': '6', 'nop': '6',
                    '7': '7', 'auto': '7', 'auto-submit': '7',
                    '8': '8', 'scan': '8', 'summary': '8',
                    '9': '9', 'auth': '9', 'cookie': '9', 'token': '9', 'config': '9',
                    's': 'S', 'solve': 'S', 'solver': 'S', 'bqa': 'S', 'eating': 'S', 'eat': 'S',
                }
                canonical = key_map.get(choice_lower, choice.upper() if choice.upper() == 'S' else choice)

                if canonical == '0':
                    _menu_console().print(
                        Text('\nGoodbye! Good luck with your CTF competition.\n',
                             style=FG_MUTED))
                    break
                elif canonical == '1':
                    self._menu_download_new()
                elif canonical == '2':
                    self._menu_switch_workspace()
                elif canonical == '3':
                    self._menu_view_tree()
                elif canonical == '4':
                    self._menu_view_challenge_detail()
                elif canonical == '5':
                    self._menu_container_manager()
                elif canonical == '6':
                    self._menu_submit_flag()
                elif canonical == '7':
                    self._menu_auto_submit()
                elif canonical == '8':
                    self._menu_scan_workspaces()
                elif canonical == '9':
                    self._menu_configure_auth()
                elif canonical == 'S':
                    self._menu_solver()
                else:
                    Logger.warning('Invalid selection. Please choose an option from 0 to 9 (or S).')
                # Ghi nhớ hành động gần nhất để vòng sau đánh dấu ❯ (§S1.1);
                # input lạ ('x', '99') không được tính là action.
                if canonical in ('1', '2', '3', '4', '5', '6', '7', '8', '9', 'S'):
                    self._last_action = canonical
            except (EOFError, KeyboardInterrupt):
                _menu_console().print(
                    Text('\nGoodbye! Good luck with your CTF competition.\n',
                         style=FG_MUTED))
                break

    def _menu_download_new(self):
        _section('Download & Initialize New CTF Competition')
        url = _prompt('CTF platform URL (e.g. https://ctf.example.com): ').strip()
        if not url:
            Logger.warning('URL cannot be empty.')
            return

        con = _menu_console()
        con.print()
        con.print('  Authentication method:', style=FG_MUTED)
        _option('1', 'Session Cookie (F12 -> Cookies -> copy session=xxx or GZCTF_Token=xxx)')
        _option('2', 'API Token / Bearer Token')
        _option('3', 'No authentication required (public platform)')
        ach = _prompt('Choice (1-3) [default 1]: ') or '1'

        cookie = None
        token = None
        if ach == '1':
            cookie = sanitize_cookie_input(_prompt('Paste Cookie: ').strip())
        elif ach == '2':
            token = _prompt('Paste Token: ').strip()

        root_hint = resolve_workspace_root()
        out = _prompt(f'Save directory (Enter to save to {root_hint}/<Competition_Name>): ').strip()
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
                Logger.success('Competition download completed!')
                if dl.output_dir and os.path.exists(dl.output_dir):
                    self.workspace_path = dl.output_dir
                    self.cookie = cookie
                    self.token = token
                    self._save_current_workspace()
                    AuthService.save_auth(
                        workspace=dl.output_dir,
                        url=url,
                        cookie=cookie,
                        token=token,
                    )
        except Exception as e:
            Logger.error(f'Download failed: {e}')
        _pause()

    def _menu_switch_workspace(self):
        base_ctf = resolve_workspace_root()
        workspaces = []
        if os.path.exists(base_ctf):
            for d in sorted(os.listdir(base_ctf)):
                p = os.path.join(base_ctf, d)
                if os.path.isdir(p):
                    dash = CTFDashboard(p)
                    st = dash.get_summary_stats()
                    if st.get('total_challenges', 0) > 0:
                        workspaces.append((p, st))

        _section('Select Active CTF Workspace')
        if not workspaces:
            Logger.warning(f'No workspaces found in {base_ctf}.')
            custom_p = _prompt('Enter competition directory path: ').strip()
            if os.path.exists(custom_p):
                self.workspace_path = os.path.abspath(custom_p)
                self._save_current_workspace()
            return

        con = _menu_console()
        active = os.path.abspath(self.workspace_path)
        for row in _workspace_rows(workspaces, active):
            con.print(row)

        _option('C', 'Enter custom directory path')
        _option('0', 'Back')
        ch = _prompt(f'Select Workspace (1-{len(workspaces)}): ').strip()
        if ch == '0':
            return
        elif ch.upper() == 'C':
            custom_p = _prompt('Enter path: ').strip()
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
                Logger.success(f"Switched to workspace: {os.path.basename(self.workspace_path)}")
            except Exception:
                Logger.error('Invalid selection.')
                _pause()

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
            Logger.warning(f'Failed to save config: {e}')
            return
        if saved_state is not None:
            self.config = saved_state
        else:
            # Review 536364d (LOW): _mut luôn trả state nên None chỉ có thể
            # là thư mục global config biến mất giữa chừng (không raise
            # OSError). Log cùng mức nhánh OSError thay vì im lặng; cache
            # nội bộ giữ nguyên — không refresh từ None.
            Logger.warning('Failed to save config: global config directory '
                           'disappeared.')

    def _menu_view_tree(self):
        dash = CTFDashboard(self.workspace_path)
        _section('Challenge Tree Display Options')
        _option('1', 'Show ALL challenges')
        _option('2', 'Show UNSOLVED challenges only')
        _option('3', 'Show SOLVED challenges only')
        _option('4', 'Show challenges with DYNAMIC CONTAINER only')
        _option('5', 'Filter by category (Web, Crypto, Pwn, Rev, Forensics, Misc)')
        fch = _prompt('Choice (1-5) [default 1]: ') or '1'

        if fch == '2':
            dash.render_tree(only_unsolved=True)
        elif fch == '3':
            dash.render_tree(only_solved=True)
        elif fch == '4':
            dash.render_tree(only_container=True)
        elif fch == '5':
            cat_in = _prompt('Enter categories (e.g. Web Crypto): ').strip().split()
            dash.render_tree(filter_cat=cat_in)
        else:
            dash.render_tree()
        _pause()

    def _menu_view_challenge_detail(self):
        dash = CTFDashboard(self.workspace_path)
        challs = dash.local_challenges
        _section('Challenge Lookup & Details')
        if not challs:
            Logger.warning('No challenges found in current workspace.')
            _pause()
            return

        con = _menu_console()
        for idx, c in enumerate(challs, 1):
            name = fit_cells(str(c.get('name') or 'Unknown'), 28, pad=True)
            cat = fit_cells(str(c.get('category') or 'Other'), 12, pad=True)
            pts = f"{c.get('points', '-'):>4} pts"
            cid = str(c.get('id', ''))
            is_solved = bool(c.get('solved_by_me'))

            row = Text('  ')
            row.append(f'[{idx:>2}]', style=ACCENT)
            row.append(f' {name} ', style=FG_BASE)
            row.append(f'{cat} ', style=FG_MUTED)
            row.append(f'{pts} ', style=FG_MUTED)
            if is_solved:
                row.append('✔ SOLVED', style='solved')
            else:
                row.append('· Unsolved', style=FG_FAINT)
            if cid:
                row.append(f' (ID: {cid})', style=FG_FAINT)
            con.print(row)

        con.print()
        q = _prompt(f'Select challenge (1-{len(challs)}), or enter ID/Name [0 to return]: ').strip()
        target, err = _resolve_challenge_selection(challs, q)
        if not target:
            if err:
                Logger.error(err)
                _pause()
            return

        ws_root = os.path.abspath(self.workspace_path)
        raw_folder = str(target.get('_folder') or '')
        if raw_folder:
            abs_folder = os.path.abspath(raw_folder if os.path.isabs(raw_folder) else os.path.join(ws_root, raw_folder))
            folder = abs_folder if os.path.isdir(abs_folder) else ''
        else:
            folder = ''

        con.print()
        # §S1.3: candidate khớp đầu tiên đánh dấu ❯ + reverse highlight.
        head = selected_row(str(target.get('name') or 'Unknown'), selected=True)
        head.append(f"  ID: {target.get('id')}  ", style=FG_FAINT)
        if target.get('solved_by_me'):
            head.append('✔ SOLVED', style='solved')
        else:
            head.append('· UNSOLVED', style=FG_FAINT)
        con.print(head)

        meta = Text('  Category: ')
        meta.append(str(target.get('category') or 'Other'), style=FG_BASE)
        meta.append('  ·  ', style=FG_FAINT)
        meta.append(f"{target.get('points', '-')} pts", style=FG_MUTED)
        meta.append('  ·  ', style=FG_FAINT)
        meta.append(f"{target.get('solves_count', '-')} solves", style=FG_MUTED)
        con.print(meta)

        rel_loc = target.get('_rel_folder') or folder or '(not downloaded)'
        loc = Text('  Local directory: ')
        loc.append(str(rel_loc), style=INFO)
        con.print(loc)
        if target.get('connection_info'):
            ci = Text('  Connection: ')
            ci.append(str(target.get('connection_info')), style=INFO)
            con.print(ci)

        if folder and os.path.isdir(folder):
            att_dir = os.path.join(folder, 'challenge')
            if os.path.isdir(att_dir):
                try:
                    files = [f for f in sorted(os.listdir(att_dir)) if not f.startswith('.') and f not in ('README.md', 'NOTE.md', 'metadata.json')]
                    if files:
                        con.print(Text(f"  Attachments ({len(files)} files): {', '.join(files)}", style=INFO))
                except OSError as e:
                    Logger.warning(f"Cannot read challenge/ directory: {e}")

            readme_candidates = [
                os.path.join(folder, 'challenge', 'README.md'),
                os.path.join(folder, 'README.md'),
            ]
            for rp in readme_candidates:
                if os.path.isfile(rp):
                    try:
                        with open(rp, 'r', encoding='utf-8', errors='replace') as rf:
                            con.print(Text('\n  Challenge Description (README.md):', style=f'bold {FG_FAINT}'))
                            con.print(rf.read()[:2000])
                        break
                    except (OSError, UnicodeError) as e:
                        Logger.warning(f"Cannot read README.md ({rp}): {e}")

            note_candidates = [
                os.path.join(folder, 'challenge', 'NOTE.md'),
                os.path.join(folder, 'NOTE.md'),
            ]
            for np in note_candidates:
                if os.path.isfile(np):
                    try:
                        with open(np, 'r', encoding='utf-8', errors='replace') as nf:
                            con.print(Text('\n  Notes / Triage (NOTE.md):', style=f'bold {FG_FAINT}'))
                            con.print(nf.read()[:1000])
                        break
                    except (OSError, UnicodeError) as e:
                        Logger.warning(f"Cannot read NOTE.md ({np}): {e}")
        else:
            con.print(Text('  (Challenge does not have a valid local directory)', style=FG_MUTED))

        while True:
            con.print()
            con.print('  Actions for this challenge:', style=FG_MUTED)
            _option('1', 'Submit flag for this challenge')
            _option('2', 'Manage Container / Instance (if available)')
            _option('3', 'BQA EATING for this challenge')
            _option('0', 'Back')
            act = _prompt('Choice (0-3) [Enter to return]: ').strip()
            if act == '1':
                self._submit_flag_for_target(target)
            elif act == '2':
                cid = str(target.get('id', ''))
                self._run_container_action_for_id(cid, challenge_name=str(target.get('name') or ''))
            elif act == '3':
                self._run_solver_for_target(target)
            else:
                break

    def _menu_container_manager(self):
        try:
            mgr = InstanceManager(self.workspace_path, cookie=self.cookie, token=self.token)
        except Exception as e:
            Logger.error(f'Failed to initialize Container Manager: {e}')
            _pause()
            return

        containers = mgr.list_containers()
        _section('Manage Dynamic Containers / Instances')
        if not containers:
            Logger.info('No challenges with auto-detected dynamic containers found in workspace metadata.')
            cid_in = _prompt('Enter Challenge ID or Name to manage container directly [0 to cancel]: ').strip()
            if not cid_in or cid_in in ('0', 'q', 'cancel', 'back'):
                return
            target = mgr.find_challenge(challenge_id=cid_in) or mgr.find_challenge(challenge_name=cid_in)
            cid = target.get('id') if target else cid_in
            cname = str(target.get('name') or f'ID {cid_in}') if target else f'ID {cid_in}'
            self._run_container_action_for_id(str(cid), challenge_name=cname)
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
            row.append(f'  ·  {solves} solves', style=FG_MUTED)
            con.print(row)

        ch = _prompt(f'Select challenge for container actions (1-{len(containers)}), or enter ID/Name [0 to cancel]: ').strip()
        if not ch or ch in ('0', 'q', 'cancel', 'back'):
            return
        target, err = _resolve_challenge_selection(containers, ch)
        if not target:
            target = mgr.find_challenge(challenge_id=ch) or mgr.find_challenge(challenge_name=ch)
            if not target:
                if err:
                    Logger.error(err)
                else:
                    Logger.error(f'Challenge not found: {ch}')
                _pause()
                return

        cid = target.get('id')
        cname = str(target.get('name') or f'ID {cid}')
        self._run_container_action_for_id(str(cid), challenge_name=cname)

    def _menu_submit_flag(self):
        dash = CTFDashboard(self.workspace_path)
        challs = dash.local_challenges
        _section('Submit Flag for Challenge')
        if not challs:
            Logger.warning('No challenges found in current workspace.')
            _pause()
            return

        con = _menu_console()
        for idx, c in enumerate(challs, 1):
            name = fit_cells(str(c.get('name') or 'Unknown'), 28, pad=True)
            cat = fit_cells(str(c.get('category') or 'Other'), 12, pad=True)
            pts = f"{c.get('points', '-'):>4} pts"
            cid = str(c.get('id', ''))
            is_solved = bool(c.get('solved_by_me'))
            row = Text('  ')
            row.append(f'[{idx:>2}]', style=ACCENT)
            row.append(f' {name} ', style=FG_BASE)
            row.append(f'{cat} ', style=FG_MUTED)
            row.append(f'{pts} ', style=FG_MUTED)
            row.append('✔ SOLVED' if is_solved else '· Unsolved', style='solved' if is_solved else FG_FAINT)
            if cid:
                row.append(f' (ID: {cid})', style=FG_FAINT)
            con.print(row)

        con.print()
        q = _prompt(f'Select challenge to submit flag (1-{len(challs)}), or enter ID/Name [0 to cancel]: ').strip()
        target, err = _resolve_challenge_selection(challs, q)
        if not target:
            if err:
                Logger.error(err)
                _pause()
            return

        self._submit_flag_for_target(target)

    def _submit_flag_for_target(self, target: dict):
        con = _menu_console()
        con.print()
        sel = Text('  Selected challenge: ')
        sel.append(str(target.get('name')), style=f'bold {FG_BASE}')
        sel.append(f" (ID: {target.get('id')}, Category: {target.get('category')})", style=FG_MUTED)
        con.print(sel)

        flag_str = _prompt('Enter flag string [Enter to cancel]: ').strip()
        if not flag_str:
            Logger.info('Flag submission cancelled.')
            return

        sub = FlagSubmitter(
            workspace_dir=self.workspace_path,
            cookie=self.cookie,
            token=self.token
        )
        sub.submit_single_flag(
            challenge_id=target.get('id'),
            challenge_name=target.get('name'),
            flag_value=flag_str
        )
        _pause()

    def _run_container_action_for_id(self, cid: str, challenge_name: str = ""):
        try:
            mgr = InstanceManager(self.workspace_path, cookie=self.cookie, token=self.token)
        except Exception as e:
            Logger.error(f'Failed to initialize Container Manager: {e}')
            _pause()
            return

        con = _menu_console()
        con.print()
        title = f'  Container actions for {challenge_name} (ID: {cid}):' if challenge_name else f'  Container actions for Challenge ID {cid}:'
        con.print(title, style=FG_MUTED)
        _option('1', 'Start / Spawn container (get IP:Port & netcat command)')
        _option('2', 'Check status & remaining lifetime')
        _option('3', 'Extend lifetime (renew countdown)')
        _option('4', 'Stop / Destroy container')
        _option('0', 'Back')
        act = _prompt('Choice (1-4) [0 to return]: ').strip()
        if act == '1':
            mgr.start_instance(cid)
        elif act == '2':
            st = mgr.get_status(cid)
            Logger.info(f'Status for ID {cid}: {st}')
        elif act == '3':
            mgr.extend_instance(cid)
        elif act == '4':
            mgr.stop_instance(cid)
        else:
            return

        diag = getattr(mgr, 'last_diagnostic', None)
        if diag and getattr(diag, 'recovery', None):
            from .bqa_recovery import offer_bqa_recovery
            if not offer_bqa_recovery(diag):
                _pause()
        else:
            _pause()

    def _run_solver_for_target(self, target: dict):
        from .services.solver_service import SolverService
        from .cli_commands import _solver_table

        try:
            service = SolverService(self.workspace_path)
            jobs = service.scan()
            target_folder = target.get('_folder') or target.get('_local_path')
            matched_job = None
            if target_folder:
                abs_tf = os.path.abspath(target_folder if os.path.isabs(target_folder) else os.path.join(self.workspace_path, target_folder))
                matched_job = next((j for j in jobs if os.path.abspath(str(j.path)) == abs_tf), None)
            if not matched_job:
                matched_job = next(
                    (j for j in jobs if str(j.challenge_id) == str(target.get('id')) or str(j.name).lower() == str(target.get('name', '')).lower()),
                    None
                )
            if not matched_job:
                Logger.warning('This challenge does not have a valid challenge/ directory or metadata to run SuperBQA.')
                _pause()
                return

            con = _menu_console()
            res = service.spawn_background(str(matched_job.display_id), workers=1)
            if not res.get("success"):
                Logger.error(res.get("message", "SuperBQA startup failed."))
                _pause()
                return

            con.print()
            con.print(f"  [bold green]✔ {res.get('message')}[/bold green]")
            con.print("  [dim]Press Ctrl+C to detach.[/dim]\n")

            try:
                with Live(_solver_table(service, show_worker_count=True), console=con, refresh_per_second=4) as live:
                    while True:
                        time.sleep(0.25)
                        live.update(_solver_table(service, show_worker_count=True))
            except KeyboardInterrupt:
                con.print("\n  [dim]Detached.[/dim]")
        except Exception as e:
            Logger.error(f'SuperBQA error: {e}')
        _pause()

    def _menu_solver(self):
        from .services.solver_service import SolverService
        from .cli_commands import _solver_table

        while True:
            _section('SuperBQA')
            try:
                service = SolverService(self.workspace_path)
                jobs = service.scan()
            except Exception as e:
                Logger.error(f'Failed to initialize Solver Service: {e}')
                _pause()
                return

            if not jobs:
                Logger.warning('No challenges with valid source code or metadata found in this workspace.')
                _pause()
                return

            con = _menu_console()
            con.print(_solver_table(service, animate=False))

            daemon_info = service.get_daemon_status()
            if daemon_info.get("is_running"):
                d_pid = daemon_info.get("daemon_pid")
                d_targets = daemon_info.get("target_ids", "-")
                d_active = ", ".join(daemon_info.get("active_ids", [])) or "preparing"
                con.print()
                st_text = Text("  🟢 SUPERBQA EATING ACTIVE ", style="bold green")
                st_text.append(f"[PID: {d_pid}]  ·  Targets: {d_targets}  ·  Active: {d_active}", style=FG_MUTED)
                con.print(st_text)

            cat_sessions = service.get_category_sessions()
            if cat_sessions:
                sess_strs = [f"{cat} ({info.get('conversation_id', '')[:8]}...)" for cat, info in cat_sessions.items()]
                con.print(f"  [dim]📁 Persistent Sessions: {', '.join(sess_strs)}[/dim]")

            con.print()
            _option('1', 'BQA EATING')
            _option('2', 'SUPERBQA EATING')
            _option('3', 'Live Radar')
            _option('4', 'Worker log tail')
            _option('5', 'Stop / Cancel')
            _option('6', 'Distill category playbook')
            _option('7', 'Help')
            _option('0', 'Back to main menu')

            try:
                act = _prompt('Choice (0-7) [default 1]: ').strip() or '1'
            except (EOFError, KeyboardInterrupt):
                return
            act_clean = act.lower()

            if act_clean in ('0', 'q', 'back', 'exit'):
                return
            elif act_clean in ('1', 'bqa', 'eat', 'eating'):
                try:
                    ids = _prompt('Enter display ID for BQA EATING (e.g. 1 or 1,3,5): ').strip()
                except (EOFError, KeyboardInterrupt):
                    return
                if not ids:
                    continue
                res = service.spawn_background(ids, workers=3)
                if not res.get("success"):
                    Logger.error(res.get("message", "BQA EATING startup failed."))
                    _pause()
                    continue
                con.print()
                con.print(f"  [bold green]✔ {res.get('message')}[/bold green]")
                continue
            elif act_clean in ('2', 'superbqa', 'super', 'all', 'feast'):
                solvable = [j for j in jobs if j.has_source or j.has_instance]
                if not solvable:
                    Logger.warning('No challenges found with attachments or dynamic containers.')
                    _pause()
                    continue
                unsolved = [j for j in solvable if not j.is_solved]
                target_jobs = unsolved if unsolved else solvable
                source_ids = ",".join(str(j.display_id) for j in target_jobs)
                con.print()
                con.print(f"  [bold yellow]Preparing SUPERBQA EATING on {len(target_jobs)} challenges ({source_ids}).[/bold yellow]")
                try:
                    confirm = Confirm.ask('  Confirm execution?', default=True)
                except (EOFError, KeyboardInterrupt):
                    return
                if not confirm:
                    continue
                category_count = len({job.category.strip().casefold() for job in target_jobs})
                res = service.spawn_background(
                    source_ids,
                    workers=max(1, category_count),
                    per_category=True,
                )
                if not res.get("success"):
                    Logger.error(res.get("message", "SUPERBQA EATING startup failed."))
                    _pause()
                    continue
                con.print(f"\n  [bold green]✔ {res.get('message')}[/bold green]")
                try:
                    watch_now = _prompt('  Attach Live Radar now? [Y/n]: ').strip().lower()
                except (EOFError, KeyboardInterrupt):
                    return
                if watch_now != 'n':
                    con.print("\n  [dim]Press Ctrl+C to detach.[/dim]\n")
                    try:
                        with Live(_solver_table(service, show_worker_count=True), console=con, refresh_per_second=4) as live:
                            while True:
                                time.sleep(0.25)
                                live.update(_solver_table(service, show_worker_count=True))
                    except KeyboardInterrupt:
                        con.print("\n  [dim]Detached.[/dim]")
                _pause()
                continue
            elif act_clean in ('3', 'radar', 'live', 'watch', 'active', 'agy', 'workers', 'tasks', 'running'):
                _run_live_radar(service, con)
                _pause()
                continue
            elif act == '4':
                try:
                    tid = _prompt('Enter display ID to view log [Enter for first]: ').strip()
                except (EOFError, KeyboardInterrupt):
                    return
                matched_job = None
                if tid:
                    try:
                        matched_job = service.select_ids(tid)[0]
                    except Exception as e:
                        Logger.error(f'Selection error: {e}')
                        _pause()
                        continue
                else:
                    matched_job = jobs[0] if jobs else None

                if not matched_job or not matched_job.log_path.is_file():
                    Logger.warning(f'No log file found for {matched_job.name if matched_job else tid}.')
                    _pause()
                    continue
                con.print(f"\n  [bold cyan]Latest log for {matched_job.name} ({matched_job.log_path}):[/bold cyan]\n")
                try:
                    with matched_job.log_path.open('r', encoding='utf-8', errors='replace') as lf:
                        lines = lf.readlines()
                        con.print(''.join(lines[-40:]), markup=False)
                except OSError as e:
                    Logger.error(f'Unable to read log file: {e}')
                _pause()
                continue
            elif act == '5':
                try:
                    tid = _prompt('Enter display ID to stop [Enter to stop all workers]: ').strip()
                    confirm = Confirm.ask('  Confirm stopping workers?', default=True)
                except (EOFError, KeyboardInterrupt):
                    return
                if confirm:
                    res = service.stop_background(tid if tid else None)
                    Logger.info(res.get("message", "Stop signal sent."))
                _pause()
                continue
            elif act == '6':
                cat_sessions = service.get_category_sessions()
                known_cats = sorted(cat_sessions.keys()) if cat_sessions else sorted({j.category for j in jobs if j.category})
                if not known_cats:
                    Logger.warning('No categories available in this workspace.')
                    _pause()
                    continue
                con.print("\n  [bold cyan]Available categories to distill:[/bold cyan]")
                for idx, cname in enumerate(known_cats, 1):
                    con.print(f"    [{idx}] {cname}")
                try:
                    c_input = _prompt('Select category number or name [Enter for all]: ').strip()
                except (EOFError, KeyboardInterrupt):
                    return
                target_cat = None
                if c_input.isdigit() and 1 <= int(c_input) <= len(known_cats):
                    target_cat = known_cats[int(c_input) - 1]
                elif c_input:
                    target_cat = c_input
                targets = [target_cat] if target_cat else known_cats

                for c in targets:
                    con.print(f"\n  [dim]Distilling operational playbook for {c}...[/dim]")
                    res = service.distill_playbook(c)
                    if res.get("success"):
                        con.print(f"  [bold green]✔ Playbook updated for {c}:[/bold green]")
                        con.print(f"    📄 File: [cyan]{res.get('playbook_path')}[/cyan]")
                        if res.get("main_conversation_id"):
                            con.print(f"    🧠 Master Session: [dim]{res.get('main_conversation_id')}[/dim]")
                    else:
                        Logger.error(f"Failed to distill playbook for {c}")
                _pause()
                continue
            elif act_clean in ('7', 'help', 'h', 'ask', 'astra', 'ctf-ask'):
                from .cli_commands import handle_ask
                import argparse
                con.print("\n  [bold cyan]Help: Escalate formal math/logic roadblock to Codex Astra (ctf-ask)[/bold cyan]")
                try:
                    ws_input = _prompt('  Enter formal workspace path [Enter for ./math_workspace]: ').strip() or 'math_workspace'
                    ws_path = Path(self.workspace_path) / ws_input if not Path(ws_input).is_absolute() else Path(ws_input)
                    if not ws_path.is_dir():
                        Logger.error(f'Workspace not found: {ws_path}')
                        _pause()
                        continue
                    mode_choice = _prompt('  Mode: [1] Full Escalation to Astra  [2] Preflight check only  [3] Dry-run [default 1]: ').strip() or '1'
                except (EOFError, KeyboardInterrupt):
                    return
                args = argparse.Namespace(
                    workspace=str(ws_path),
                    output=None,
                    model=None,
                    effort=None,
                    preflight_only=(mode_choice == '2'),
                    verify_only=None,
                    dry_run=(mode_choice == '3'),
                )
                try:
                    handle_ask(args)
                except SystemExit:
                    pass
                except Exception as e:
                    Logger.error(f"Help (ctf-ask) execution error: {e}")
                _pause()
                continue
            else:
                Logger.warning('Invalid selection. Please choose an option from 0 to 7.')
                _pause()
                continue

    def _menu_auto_submit(self):
        _section('Auto-Scan & Submit Solved Flags in Workspace')
        confirm = Confirm.ask('Scan all README.md files and auto-submit filled flags?', default=True)
        if confirm:
            sub = FlagSubmitter(
                workspace_dir=self.workspace_path,
                cookie=self.cookie,
                token=self.token
            )
            sub.auto_submit_all()
        _pause()

    def _menu_scan_workspaces(self):
        base_dir = resolve_workspace_root()
        # Single scan table located in StatusService.scan_all_workspaces
        # (shared with cli handle_workspaces / manage.py -A)
        StatusService.scan_all_workspaces(base_dir)
        _pause()

    def _menu_configure_auth(self):
        _section(f'Configure Authentication for: {os.path.basename(self.workspace_path)}')
        con = _menu_console()
        ck_show = f"Saved ({self.cookie[:8]}...)" if self.cookie else '(None)'
        tk_show = f"Saved ({self.token[:8]}...)" if self.token else '(None)'
        cur = Text('  Current Cookie: ')
        cur.append(ck_show, style=INFO if self.cookie else FG_FAINT)
        con.print(cur)
        cur2 = Text('  Current Token : ')
        cur2.append(tk_show, style=INFO if self.token else FG_FAINT)
        con.print(cur2)

        con.print()
        _option('1', 'Enter / Paste new Session Cookie (session=... or GZCTF_Token=...)')
        _option('2', 'Enter API Token / Bearer Token')
        _option('3', 'Clear saved credentials')
        _option('0', 'Back')
        ch = _prompt('Choice (0-3): ').strip()

        if ch == '1':
            c_in = _prompt('Paste Cookie [Enter to cancel]: ').strip()
            if not c_in:
                return
            if os.path.isfile(c_in):
                with open(c_in, 'r', encoding='utf-8') as f:
                    self.cookie = f.read().strip()
            else:
                self.cookie = c_in
            self._save_current_workspace()
            Logger.success('Cookie saved successfully for this workspace!')
            _pause()
        elif ch == '2':
            t_in = _prompt('Paste API/Bearer Token [Enter to cancel]: ').strip()
            if not t_in:
                return
            self.token = t_in
            self._save_current_workspace()
            Logger.success('Token saved successfully for this workspace!')
            _pause()
        elif ch == '3':
            self.cookie = None
            self.token = None
            self._save_current_workspace()
            Logger.info('Credentials cleared.')
            _pause()


def _pause():
    _prompt('Press Enter to continue...')


def launch_interactive_menu(workspace_path: Optional[str] = None, cookie: Optional[str] = None, token: Optional[str] = None):
    # Full brand owns the first frame. The first menu redraw therefore skips
    # AppHeader to avoid showing UCS_ExOdia twice back-to-back.
    con = _menu_console()
    con.print(splash(con.width))
    app = CTFInteractiveConsole(
        workspace_path=workspace_path,
        cookie=cookie,
        token=token,
    )
    app._suppress_next_brand = True
    app.run()
