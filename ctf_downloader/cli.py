import os
import sys
import argparse
from typing import Optional
from rich.prompt import Prompt, Confirm

from .config import DownloaderConfig
from .core import CTFDownloader
from .submitter import FlagSubmitter
from .instance_manager import InstanceManager
from .dashboard import CTFDashboard
from .interactive_menu import launch_interactive_menu
from .services.auth_service import AuthService
from .utils.logger import Logger, console

def get_auth_for_workspace(ws_path: str, cookie_arg: Optional[str], token_arg: Optional[str]):
    return AuthService.resolve(ws_path, cookie_arg=cookie_arg, token_arg=token_arg)

def build_unified_parser():
    parser = argparse.ArgumentParser(
        prog='ctf',
        description='⚡ CTF Toolkit: Unified CTF Downloader, Submitter, Container Manager & Dashboard ⚡',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Quick Examples:
  # 1. Interactive Menu (Default):
  ctf

  # 2. Download a CTF Competition:
  ctf pull -u https://ctf.example.com -c "session=xxx" -o ./my_ctf

  # 3. View Challenge Hierarchy & Progress:
  ctf status -w ./my_ctf
  ctf status -u   # Only unsolved challenges

  # 4. Manage Dynamic Containers (Start/Stop/Extend):
  ctf instance --list
  ctf instance start --id 34 -c "session=xxx"

  # 5. Submit Flag:
  ctf submit --id 16 -f "FLAG{...}"
  ctf submit --auto   # Submit all solved flags in workspace

  # 6. Scan all downloaded CTF workspaces:
  ctf workspaces
        '''
    )
    parser.add_argument('-v', '--version', action='version', version='ctf-toolkit 2.0.0')
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
    pull_parser.add_argument('--timeout', type=int, default=30, help='Request timeout in seconds (default: 30)')
    pull_parser.add_argument('-i', '--interactive', action='store_true', help='Launch interactive download wizard')

    # 2. STATUS / TREE / LS / DASHBOARD
    status_parser = subparsers.add_parser('status', aliases=['tree', 'ls', 'dashboard'], help='Display challenge structure, points, and solve progress')
    status_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    status_parser.add_argument('-u', '--unsolved', action='store_true', help='Show only unsolved challenges')
    status_parser.add_argument('-s', '--solved', action='store_true', help='Show only solved challenges')
    status_parser.add_argument('-C', '--category', nargs='+', help='Filter specific categories (e.g. -C Web Crypto)')
    status_parser.add_argument('--container', action='store_true', help='Filter only dynamic container challenges')

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

    # 6. RANK / SCOREBOARD / LEADERBOARD
    rank_parser = subparsers.add_parser('rank', aliases=['scoreboard', 'leaderboard'], help='Display live scoreboard standings and update ranking docs')
    rank_parser.add_argument('-w', '--workspace', default='.', help='CTF workspace directory (default: current dir)')
    rank_parser.add_argument('-u', '--url', help='Platform base URL')
    rank_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    rank_parser.add_argument('-t', '--token', help='API token or Bearer token')
    rank_parser.add_argument('-n', '--top', type=int, default=15, help='Number of top teams to display (default: 15)')
    rank_parser.add_argument('--no-docs', action='store_true', help='Do not write/update RANKING.md or SUMMARY.md')

    # 7. MENU / UI / INTERACTIVE
    menu_parser = subparsers.add_parser('menu', aliases=['ui', 'console'], help='Launch full interactive CTF suite dashboard')
    menu_parser.add_argument('-w', '--workspace', default=None, help='CTF workspace directory')
    menu_parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    menu_parser.add_argument('-t', '--token', help='API token or Bearer token')

    return parser

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
        downloader = CTFDownloader(config)
        success = downloader.run()
        if not success:
            sys.exit(1)
    except KeyboardInterrupt:
        console.print("[bold red][!] Download aborted by user.[/bold red]")
        sys.exit(130)
    except Exception as e:
        Logger.error(f'Fatal error during pull: {e}')
        sys.exit(1)

def handle_status(args):
    dash = CTFDashboard(args.workspace)
    dash.render_tree(
        filter_cat=args.category,
        only_unsolved=args.unsolved,
        only_solved=args.solved,
        only_container=args.container
    )

def handle_workspaces(args):
    base_dir = os.path.abspath(os.path.expanduser(args.dir))
    print('=' * 85)
    print(f' 📁 SCANNING ALL CTF WORKSPACES IN: {base_dir}')
    print('=' * 85)
    print(f'{"CTF Competition":<35} | {"Platform":<10} | {"Solved/Total":<14} | {"Progress":<15}')
    print('=' * 85)
    
    if not os.path.exists(base_dir):
        Logger.warning(f'Directory {base_dir} does not exist.')
        return

    for entry in sorted(os.listdir(base_dir)):
        full_p = os.path.join(base_dir, entry)
        if os.path.isdir(full_p):
            dash = CTFDashboard(full_p)
            stats = dash.get_summary_stats()
            if stats['total_challenges'] > 0:
                title = stats['title'][:35]
                plat = stats['platform'][:10].upper()
                solv_str = f"{stats['solved_challenges']}/{stats['total_challenges']}"
                rate = stats['completion_rate']
                bar = '█' * int(8 * rate // 100) + '░' * (8 - int(8 * rate // 100))
                prog_str = f'[{bar}] {rate:.0f}%'
                print(f'{title:<35} | {plat:<10} | {solv_str:<14} | {prog_str:<15}')
    print('=' * 85)

def handle_instance(args):
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)

    try:
        mgr = InstanceManager(args.workspace, cookie=cookie_val, token=token_val)
    except Exception as e:
        Logger.error(f'Initialization error: {e}')
        sys.exit(1)

    # 1. List
    if args.action == 'list' or args.list:
        containers = mgr.list_containers()
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

    # 2. Interactive
    if args.interactive or (not args.action and not args.id and not args.name):
        containers = mgr.list_containers()
        if not containers:
            Logger.warning('No container challenges detected. Enter challenge ID manually.')
            chall_id = input('Enter Challenge ID: ').strip()
        else:
            print("\nSelect Challenge to manage:")
            for idx, c in enumerate(containers, 1):
                print(f'  [{idx}] {c.get("name")} (ID: {c.get("id")}, {c.get("category")})')
            choice = input(f'Choice (1-{len(containers)}): ').strip()
            try:
                selected = containers[int(choice) - 1]
                chall_id = selected.get('id')
            except Exception:
                Logger.error('Invalid choice.')
                return

        print("\nAction:")
        print('  [1] Start / Renew Container')
        print('  [2] Check Container Status')
        print('  [3] Extend Container Lifetime')
        print('  [4] Stop / Destroy Container')
        act = input('Choice (1-4): ').strip()
        if act == '1':
            mgr.start_instance(chall_id)
        elif act == '2':
            st = mgr.get_status(chall_id)
            Logger.info(f'Status: {st}')
        elif act == '3':
            mgr.extend_instance(chall_id)
        elif act == '4':
            mgr.stop_instance(chall_id)
        return

    # 3. Direct action
    target_chall = mgr.find_challenge(challenge_id=args.id, challenge_name=args.name)
    if not target_chall:
        Logger.error(f'Challenge not found for ID={args.id}, Name={args.name}')
        sys.exit(1)
    cid = target_chall.get('id')

    act = args.action or 'start'
    if act == 'start':
        mgr.start_instance(cid)
    elif act == 'stop':
        mgr.stop_instance(cid)
    elif act == 'extend':
        mgr.extend_instance(cid)
    elif act == 'status':
        st = mgr.get_status(cid)
        Logger.info(f'Status for ID {cid}:')
        for k, v in st.items():
            print(f'  {k}: {v}')

def handle_submit(args):
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)

    submitter = FlagSubmitter(
        url=args.url,
        cookie=cookie_val,
        token=token_val,
        workspace_dir=args.workspace,
        flag_format=getattr(args, 'flag_format', None)
    )

    if args.auto:
        submitter.auto_submit_all(force=getattr(args, 'force', False))
        return

    chall_id = args.id or (args.target if args.target and args.target.isdigit() else None)
    chall_name = args.name or (args.target if args.target and not args.target.isdigit() else None)
    flag_value = args.flag or args.flag_val
    force_flag = getattr(args, 'force', False)

    if args.interactive or (not chall_id and not chall_name and not flag_value):
        submitter.interactive_submit(force=force_flag)
        return

    if not flag_value:
        Logger.error('Please specify the flag string with -f or as an argument.')
        sys.exit(1)

    success, message = submitter.submit_single_flag(
        challenge_id=chall_id,
        challenge_name=chall_name,
        flag_value=flag_value,
        force=force_flag
    )
    if not success:
        sys.exit(1)

def handle_rank(args):
    from .ranking import RankingManager
    cookie_val, token_val = get_auth_for_workspace(args.workspace, args.cookie, args.token)
    try:
        mgr = RankingManager(
            workspace_path=args.workspace,
            url=args.url,
            cookie=cookie_val,
            token=token_val
        )
        mgr.display_and_update(top_n=args.top, update_docs=not args.no_docs)
    except Exception as e:
        Logger.error(f'Failed to fetch ranking: {e}')
        sys.exit(1)

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
        handle_status(args)
    elif cmd in ['workspaces', 'scan']:
        handle_workspaces(args)
    elif cmd in ['instance', 'container', 'spawn']:
        handle_instance(args)
    elif cmd in ['submit', 'flag']:
        handle_submit(args)
    elif cmd in ['rank', 'scoreboard', 'leaderboard']:
        handle_rank(args)
    elif cmd in ['menu', 'ui', 'console']:
        launch_interactive_menu(workspace_path=args.workspace, cookie=args.cookie, token=args.token)
    else:
        launch_interactive_menu()

if __name__ == '__main__':
    main()
