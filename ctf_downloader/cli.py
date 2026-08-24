"""Unified CLI: định nghĩa argparse + dispatch. Logic command nằm ở cli_commands
(lớp mỏng gọi services); script legacy nằm ở cli_legacy."""
import argparse
import os
import sys

from .cli_commands import (  # noqa: F401 — re-export cho script legacy/test cũ
    get_auth_for_workspace,
    handle_instance,
    handle_pull,
    handle_rank,
    handle_status,
    handle_submit,
    handle_workspaces,
)
from .interactive_menu import launch_interactive_menu


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
