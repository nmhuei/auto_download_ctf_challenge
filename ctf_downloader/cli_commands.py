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
        svc.start_instance(cid)
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
