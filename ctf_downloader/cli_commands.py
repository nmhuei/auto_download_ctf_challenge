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
        exclude_categories=args.exclude
    )

    try:
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
        only_container=args.container
    )


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
