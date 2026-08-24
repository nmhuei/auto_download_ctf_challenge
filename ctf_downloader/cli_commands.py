"""Lớp command mỏng cho unified CLI: parse (ở cli.py) -> service -> render -> exit code.

Quy tắc kiến trúc Phase 7: file này KHÔNG chứa ``input()`` / ``Prompt.ask`` /
``Confirm.ask`` nào — mọi wizard interactive nằm ở tầng services
(``InstanceService.interactive_pick`` / ``SubmitService.interactive_submit``)
hoặc ở interactive_menu.
"""
import os
import sys
from typing import Optional

from .config import DownloaderConfig
from .interactive_menu import launch_interactive_menu
from .services.auth_service import AuthService
from .services.instance_service import InstanceService
from .services.pull_service import PullService
from .services.rank_service import RankService
from .services.status_service import StatusService
from .services.submit_service import SubmitService
from .storage.workspace_repo import WorkspaceRepo
from .utils.logger import Logger, console


def get_auth_for_workspace(ws_path: str, cookie_arg: Optional[str] = None,
                           token_arg: Optional[str] = None):
    """Re-export mỏng quanh AuthService.resolve (giữ tên cũ cho script legacy)."""
    return AuthService.resolve(ws_path, cookie_arg=cookie_arg, token_arg=token_arg)


def handle_pull(args):
    if args.interactive or not args.url:
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
        console.print("[bold red][!] Download aborted by user.[/bold red]")
        sys.exit(130)
    except Exception as e:
        Logger.error(f'Fatal error during pull: {e}')
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


def handle_workspaces(args):
    StatusService.scan_all_workspaces(args.dir)


def handle_instance(args):
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)

    try:
        svc = InstanceService(args.workspace, cookie=cookie_val, token=token_val)
    except Exception as e:
        Logger.error(f'Initialization error: {e}')
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
            Logger.info('No dynamic container challenges found in workspace.')
            return
        Logger.info(f'Found {len(containers)} dynamic container challenges:')
        print('='*75)
        print(f'{"ID":<8} | {"Category":<12} | {"Name":<30} | {"Solves":<8}')
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
        Logger.error(f'Challenge not found for ID={args.id}, Name={args.name}')
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
        Logger.info(f'Status for ID {cid}:')
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
        Logger.error('Please specify the flag string with -f or as an argument.')
        sys.exit(1)

    success, message = svc.submit_single_flag(
        challenge_id=chall_id,
        challenge_name=chall_name,
        flag_value=flag_value,
        force=force_flag
    )
    if not success:
        sys.exit(1)


def handle_hoard(args):
    """GAP-02 / spec §7: ``ctf hoard <chal> <FLAG>`` — lưu flag tìm được vào kho
    local (flag.value=x, state=hoarded) KHÔNG submit lên platform.

    Quyết định đặt tên: tên ``flag`` theo spec đã bị ``submit`` dùng làm alias
    (tồn tại từ trước) — nên lệnh mới là ``hoard``, alias ``flag-stash``.
    """
    chall_id = getattr(args, 'id', None) or (
        args.target if args.target and str(args.target).isdigit() else None)
    chall_name = getattr(args, 'name', None) or (
        args.target if args.target and not str(args.target).isdigit() else None)
    identifier = chall_id if chall_id is not None else chall_name
    flag_value = args.flag or args.flag_val

    if not identifier or not flag_value:
        Logger.error("Usage: ctf hoard <challenge_id|name> <FLAG>")
        sys.exit(2)

    try:
        svc = SubmitService(workspace_dir=args.workspace)
    except Exception as e:
        Logger.error(f'Initialization error: {e}')
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
        Logger.error(f'Doctor failed unexpectedly: {e}')
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
        Logger.error(f'Failed to fetch ranking: {e}')
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
        Logger.error(f'Cannot resolve platform for workspace '
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
        Logger.error(f'Sync failed: {e}')
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
             'me' if d.get('by_me') else 'team',
             ', '.join(d.get('solver_names') or []) or '(không rõ)']
            for d in drift]
    Logger.print_table(
        'Verify — unsolved locally, solved remotely',
        ['Challenge', 'By', 'Solvers'], rows)
    Logger.warning("⚠️ KHÔNG tự đổi trạng thái — user quyết định qua "
                   "'status set' hoặc submit flag.")


def handle_export_pack(args):
    """``ctf export-pack`` — đóng gói writeup các bài đã solve thành pack
    markdown + zip (P2-3). Handler mỏng quanh WriteupExporter."""
    from .services.writeup_exporter import WriteupExporter
    from rich.markup import escape

    try:
        exporter = WriteupExporter(args.workspace)
        entries = exporter.collect()
    except Exception as e:
        Logger.error(f'Export failed: {e}')
        sys.exit(1)

    # escape(): cảnh báo chứa tên challenge/category dạng [tag] không được
    # rich nuốt mất như markup.
    for w in exporter.validate(entries):
        console.print(escape(w), style='yellow')

    try:
        pack_dir = exporter.build_pack(args.out or '.')
    except ValueError as e:
        # Không có bài nào đạt điều kiện export — service đã hướng dẫn chi tiết.
        Logger.error(str(e))
        sys.exit(1)
    except Exception as e:
        Logger.error(f'Export failed: {e}')
        sys.exit(1)
    Logger.success(f'📦 Đã export writeup pack: {pack_dir}.zip')


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


def handle_history(args):
    """``ctf history`` — bảng lịch sử submit từ submit_history.json.

    Flag bị che mặc định (4 ký tự đầu + ***) chống lộ khi share screen;
    ``--all`` hiện đầy đủ. Workspace chưa từng submit → thông báo thân thiện,
    KHÔNG lỗi.
    """
    if not os.path.isdir(args.workspace):
        Logger.error(f"Workspace không tồn tại: {args.workspace}")
        sys.exit(1)
    repo = WorkspaceRepo(args.workspace)
    entries = repo.load_submit_history().get('entries') or []
    if not entries:
        Logger.info(f"Chưa có lịch sử submit nào trong workspace "
                    f"'{args.workspace}' (submit_history.json chưa tồn tại).")
        return

    show_all = bool(getattr(args, 'show_all', False))
    rows = []
    for e in entries:
        cid = e.get('challenge_id')
        try:
            chall = repo.find_challenge(cid)
        except Exception:
            chall = None
        name = (chall or {}).get('name') or str(cid if cid is not None else '?')
        icon = _HISTORY_RESULT_ICONS.get(e.get('result'), '❓')
        flag = str(e.get('flag', '') or '')
        shown = flag if show_all else _redact_flag(flag)
        rows.append([e.get('timestamp') or '-', str(name),
                     f"{icon} {e.get('result') or 'unknown'}", shown])
    Logger.print_table('Submit History',
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
        Logger.error(f'Initialization error: {e}')
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
    print(StorageManager.format_report(usages, threshold_mb=args.threshold_mb))

    suggestions = StorageManager.suggest_actions(
        args.base_dir, threshold_mb=args.threshold_mb
    )
    meaningful = [s for s in suggestions if not s.startswith('✅')]
    if meaningful:
        print()
        console.print('[bold]Gợi ý:[/bold]')
        for s in meaningful:
            console.print(f'- {s}')


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
        f"📦 Đã archive → {result['archive_path']} "
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
        Logger.success(f"🗑️ Đã chuyển workspace vào thùng rác: {trash}")
    else:
        Logger.info('Giữ nguyên workspace gốc.')
