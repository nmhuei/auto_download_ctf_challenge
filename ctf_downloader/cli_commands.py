"""Lớp command mỏng cho unified CLI: parse (ở cli.py) -> service -> render -> exit code.

Quy tắc kiến trúc Phase 7: file này KHÔNG chứa ``input()`` / ``Prompt.ask`` /
``Confirm.ask`` nào — mọi wizard interactive nằm ở tầng services
(``InstanceService.interactive_pick`` / ``SubmitService.interactive_submit``)
hoặc ở interactive_menu.
"""
import os
import subprocess
import sys
import textwrap
from typing import Optional

from rich.console import Console
from rich.text import Text

from .config import DownloaderConfig
from .interactive_menu import launch_interactive_menu
from .services.auth_service import AuthService
from .services.instance_service import InstanceService
from .services.pull_service import PullService
from .services.rank_service import RankService
from .services.status_service import StatusService
from .services.submit_service import SubmitService
from .storage.workspace_repo import WorkspaceRepo
from .ui.theme import ERROR as _ERROR_COLOR
from .ui.theme import FG_FAINT as _FAINT_COLOR
from .ui.theme import FG_MUTED as _MUTED_COLOR
from .ui.theme import INFO as _INFO_COLOR
from .ui.theme import SOLVED as _SOLVED_COLOR
from .utils.logger import Logger, console


def get_auth_for_workspace(ws_path: str, cookie_arg: Optional[str] = None,
                           token_arg: Optional[str] = None):
    """Re-export mỏng quanh AuthService.resolve (giữ tên cũ cho script legacy)."""
    return AuthService.resolve(ws_path, cookie_arg=cookie_arg, token_arg=token_arg)


def handle_pull(args):
    if args.interactive or not args.url:
        if not sys.stdin.isatty():
            # Live-verify v4: `ctf pull </dev/null` từng nổ EOFError traceback
            # khi rẽ vào interactive menu — non-tty thì từ chối sạch kèm hint.
            Logger.error("Thiếu --url và stdin không phải terminal tương tác.")
            Logger.info("Dùng `ctf pull <url>` hoặc chạy trong terminal thật "
                        "để mở menu.")
            sys.exit(2)
        launch_interactive_menu()
        return

    cookie_val = args.cookie
    if cookie_val and os.path.isfile(cookie_val):
        with open(cookie_val, 'r', encoding='utf-8') as f:
            cookie_val = f.read().strip()

    config = DownloaderConfig(
        url=args.url,
        cookie=cookie_val,
        token=args.token,
        output_dir=args.output,
        threads=args.threads,
        download_third_party=not args.no_third_party,
        create_solve_template=not args.no_template,
        force_redownload=args.force,
        timeout=args.timeout,
        categories=args.category,
        exclude_categories=args.exclude,
        incremental_update=getattr(args, 'update', False) or getattr(args, 'refresh_meta', False),
        refresh_meta=getattr(args, 'refresh_meta', False)
    )

    try:
        if config.refresh_meta or config.incremental_update:
            result = PullService.run_update(config, refresh_meta=config.refresh_meta)
        else:
            result = PullService.run(config)
        if not result.get('ok'):
            sys.exit(1)
    except KeyboardInterrupt:
        console.print("[bold red][!] Download đã bị huỷ bởi người dùng.[/bold red]")
        sys.exit(130)
    except Exception as e:
        Logger.error(f'Lỗi nghiêm trọng khi pull: {e}')
        sys.exit(1)


def handle_status(args):
    repo = WorkspaceRepo(args.workspace)
    StatusService.render_tree(
        repo,
        filter_cat=args.category,
        only_unsolved=args.unsolved,
        only_solved=args.solved,
        only_container=args.container,
        filter_labels=getattr(args, 'labels', None),
        search=getattr(args, 'search', None)
    )


def handle_note(args):
    """P1-6: ``ctf note <challenge> [content] [--remove]`` — prompt multi-line
    nằm ở StatusService (tầng services, không input() ở lớp command)."""
    content = ' '.join(getattr(args, 'content', None) or []).strip() or None
    repo = WorkspaceRepo(args.workspace)
    ok = StatusService.set_note(repo, args.target, text=content,
                                remove=bool(getattr(args, 'remove', False)))
    if not ok:
        sys.exit(1)


def handle_tag(args):
    """P1-6: ``ctf tag <challenge> <tag...> [-r]`` — validate [a-z0-9-] ≤24."""
    repo = WorkspaceRepo(args.workspace)
    ok, _rejected = StatusService.update_tags(
        repo, args.target, list(getattr(args, 'tags', None) or []),
        remove=bool(getattr(args, 'remove', False)))
    if not ok:
        sys.exit(1)


def handle_open(args):
    """``ctf open <challenge> [-w WS]`` — mở thư mục challenge trong file
    manager/terminal (xdg-open trên Linux).

    Resolve theo cùng tier với ``WorkspaceRepo.find_challenge`` qua
    ``StatusService.resolve_challenge`` (exact id -> exact name -> substring;
    ambiguous -> liệt kê candidate, không partial-match âm thầm).
    ``xdg-open`` chạy không shell=True, check=True; thiếu binary ->
    hint cài xdg-utils."""
    from pathlib import Path

    from .services.status_service import (
        AmbiguousChallengeError,
        ChallengeNotFoundError,
    )

    repo = WorkspaceRepo(args.workspace)
    try:
        meta_path, _meta = StatusService.resolve_challenge(repo, args.target)
    except ChallengeNotFoundError as e:
        Logger.error(str(e))
        sys.exit(1)
    except AmbiguousChallengeError as e:
        Logger.error(str(e))
        StatusService._print_matches(e.matches)
        sys.exit(1)

    chall_dir = str(Path(meta_path).parent)
    Logger.info(f"Đang mở thư mục challenge: {chall_dir}")
    try:
        subprocess.run(["xdg-open", chall_dir], check=True, shell=False)
    except FileNotFoundError:
        Logger.error("Không tìm thấy lệnh 'xdg-open' — hãy cài gói xdg-utils "
                     "(vd: sudo apt install xdg-utils).")
        sys.exit(1)
    except subprocess.CalledProcessError as e:
        Logger.error(f"xdg-open thất bại (exit {e.returncode}): {chall_dir}")
        sys.exit(1)


def handle_workspaces(args):
    """``ctf workspaces`` — bảng mọi workspace (PHOSPHOR, không viền dọc).

    StatusService.scan_all_workspaces vẫn là nguồn dữ liệu DUY NHẤT (quy tắc
    delegation Phase 7); log legacy ``[*] Scanning...`` + bảng cũ của service
    được nuốt (redirect stdout) rồi handler tự vẽ: heading UPPERCASE faint +
    path info literal, bảng borderless (box=None) và footer muted với số
    solved dạng ``✔ N/M`` — không cyan ``[*]``, không viền kẻ dọc.
    """
    import contextlib
    import io as _io

    from rich.table import Table
    from rich.text import Text as _Text

    from .ui.theme import load_theme

    base_dir = os.path.abspath(os.path.expanduser(args.dir))

    with contextlib.redirect_stdout(_io.StringIO()):
        try:
            rows = list(StatusService.scan_all_workspaces(args.dir))
        except Exception as e:
            Logger.error(f'Không scan được workspace: {e}')
            sys.exit(1)
    if not rows and not os.path.exists(base_dir):
        Logger.warning(f'Thư mục không tồn tại: {base_dir}')
        return

    ws_console = Console(theme=load_theme(None))
    ws_console.print()
    head = _Text("WORKSPACES", style=_FAINT_COLOR)
    head.append("  ·  ", style=_FAINT_COLOR)
    head.append(base_dir, style=_INFO_COLOR)   # path thật → info literal
    ws_console.print(head)

    table = Table(
        box=None, show_header=True, show_edge=False,
        header_style=_FAINT_COLOR, padding=(0, 2), pad_edge=False)
    table.add_column("WORKSPACE", no_wrap=False)
    table.add_column("PLATFORM", no_wrap=True)
    table.add_column("PROGRESS", no_wrap=True)
    table.add_column("SOLVED", no_wrap=True, justify="right")

    total_solved = 0
    total_challs = 0
    for stats in rows:
        total_solved += stats['solved_challenges']
        total_challs += stats['total_challenges']

        name_cell = _Text(str(stats['title'])[:35], style="fg.base")
        if stats.get('_ended'):
            name_cell.append(" · kết thúc", style=_MUTED_COLOR)

        rate = stats['completion_rate']
        progress_cell = StatusService._meter_only(rate, 10)
        progress_cell.append(f" {rate:.0f}%", style=_MUTED_COLOR)

        # Glyph ✔ xanh CHỈ khi workspace thực sự có solve (codex-r2 P2):
        # 0/N → số trung tính không glyph, không biến semantic thành bullet.
        n_solved = stats['solved_challenges']
        solved_cell = _Text()
        if n_solved > 0:
            solved_cell.append("✔ ", style=_SOLVED_COLOR)
        solved_cell.append(str(n_solved), style="fg.base")
        solved_cell.append(f"/{stats['total_challenges']}",
                           style=_MUTED_COLOR)

        table.add_row(
            name_cell,
            _Text(str(stats['platform'])[:10].lower(), style=_MUTED_COLOR),
            progress_cell,
            solved_cell,
        )

    ws_console.print(table)
    footer = _Text(
        f"{len(rows)} workspace · {total_solved}/{total_challs} "
        f"challs đã solve", style=_MUTED_COLOR)
    ws_console.print(footer)
    ws_console.print()


def handle_instance(args):
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)

    try:
        svc = InstanceService(args.workspace, cookie=cookie_val, token=token_val)
    except Exception as e:
        Logger.error(f'Khởi tạo thất bại: {e}')
        sys.exit(1)

    # 0. Keep-alive foreground (spec event-window §9):
    #    --auto-extend-all → mọi container; --auto-extend → target --id/-n
    if getattr(args, 'auto_extend_all', False) or getattr(args, 'auto_extend', False):
        from .services.instance_keepalive import InstanceKeepAlive
        ka = InstanceKeepAlive(svc, repo=svc.repo)
        targets = None
        if not getattr(args, 'auto_extend_all', False):
            chall = svc.find_challenge(challenge_id=args.id, challenge_name=args.name)
            if not chall:
                Logger.error('--auto-extend cần --id hoặc -n để chỉ định challenge.')
                sys.exit(1)
            targets = [chall.get('id')]
        _run_keepalive_forever(ka, targets)
        return

    # 1. List
    if args.action == 'list' or args.list:
        containers = svc.list_containers()
        if not containers:
            Logger.info('Không có challenge container động nào trong workspace.')
            return
        Logger.info(f'Tìm thấy {len(containers)} challenge container động:')
        print('='*75)
        print(f'{"ID":<8} | {"Thể loại":<12} | {"Tên":<30} | {"Solves":<8}')
        print('='*75)
        for c in containers:
            solves = c.get('solves_count', c.get('solves', '-'))
            c_id = str(c.get('id', '?'))
            c_cat = c.get('category', 'Misc')
            u_name = c.get('name', 'Unknown')[:30]
            print(f"{c_id:<8} | {c_cat:<12} | {u_name:<30} | {str(solves):<8}")
        print('='*75)
        return

    # 2. Interactive — wizard nằm ở InstanceService.interactive_pick
    #    (dùng chung với instance.py / interactive_menu)
    if args.interactive or (not args.action and not args.id and not args.name):
        svc.interactive_pick()
        return

    # 3. Direct action
    target_chall = svc.find_challenge(challenge_id=args.id, challenge_name=args.name)
    if not target_chall:
        Logger.error(f'Không tìm thấy challenge với ID={args.id}, Name={args.name}')
        sys.exit(1)
    cid = target_chall.get('id')

    act = args.action or 'start'
    if act == 'start':
        _start_instance_with_ra_consent(svc, cid,
                                        assume_yes=bool(getattr(args, 'yes', False)))
    elif act == 'stop':
        svc.stop_instance(cid)
    elif act == 'extend':
        svc.extend_instance(cid)
    elif act == 'status':
        st = svc.get_status(cid)
        Logger.info(f'Trạng thái của ID {cid}:')
        for k, v in st.items():
            print(f'  {k}: {v}')


def handle_submit(args):
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)

    svc = SubmitService(
        url=args.url,
        cookie=cookie_val,
        token=token_val,
        workspace_dir=args.workspace,
        flag_format=getattr(args, 'flag_format', None)
    )

    if args.auto:
        svc.auto_submit_all(force=getattr(args, 'force', False))
        return

    chall_id = args.id or (args.target if args.target and args.target.isdigit() else None)
    chall_name = args.name or (args.target if args.target and not args.target.isdigit() else None)
    flag_value = args.flag or args.flag_val
    force_flag = getattr(args, 'force', False)

    if args.interactive or (not chall_id and not chall_name and not flag_value):
        svc.interactive_submit(force=force_flag)
        return

    if not flag_value:
        Logger.error('Vui lòng chỉ định flag bằng -f hoặc làm đối số.')
        sys.exit(1)

    success, message = svc.submit_single_flag(
        challenge_id=chall_id,
        challenge_name=chall_name,
        flag_value=flag_value,
        force=force_flag
    )
    if not success:
        sys.exit(1)


def _sanitize_points(raw) -> int:
    """points metadata có thể là float('inf') (literal Infinity từ platform
    API) / None / rác — sanitize về int >= 0, KHÔNG bao giờ raise."""
    try:
        return max(0, int(raw))
    except (TypeError, ValueError, OverflowError):
        return 0


def _age_human(iso_ts) -> str:
    """Tuổi flag từ ``status.updated_at`` (ISO-8601 UTC 'Z') → '5m'/'3h'/'2d'.
    Thiếu/hỏng timestamp → '-'."""
    import datetime as dt

    if not iso_ts:
        return '-'
    try:
        ts = str(iso_ts).strip().replace('Z', '+00:00')
        then = dt.datetime.fromisoformat(ts)
        if then.tzinfo is None:
            then = then.replace(tzinfo=dt.timezone.utc)
        delta = dt.datetime.now(dt.timezone.utc) - then
    except ValueError:
        return '-'
    secs = max(0, int(delta.total_seconds()))
    if secs < 60:
        return f"{secs}s"
    mins = secs // 60
    if mins < 60:
        return f"{mins}m"
    hours = mins // 60
    if hours < 48:
        return f"{hours}h"
    days = hours // 24
    if days < 30:
        return f"{days}d"
    return f"{days // 30}mo"


#: Trạng thái flag được tính là "đang giữ trong kho" chờ submit.
_HOARD_STATES = ('hoarded', 'found_unverified')


def _collect_hoarded(repo: WorkspaceRepo) -> list:
    """Quét workspace trả về mọi challenge đang GIỮ flag (state ∈
    hoarded/found_unverified và có value). Mỗi dòng: name/points/state/value/
    note/updated_at — sort điểm giảm dần (tie-break tên A→Z)."""
    rows = []
    for meta_path in repo.iter_challenges():
        meta = repo.read_metadata(meta_path)
        if not meta:
            continue
        st = repo.read_status(meta_path, meta=meta)
        fl = st.get('flag') or {}
        state = fl.get('state')
        value = str(fl.get('value') or '').strip()
        if state not in _HOARD_STATES or not value:
            continue
        rows.append({
            'name': str(meta.get('name') or meta.get('id') or '?'),
            'points': _sanitize_points(meta.get('points')),
            'state': state,
            'value': value,
            'note': str(st.get('notes') or '').strip(),
            'updated_at': st.get('updated_at'),
        })
    rows.sort(key=lambda r: (-r['points'], r['name'].lower()))
    return rows


def _render_hoard_list(args):
    """``ctf hoard -w WS --list`` — bảng rich các flag đang giữ chờ submit.

    PHOSPHOR như handle_workspaces: heading UPPERCASE faint, bảng borderless
    (box=None), footer muted tổng kết ``N flags chờ submit · X điểm đang giữ``.
    Glyph lấy TỪ STATUS_ICONS (🏴 hoarded / ❓ found_unverified); flag bị che
    mặc định (4 ký tự đầu + ***) giống history, ``--all`` hiện đầy đủ.
    Workspace chưa có gì → EmptyState thân thiện, KHÔNG lỗi."""
    from rich.table import Table
    from rich.text import Text as _Text

    from .storage.constants import STATUS_ICONS
    from .ui.theme import load_theme

    ws = args.workspace
    if not os.path.isdir(ws):
        Logger.error(f"Workspace không tồn tại: {ws}")
        sys.exit(1)
    repo = WorkspaceRepo(ws)
    entries = _collect_hoarded(repo)

    ws_console = Console(theme=load_theme(None), highlight=False)
    ws_console.print()
    _emit_section_heading("KHO FLAG CHỜ SUBMIT", ws_console)

    if not entries:
        ws_path = os.path.abspath(ws)
        _emit_empty_state(
            "Kho trống — lưu flag bằng ",
            literal="ctf hoard <challenge> <FLAG>",
            tail=f" (workspace: {ws_path}).",
        )
        return

    show_all = bool(getattr(args, 'show_all', False))
    table = Table(
        box=None, show_header=True, show_edge=False,
        header_style=_FAINT_COLOR, padding=(0, 2), pad_edge=False)
    table.add_column("CHALLENGE", no_wrap=False)
    table.add_column("PTS", no_wrap=True, justify="right")
    table.add_column("FLAG", no_wrap=True)
    table.add_column("NOTE", no_wrap=False)
    table.add_column("TUỔI", no_wrap=True, justify="right")

    for e in entries:
        glyph = STATUS_ICONS['flag'].get(e['state'], '❓')
        shown = e['value'] if show_all else _redact_flag(e['value'])
        note = e['note']
        if len(note) > 40:
            note = note[:39] + '…'
        name_cell = _Text(e['name'], style="fg.base")
        flag_cell = _Text(f"{glyph} ", style=_MUTED_COLOR)
        flag_cell.append(shown, style="fg.base" if show_all else _MUTED_COLOR)
        table.add_row(
            name_cell,
            _Text(str(e['points']), style=_MUTED_COLOR),
            flag_cell,
            _Text(note or '-', style=_MUTED_COLOR),
            _Text(_age_human(e['updated_at']), style=_FAINT_COLOR),
        )

    ws_console.print(table)
    total_pts = sum(e['points'] for e in entries)
    footer = _Text(
        f"{len(entries)} flags chờ submit · {total_pts} điểm đang giữ",
        style=_MUTED_COLOR)
    ws_console.print(footer)
    ws_console.print()


def _handle_hoard_remove(args):
    """``ctf hoard <chal> --remove`` — gỡ flag khỏi kho: state về ``none``,
    xoá value. Trục solve KHÔNG bị hạ (nguyên tắc chỉ-nâng). Resolve qua
    StatusService.resolve_challenge (exact id → exact name → substring)."""
    from .services.status_service import (
        AmbiguousChallengeError,
        ChallengeNotFoundError,
    )

    chall_id = getattr(args, 'id', None)
    chall_name = getattr(args, 'name', None)
    identifier = chall_id or chall_name or getattr(args, 'target', None)
    if not identifier:
        Logger.error("Usage: ctf hoard <challenge_id|name> --remove")
        sys.exit(2)

    repo = WorkspaceRepo(args.workspace)
    try:
        meta_path, meta = StatusService.resolve_challenge(repo, identifier)
    except ChallengeNotFoundError as e:
        Logger.error(str(e))
        sys.exit(1)
    except AmbiguousChallengeError as e:
        Logger.error(str(e))
        StatusService._print_matches(e.matches)
        sys.exit(1)

    def _mut(st):
        st["flag"]["value"] = None
        st["flag"]["state"] = "none"
        return st

    repo.update_status(meta_path, _mut)
    shown_name = (meta or {}).get('name') or str(identifier)
    Logger.success("🗑 Đã gỡ flag khỏi kho cho "
                   f"[bold cyan]{shown_name}[/bold cyan].")


def handle_hoard(args):
    """GAP-02 / spec §7: ``ctf hoard`` — kho flag local.

    Ba nhánh:
      - ``--list``: bảng các flag đang giữ (state=hoarded/found_unverified)
        chờ submit; ``--all`` hiện flag đầy đủ.
      - ``<chal> --remove``: gỡ flag khỏi kho (state về none, xoá value).
      - mặc định: ``<chal> <FLAG>`` lưu vào kho (flag.value=x, state=hoarded)
        KHÔNG submit lên platform — qua SubmitService.hoard_flag.

    Quyết định đặt tên: tên ``flag`` theo spec đã bị ``submit`` dùng làm alias
    (tồn tại từ trước) — nên lệnh mới là ``hoard``, alias ``flag-stash``.
    """
    if getattr(args, 'list', False):
        _render_hoard_list(args)
        return

    if getattr(args, 'remove', False):
        _handle_hoard_remove(args)
        return

    chall_id = getattr(args, 'id', None) or (
        args.target if args.target and str(args.target).isdigit() else None)
    chall_name = getattr(args, 'name', None) or (
        args.target if args.target and not str(args.target).isdigit() else None)
    identifier = chall_id if chall_id is not None else chall_name
    flag_value = args.flag or args.flag_val

    if not identifier or not flag_value:
        Logger.error("Usage: ctf hoard <challenge_id|name> <FLAG>\n"
                     "       ctf hoard -w WS --list [--all]\n"
                     "       ctf hoard <challenge_id|name> --remove")
        sys.exit(2)

    try:
        svc = SubmitService(workspace_dir=args.workspace)
    except Exception as e:
        Logger.error(f'Khởi tạo thất bại: {e}')
        sys.exit(1)

    ok, message = svc.hoard_flag(identifier, flag_value)
    if not ok:
        Logger.error(message)
        sys.exit(1)


def _start_instance_with_ra_consent(svc, challenge_id, assume_yes: bool = False):
    """R-A (spec event-window §9): start/restart khi user đang GIỮ flag của
    bài dynamic mà recreate CÓ ĐỔI FLAG (whale/platform không rõ) → bắt buộc
    xác nhận (hoặc --yes); restart xong xoá flag cũ + state found_unverified
    + note rotate qua InstanceKeepAlive.manual_restart_approved. GZCTF giữ
    flag → start bình thường."""
    from .services.instance_keepalive import InstanceKeepAlive

    try:
        ka = InstanceKeepAlive(svc, repo=getattr(svc, 'repo', None))
        trackers = ka.discover_containers()
    except Exception:
        ka, trackers = None, []
    tracker = next((t for t in trackers
                    if str(t.challenge_id) == str(challenge_id)), None)

    if tracker is None:
        # Không phải container đang track → start thường
        svc.start_instance(challenge_id)
        return

    flag = ka._flag_status(tracker)
    holds_flag = bool(flag.get('value')) or flag.get('state', 'none') != 'none'
    if holds_flag and ka.restart_rotates_flag(tracker):
        ok, msg = ka.interactive_restart(tracker, assume_yes=assume_yes)
        if ok:
            Logger.success(f'🔄 Đã restart {tracker.name} — flag cũ hết hiệu lực.')
            try:
                svc.get_status(challenge_id)   # sync entry mới vào metadata
            except Exception:
                pass
        elif msg == 'cancelled':
            Logger.info('Đã huỷ restart — giữ nguyên flag hiện có.')
        else:
            Logger.error(f'Restart thất bại: {msg}')
        return

    svc.start_instance(challenge_id)


def _run_keepalive_forever(ka, targets=None):
    """Vòng lặp keep-alive foreground cho ``ctf instance --auto-extend[-all]``.

    Ctrl-C thoát sạch. Mỗi tick poll 30-60s (state machine tự siết 5s khi
    DUE_SOON/RENEW_FAILED)."""
    import time as _time

    from .services.instance_keepalive import InstanceKeepAlive  # noqa: F401

    Logger.info('♻️ Keep-alive bật — Ctrl-C để thoát.')
    try:
        trackers = ka.discover_containers()
        if targets is not None:
            wanted = {str(t) for t in targets}
            ka.trackers = {cid: tr for cid, tr in ka.trackers.items()
                           if str(cid) in wanted}
        if not ka.trackers:
            Logger.warning('Không có container nào để keep-alive.')
            return
        next_poll = 0.0
        while True:
            events = []
            for tracker in ka.trackers.values():
                try:
                    events.extend(ka.tick_one(tracker))
                except Exception as exc:
                    events.append(('error', f'keepalive {tracker.name}: {exc}'))
            level_icon = {'info': '', 'warning': '⚠️ ',
                          'error': '❌ ', 'critical': '📢 '}
            for lv, msg in events:
                Logger.info(f"{level_icon.get(lv, '')}{msg}")
            # Poll interval: min các next_poll_in của tracker (30-60s bình thường,
            # 5s khi DUE_SOON/RENEW_FAILED)
            next_poll = min((tr.next_poll_in() for tr in ka.trackers.values()),
                            default=45.0)
            _time.sleep(max(2.0, next_poll))
    except KeyboardInterrupt:
        Logger.info('👋 Keep-alive dừng.')


def handle_watch(args):
    """``ctf watch`` — auto-sync trong event window + keep-alive instance."""
    import datetime as _dt

    from .services.watch_service import WatchService, parse_time_arg

    cookie_val = args.cookie
    if cookie_val and os.path.isfile(cookie_val):
        with open(cookie_val, 'r', encoding='utf-8') as f:
            cookie_val = f.read().strip()

    start_utc = parse_time_arg(getattr(args, 'start', None))
    end_utc = parse_time_arg(getattr(args, 'end', None))
    if (getattr(args, 'start', None) and start_utc is None) or \
            (getattr(args, 'end', None) and end_utc is None):
        Logger.error('--start/--end phải là ISO-8601 (vd 2026-08-24T09:00) '
                     'hoặc epoch giây.')
        sys.exit(2)

    svc = WatchService(
        workspace_path=args.workspace,
        cookie=cookie_val,
        token=args.token,
        once=bool(getattr(args, 'once', False)),
        no_scoreboard=bool(getattr(args, 'no_scoreboard', False)),
        start_utc=start_utc,
        end_utc=end_utc,
    )
    try:
        exit_code = svc.run()
    except KeyboardInterrupt:
        exit_code = 130
    if exit_code:
        sys.exit(exit_code)


def handle_register(args):
    """``ctf register`` — auto-register ĐÚNG 1 tài khoản/lần chạy (spec §4).

    Exit code: 0 thành công | 1 thất bại/captcha-unsupported | 2 thiếu tham số
    hoặc đang bị rate limit."""
    from .platforms.base import PlatformRegisterUnsupported
    from .services.register_service import RegisterService

    if not getattr(args, 'url', None):
        Logger.error("Usage: ctf register -u <platform-url> "
                     "[--email me@x.com | --tempmail] [--username PREFIX] [--password PASS]")
        sys.exit(2)

    svc = RegisterService()
    try:
        result = svc.run(
            url=args.url,
            email=getattr(args, 'email', None),
            use_tempmail=bool(getattr(args, 'tempmail', False)),
            username_prefix=getattr(args, 'username_prefix', None) or 'player',
            password=getattr(args, 'password', None),
            workspace=getattr(args, 'workspace', None),
        )
    except PlatformRegisterUnsupported:
        # Service đã in credentials + hướng dẫn thủ công — captcha không bypass.
        sys.exit(1)
    except RuntimeError as exc:
        Logger.error(str(exc))
        sys.exit(2)
    except KeyboardInterrupt:
        Logger.info('Đã huỷ register.')
        sys.exit(130)

    if not result.get('ok'):
        creds = result.get('credentials') or {}
        if creds.get('username'):
            Logger.info(f"Credentials đã sinh (chưa dùng được): "
                        f"{creds.get('username')} / {creds.get('password')} "
                        f"(email: {creds.get('email')})")
        sys.exit(1)


def handle_doctor(args):
    """``ctf doctor`` 🩺 — health-check platform trước giờ giải (P1-3).

    Offline-safe: mỗi check tự bắt exception riêng; mạng chết vẫn render
    đầy đủ report. Exit code: 0 khi có ít nhất 1 check pass | 1 khi tất cả
    fail | 2 thiếu -u."""
    from .services.health_service import HealthService

    url = getattr(args, 'url', None)
    if not url:
        Logger.error("Usage: ctf doctor -u <platform-url> "
                     "[-w workspace] [-c cookie] [-t token]")
        sys.exit(2)

    cookie_val = args.cookie
    if cookie_val and os.path.isfile(cookie_val):
        with open(cookie_val, 'r', encoding='utf-8') as f:
            cookie_val = f.read().strip()

    svc = HealthService()
    try:
        report = svc.check(
            url,
            cookie=cookie_val,
            token=getattr(args, 'token', None),
            workspace=getattr(args, 'workspace', None),
        )
    except Exception as e:
        Logger.error(f'Doctor gặp lỗi bất ngờ: {e}')
        sys.exit(1)
    report.render()
    if report.passed == 0:
        sys.exit(1)


def handle_rank(args):
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)
    try:
        svc = RankService(
            workspace_path=args.workspace,
            url=args.url,
            cookie=cookie_val,
            token=token_val
        )
        svc.display_and_update(top_n=args.top, update_docs=not args.no_docs)
    except Exception as e:
        Logger.error(f'Không lấy được ranking: {e}')
        sys.exit(1)


def handle_sync(args):
    """``ctf sync`` — đồng bộ metadata động workspace ↔ platform (P2-1).

    Handler mỏng: auth từ auth map + dựng platform qua PlatformResolver
    (cùng đường như InstanceService/WatchService), rồi gọi
    ``PullService.sync_workspace(repo, platform)`` — bảng updated/new/drift
    do service tự in. LOCAL STATE LÀ CHỦ: không đụng status/flag/file.
    ``--verify`` chạy thêm ``PullService.verify`` in drift chi tiết.
    """
    from .services.platform_resolver import PlatformResolver

    cookie_val, token_val = get_auth_for_workspace(args.workspace)
    repo = WorkspaceRepo(args.workspace)
    try:
        _session, platform, _info = PlatformResolver.for_workspace(
            repo, cookie=cookie_val, token=token_val)
    except Exception as e:
        Logger.error(f'Không resolve được platform cho workspace '
                     f"'{args.workspace}': {e}")
        sys.exit(1)

    try:
        result = PullService.sync_workspace(repo, platform)
        if getattr(args, 'verify', False):
            verdict = PullService.verify(repo, platform)
            _render_verify_drift(verdict)
    except KeyboardInterrupt:
        Logger.info('👋 Sync dừng.')
        sys.exit(130)
    except Exception as e:
        Logger.error(f'Sync thất bại: {e}')
        sys.exit(1)
    if not result.get('ok'):
        sys.exit(1)


def _render_verify_drift(verdict):
    """In kết quả ``PullService.verify`` (dùng chung cho sync --verify)."""
    drift = verdict.get('unsolved_locally_solved_remotely') or []
    if not drift:
        Logger.success('✅ Verify: không có challenge nào solved trên server '
                       'mà local còn unsolved.')
        return
    rows = [[f"{d.get('name')} ({d.get('category')})",
             'tôi' if d.get('by_me') else 'team',
             ', '.join(d.get('solver_names') or []) or '(không rõ)']
            for d in drift]
    Logger.print_table(
        'Verify — local chưa solve, server đã solve',
        ['Challenge', 'Ai', 'Người solve'], rows)
    Logger.warning("⚠️ KHÔNG tự đổi trạng thái — user quyết định qua "
                   "'status set' hoặc submit flag.")


def handle_export_pack(args):
    """``ctf export-pack`` — đóng gói writeup các bài đã solve thành pack
    markdown + zip (P2-3). Handler mỏng quanh WriteupExporter."""
    from .services.writeup_exporter import WriteupExporter

    try:
        exporter = WriteupExporter(args.workspace)
        pack_dir = exporter.build_pack(args.out or '.')
    except ValueError as e:
        # Không có bài nào đạt điều kiện export — message đã hướng dẫn chi tiết.
        Logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        Logger.error(f'Export thất bại: {e}')
        sys.exit(1)
    # Cảnh báo validate + dòng tổng kết do service tự in qua err_console
    # (PHOSPHOR stderr) — handler KHÔNG in lại để tránh trùng output.


# Icon kết quả submit cho `ctf history` (result strings của SubmitService).
_HISTORY_RESULT_ICONS = {
    'correct': '🚩✔',
    'incorrect': '⛔',
    'ratelimited': '⏳',
}


def _redact_flag(flag) -> str:
    """Che flag: 4 ký tự đầu + *** (mặc định; --all hiện đầy đủ)."""
    flag = str(flag or '').strip()
    if not flag:
        return '-'
    return flag[:4] + '***'


def _emit_section_heading(title: str, console_=None) -> None:
    """Heading section PHOSPHOR: UPPERCASE faint (chrome neutral)."""
    target = console_ or console
    target.print(Text(title.upper(), style=_FAINT_COLOR))


def _emit_wrapped(segments, indent="  ", console_=None) -> None:
    """In một dòng gồm nhiều ``(text, style)`` với word-wrap có chủ đích:
    continuation line được thụt ``indent`` (2 spaces mặc định), KHÔNG treo
    ở cột 0 khi terminal hẹp."""
    target = console_ or console
    width = getattr(target, 'width', None) or 80
    avail = max(30, int(width) - len(indent))

    words = []
    for text, style in segments:
        for word in str(text).split(" "):
            if word:
                words.append((word, style))

    lines = []
    current, cur_len = [], 0
    for word, style in words:
        extra = len(word) + (1 if current else 0)
        if current and cur_len + extra > avail:
            lines.append(current)
            current, cur_len = [(word, style)], len(word)
        else:
            current.append((word, style))
            cur_len += extra
    if current:
        lines.append(current)

    for i, line_words in enumerate(lines):
        line = Text("" if i == 0 else indent)
        for j, (word, style) in enumerate(line_words):
            if j:
                line.append(" ")
            line.append(word, style=style)
        target.print(line)


def _emit_empty_state(message: str, literal: str = "", tail: str = "") -> None:
    """EmptyState chung: dòng ``·`` muted + literal/path info (chỉ khi là
    path/literal thật — không bao giờ cyan ``[*]`` trần). Wrap an toàn qua
    :func:`_emit_wrapped`."""
    segments = [("· ", _MUTED_COLOR), (message, _MUTED_COLOR)]
    if literal:
        segments.append((literal, _INFO_COLOR))
    if tail:
        segments.append((tail, _MUTED_COLOR))
    _emit_wrapped(segments)


def handle_history(args):
    """``ctf history`` — bảng lịch sử submit từ submit_history.json.

    Flag bị che mặc định (4 ký tự đầu + ***) chống lộ khi share screen;
    ``--all`` hiện đầy đủ. Workspace chưa từng submit → EmptyState thân thiện
    (heading UPPERCASE faint + dòng ``·`` muted), KHÔNG lỗi.
    """
    if not os.path.isdir(args.workspace):
        Logger.error(f"Workspace không tồn tại: {args.workspace}")
        sys.exit(1)
    repo = WorkspaceRepo(args.workspace)
    entries = repo.load_submit_history().get('entries') or []
    _emit_section_heading("LỊCH SỬ SUBMIT")
    if not entries:
        # Path/filename thật mới được màu info literal (quy tắc palette §3).
        ws_path = os.path.abspath(args.workspace)
        _emit_empty_state(
            "Chưa có lịch sử submit trong workspace ",
            literal=ws_path,
            tail=" — submit_history.json chưa tồn tại.",
        )
        return

    show_all = bool(getattr(args, 'show_all', False))
    rows = []
    # C6-02: entry thiếu challenge_id KHÔNG được đưa vào find_challenge —
    # cid=None rơi xuống tier substring ("none" in name) và gán nhầm tên
    # challenge nào đó chứa "none" cho một submit vô danh.
    # PERF: find_challenge quét toàn bộ workspace metadata mỗi lần gọi —
    # cache theo cid để history N entries chỉ scan tối đa (số cid distinct)
    # lần thay vì N lần.
    chall_cache = {}
    for e in entries:
        cid = e.get('challenge_id')
        if cid is None:
            chall = None
        else:
            key = str(cid)
            if key not in chall_cache:
                try:
                    chall_cache[key] = repo.find_challenge(cid)
                except Exception:
                    chall_cache[key] = None
            chall = chall_cache[key]
        name = (chall or {}).get('name') or str(cid if cid is not None else '?')
        icon = _HISTORY_RESULT_ICONS.get(e.get('result'), '❓')
        flag = str(e.get('flag', '') or '')
        shown = flag if show_all else _redact_flag(flag)
        rows.append([e.get('timestamp') or '-', str(name),
                     f"{icon} {e.get('result') or 'unknown'}", shown])
    Logger.print_table('Lịch sử submit',
                       ['Thời gian (UTC)', 'Challenge', 'Kết quả', 'Flag'],
                       rows)
    if not show_all:
        Logger.info('(Flag đang bị che — dùng --all để hiện đầy đủ.)')


def handle_sniper(args):
    """``ctf sniper`` — preload flag, nộp tự động đúng giờ G (P2-6).

    Handler mỏng: dựng SubmitService (tự resolve URL từ workspace) +
    SniperService rồi gọi run(). Cảnh báo automation do service tự in;
    Ctrl-C được service bắt sạch (in target còn lại) — đây chỉ là lớp chặn
    phòng thủ với exit code 130.
    """
    from .services.sniper_service import SniperService
    from .services.submit_service import SubmitService

    cookie_val, token_val = get_auth_for_workspace(args.workspace)
    try:
        submitter = SubmitService(cookie=cookie_val, token=token_val,
                                  workspace_dir=args.workspace)
    except Exception as e:
        Logger.error(f'Khởi tạo thất bại: {e}')
        sys.exit(1)

    svc = SniperService(WorkspaceRepo(args.workspace), submitter)
    try:
        svc.run(poll_interval=float(getattr(args, 'poll', 10) or 10),
                start_at=getattr(args, 'start_at', None),
                retry_wrong=bool(getattr(args, 'retry_wrong', False)))
    except KeyboardInterrupt:
        Logger.info('👋 Sniper dừng — target còn lại vẫn giữ trong sniper.json.')
        sys.exit(130)


def handle_serve(args):
    """``ctf serve`` — dashboard web read-only (P2-4).

    Mặc định bind 127.0.0.1 (KHÔNG expose LAN); Ctrl-C tắt server sạch
    (WebDashboard.serve tự xử lý). Port bận → OSError → exit 1.
    """
    if not os.path.isdir(args.workspace):
        Logger.error(f"Workspace không tồn tại: {args.workspace}")
        sys.exit(1)
    from .services.web_dashboard import WebDashboard

    port = int(getattr(args, 'port', WebDashboard.DEFAULT_PORT))
    Logger.info(f'🌐 Dashboard sẵn sàng — mở http://127.0.0.1:{port}/ '
                f'trong trình duyệt.')
    try:
        WebDashboard(WorkspaceRepo(args.workspace)).serve(port=port)
    except OSError as e:
        Logger.error(str(e))
        sys.exit(1)
    except KeyboardInterrupt:
        pass
    Logger.info('👋 Dashboard đã tắt.')


def _prompt_yes_no(question: str) -> bool:
    """Hỏi y/N trên tty — chỉ trả True khi user gõ y/yes.

    Non-tty luôn trả False (không bao giờ tự xác nhận thao tác phá dữ liệu).
    Dùng ``sys.stdin.readline`` thay vì ``input()`` để tuân thủ rule Phase 7:
    tầng CLI cấm gọi input()/Prompt.ask/Confirm.ask (AST check).
    """
    if not sys.stdin.isatty():
        return False
    try:
        console.print(f"{question} ", end="")
        console.print("[bold]y/N[/bold]", end=" ")
        answer = sys.stdin.readline()
    except Exception:
        return False
    return answer.strip().lower() in ('y', 'yes')


def handle_storage(args):
    """``ctf storage`` (alias du/archive) — báo cáo dung lượng + archive.

    - Không subcommand: scan_usage + format_report (+ suggest_actions khi có
      gợi ý thực sự, bỏ qua dòng ✅ all-good).
    - Subcommand ``archive <workspace_name>``: confirm (hoặc --yes), gọi
      StorageManager.archive_workspace, in ratio, rồi HỎI RIÊNG việc xoá
      workspace gốc — chỉ xoá khi user gõ yes (delete là rename an toàn).
    """
    from .services.storage_manager import StorageManager, human_size

    if getattr(args, 'storage_command', None) == 'archive':
        _handle_storage_archive(args, StorageManager, human_size)
        return

    usages = StorageManager.scan_usage(args.base_dir)
    # format_report trả markup rich-ready (glyph ngưỡng !/✗ ở cột NOTE, nhãn
    # faint) — in qua rich console để resolve, không print() thô.
    # soft_wrap=True: bảng rộng không bị ngắt dòng giữa các cột ở terminal
    # hẹp (giữ hành vi print() cũ).
    # highlight=False: tắt ReprHighlighter của rich — không tô cyan tự do
    # lên các con số trong bảng (palette §3: số liệu neutral).
    console.print(
        StorageManager.format_report(usages, threshold_mb=args.threshold_mb),
        soft_wrap=True, highlight=False)

    suggestions = StorageManager.suggest_actions(
        args.base_dir, threshold_mb=args.threshold_mb
    )
    meaningful = [s for s in suggestions if not s.startswith('✔')]
    if meaningful:
        _render_suggestions(meaningful)


def _render_suggestions(items):
    """Gợi ý storage dạng list PHOSPHOR: heading UPPERCASE faint, glyph
    semantic (! warn / ℹ info) màu đúng luật, continuation line wrap với
    indent 2 spaces (không treo dòng ở cột 0)."""
    width = getattr(console, 'width', None) or 80
    # Khoảng thở kép trước block GỢI Ý — tách biệt rõ hơn khỏi bảng
    # (codex-r2 P1: bảng dài khá phẳng, tăng hierarchy block gợi ý).
    console.print()
    console.print()
    console.print(Text('GỢI Ý', style=_FAINT_COLOR))
    for s in items:
        glyph, gstyle, body = '', '', s
        if s.startswith('! '):
            glyph, gstyle, body = '!', '#EAC54F', s[2:]
        elif s.startswith('ℹ '):
            glyph, gstyle, body = 'ℹ', _INFO_COLOR, s[2:]
        chunks = textwrap.wrap(
            body, width=max(40, int(width) - 4),
            break_on_hyphens=False) or [body]
        line = Text()
        if glyph:
            line.append(glyph + ' ', style=gstyle)
        else:
            line.append('- ', style=_MUTED_COLOR)
        line.append(chunks[0])
        console.print(line)
        for chunk in chunks[1:]:
            console.print(Text('  ' + chunk))


def _handle_storage_archive(args, StorageManager, human_size):
    base_dir = os.path.expanduser(args.base_dir)
    ws_path = os.path.join(base_dir, args.workspace_name)

    if not os.path.isdir(ws_path):
        Logger.error(f"Workspace không tồn tại: {ws_path}")
        sys.exit(1)

    # Confirm 1: archive. Non-tty không --yes → exit 2 (bắt buộc --yes).
    if not args.yes:
        if not sys.stdin.isatty():
            Logger.error(
                'Chạy non-interactive: cần --yes để xác nhận archive '
                '(không bao giờ tự xác nhận).'
            )
            sys.exit(2)
        if not _prompt_yes_no(
                f"Xác nhận archive workspace '{args.workspace_name}'?"):
            Logger.info('Đã huỷ — không archive.')
            return

    try:
        result = StorageManager.archive_workspace(
            ws_path,
            out_dir=args.out,
            git_remote=args.git_remote,
        )
    except Exception as exc:
        Logger.error(f'Archive thất bại: {exc}')
        sys.exit(1)

    Logger.success(
        f"Đã archive → {result['archive_path']} "
        f"({human_size(result['original_bytes'])} → "
        f"{human_size(result['archived_bytes'])}, "
        f"ratio {result['ratio']:.2%})"
    )

    # Confirm 2 (riêng biệt): xoá workspace gốc — CHỈ khi user gõ yes.
    # Non-tty → skip hoàn toàn (dữ liệu giữ nguyên).
    if _prompt_yes_no(
            f"Xoá workspace gốc '{args.workspace_name}'? "
            f"(rename an toàn vào _archives)"):
        trash = StorageManager.delete_workspace(ws_path)
        Logger.success(f"Đã chuyển workspace vào thùng rác: {trash}")
    else:
        Logger.info('Giữ nguyên workspace gốc.')


# ----------------------------------------------------------------------
# CONFIG — xem/đặt cấu hình toàn cục (spec event-window §4)
# ----------------------------------------------------------------------

#: Registry các key config toàn cục điều khiển được từ ``ctf config``.
#: ``path`` = vị trí lưu trong global config JSON (~/.config/ctf_toolkit/
#: config.json); ``values`` = bảng giá trị CLI hợp lệ -> giá trị lưu.
#: Spec event-window §4 ("Đổi ý: ctf config auto-sync off"). Precedence
#: (R6): giá trị toàn cục là MẶC ĐỊNH; ``.ctf/config.json`` của workspace
#: là OVERRIDE — watch_service đọc hai tầng qua resolve_auto_sync_enabled.
_CONFIG_KEYS = {
    'auto-sync': {
        'path': ('auto_sync', 'enabled'),
        'values': {'on': True, 'off': False},
        'default': True,
        'desc': ('Tự động cập nhật challenge/scoreboard/notices '
                 '(ctf watch) — mặc định toàn cục, workspace '
                 '.ctf/config.json override'),
    },
}


def _config_render(spec, stored):
    """Giá trị lưu trong JSON -> chuỗi hiển thị CLI (vd True -> 'on')."""
    for name, val in spec['values'].items():
        if val == stored:
            return name
    return str(stored)


def handle_config(args):
    """``ctf config`` — xem/đặt cấu hình toàn cục.

    - Không đối số: liệt kê mọi key biết được + giá trị hiện tại.
    - ``ctf config <key>``: xem giá trị hiện tại của một key.
    - ``ctf config <key> <value>``: đặt giá trị mới + persist global config.
    Exit code: 0 thành công | 2 key lạ hoặc giá trị lạ.
    """
    from .storage.global_config import (
        GLOBAL_CONFIG_FILE, load_global_config, save_global_config,
    )

    key = getattr(args, 'key', None)
    value = getattr(args, 'value', None)

    if key is not None and key not in _CONFIG_KEYS:
        Logger.error(f"Key không hỗ trợ: '{key}'. Các key biết được: "
                     f"{', '.join(sorted(_CONFIG_KEYS))}.")
        sys.exit(2)

    spec = _CONFIG_KEYS.get(key)          # None khi liệt kê (không có key)
    normalized = None
    if value is not None:
        normalized = value.strip().lower()
        if normalized not in spec['values']:
            Logger.error(f"Giá trị không hợp lệ cho '{key}': '{value}' "
                         f"(nhận: {'|'.join(spec['values'])}).")
            sys.exit(2)

    cfg = load_global_config()

    if value is None:                                   # chế độ XEM
        shown = sorted(_CONFIG_KEYS.items()) if key is None else [(key, spec)]
        Logger.info(f'Cấu hình toàn cục ({GLOBAL_CONFIG_FILE}):')
        for name, kspec in shown:
            node, found = cfg, True
            for part in kspec['path']:
                if isinstance(node, dict) and part in node:
                    node = node[part]
                else:
                    found = False
                    break
            current = node if found else kspec['default']
            rendered = _config_render(kspec, current)
            suffix = '' if found else ' (mặc định)'
            Logger.info(f'  {name:<12} = {rendered}{suffix} — {kspec["desc"]}')
        return

    # Chế độ ĐẶT: ghi đúng path của key, giữ nguyên mọi dữ liệu khác
    # (workspaces/auth/…) đang có trong global config.
    new_val = spec['values'][normalized]
    node = cfg
    for part in spec['path'][:-1]:
        child = node.get(part) if isinstance(node, dict) else None
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[spec['path'][-1]] = new_val
    save_global_config(cfg)
    Logger.success(f"Đã lưu {key} = {_config_render(spec, new_val)} "
                   f"({GLOBAL_CONFIG_FILE}).")
