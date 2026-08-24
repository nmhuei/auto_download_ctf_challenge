"""Legacy entrypoints: argparse NGUYÊN VĂN của 4 script root cũ
(submit.py / manage.py / instance.py / rank.py).

Các script root giờ là shim ≤10 dòng gọi vào đây — help text & argv
không đổi với user. Body được route về tầng services nơi đã có bản
1:1 (SubmitService / RankService / InstanceService /
StatusService.scan_all_workspaces); wizard interactive (có input/Prompt)
nằm ở services hoặc giữ nguyên tại đây cho manage.
"""
import argparse
import json
import os
import sys

from ctf_downloader.dashboard import CTFDashboard
from ctf_downloader.instance_manager import InstanceManager
from ctf_downloader.services.auth_service import AuthService
from ctf_downloader.services.instance_service import InstanceService
from ctf_downloader.services.rank_service import RankService
from ctf_downloader.services.status_service import StatusService
from ctf_downloader.services.submit_service import SubmitService
from ctf_downloader.submitter import FlagSubmitter
from ctf_downloader.utils.logger import Logger, console


# ------------------------------------------------------------------ #
# legacy submit (nguyên văn submit.py)                                #
# ------------------------------------------------------------------ #

def _legacy_submit_parse_args():
    parser = argparse.ArgumentParser(
        description="🚩 Automated CTF Flag Submitter 🚩",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Submit by Challenge Name:
  python submit.py -u https://ctf.example.com -c "session=xxx" --name "Tiger Bạc" -f "PTITCTF{flag_here}"

  # Submit by Challenge ID:
  python submit.py -u https://ctf.example.com -c "session=xxx" --id 18 -f "PTITCTF{flag_here}"

  # Auto-submit all solved flags in workspace directory:
  python submit.py -w ./PTIT_CTF_2026 -c "session=xxx" --auto

  # Interactive submission wizard:
  python submit.py -i
        """
    )

    parser.add_argument("-u", "--url", type=str, help="Target CTF platform URL")
    parser.add_argument("-c", "--cookie", type=str, help="Cookie string or path to cookie file")
    parser.add_argument("-t", "--token", type=str, help="API token or Bearer token")
    parser.add_argument("-w", "--workspace", type=str, default="./ctf_challenges", help="Local CTF workspace directory")
    parser.add_argument("--id", type=int, help="Target challenge ID")
    parser.add_argument("-n", "--name", type=str, help="Target challenge name")
    parser.add_argument("-f", "--flag", type=str, help="Flag string to submit")
    parser.add_argument("--flag-format", dest="flag_format", type=str, help="Regex định dạng flag của giải (vd: \"^PTITCTF\\\\{.+\\\\}$\")")
    parser.add_argument("--force", action="store_true", help="Vượt blacklist flag sai để vẫn submit")
    parser.add_argument("--auto", action="store_true", help="Auto-scan workspace for filled flags and submit them")
    parser.add_argument("-i", "--interactive", action="store_true", help="Launch interactive guided prompt")

    return parser.parse_args()


def _submit_interactive_wizard(flag_format: str = None):
    from rich.prompt import Prompt

    Logger.banner()
    console.print("[bold yellow]🚩 Interactive Flag Submitter[/bold yellow]\n")

    # Look for existing workspace
    default_workspace = os.path.expanduser("~/Workspace/CTF/PTIT_CTF_2026")
    if not os.path.exists(default_workspace):
        default_workspace = "./PTIT_CTF_2026" if os.path.exists("./PTIT_CTF_2026") else os.path.expanduser("~/Workspace/CTF")
    workspace = Prompt.ask("[bold cyan]Workspace directory (or press enter to skip)[/bold cyan]", default=default_workspace).strip()

    # Try reading URL from challenges.json if available
    default_url = ""
    if os.path.exists(os.path.join(workspace, "challenges.json")):
        try:
            with open(os.path.join(workspace, "challenges.json"), "r") as f:
                data = json.load(f)
                default_url = data.get("ctf_info", {}).get("url", "")
        except Exception:
            pass

    url = Prompt.ask("[bold cyan]Enter CTF Platform URL[/bold cyan]", default=default_url or "https://jeo.infosecptit.org/games/6/challenges").strip()
    cookie = Prompt.ask("[bold cyan]Paste Cookie (or path to cookie file)[/bold cyan]").strip()
    if os.path.isfile(cookie):
        with open(cookie, "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    svc = SubmitService(url=url, cookie=cookie, workspace_dir=workspace, flag_format=flag_format)

    console.print("\n[dim]Choose Action:[/dim]")
    console.print(" [bold green]1[/bold green]. Submit flag for a specific challenge")
    console.print(" [bold green]2[/bold green]. Auto-scan workspace and submit all filled flags")

    choice = Prompt.ask("[bold cyan]Select action[/bold cyan]", choices=["1", "2"], default="1")

    if choice == "1":
        chall_input = Prompt.ask("[bold cyan]Enter Challenge Name or ID[/bold cyan]").strip()
        flag_input = Prompt.ask("[bold cyan]Enter Flag string[/bold cyan]").strip()
        svc.submit(chall_input, flag_input)
    else:
        svc.auto_scan_and_submit()


def legacy_submit_main():
    args = _legacy_submit_parse_args()

    if args.interactive or (not args.flag and not args.auto and not args.url):
        _submit_interactive_wizard(flag_format=args.flag_format)
        return

    # Check url from workspace if not provided
    url = args.url
    if not url and args.workspace and os.path.exists(os.path.join(args.workspace, "challenges.json")):
        try:
            with open(os.path.join(args.workspace, "challenges.json"), "r") as f:
                data = json.load(f)
                url = data.get("ctf_info", {}).get("url", "")
        except Exception:
            pass

    if not url:
        Logger.error("CTF URL is required. Use -u <URL> or -i for interactive mode.")
        sys.exit(1)

    cookie = args.cookie
    if cookie and os.path.isfile(cookie):
        with open(cookie, "r", encoding="utf-8") as f:
            cookie = f.read().strip()

    svc = SubmitService(
        url=url,
        cookie=cookie,
        token=args.token,
        workspace_dir=args.workspace,
        flag_format=args.flag_format
    )

    if args.auto:
        svc.auto_scan_and_submit(force=args.force)
    elif args.flag:
        chall = args.id if args.id is not None else args.name
        if not chall:
            Logger.error("Please specify target challenge with --id <ID> or --name <NAME>.")
            sys.exit(1)
        succ, msg = svc.submit(chall, args.flag, force=args.force)
        if not succ:
            sys.exit(1)
    else:
        Logger.error("Please provide a flag to submit with -f <FLAG> or use --auto.")
        sys.exit(1)


# ------------------------------------------------------------------ #
# legacy manage (nguyên văn manage.py)                                #
# ------------------------------------------------------------------ #

def _manage_interactive_mode(dash, workspace, cookie, token):
    challs = dash.local_challenges
    if not challs:
        Logger.warning('No challenges found in workspace.')
        return

    while True:
        print("\n" + '='*75)
        print(' 🛠️  CTF CHALLENGE MANAGER & EXPLORER')
        print('='*75)
        print('  [1] Show Complete Challenge Tree & Status')
        print('  [2] View Challenge Details & Description')
        print('  [3] Launch / Manage Dynamic Container Instance')
        print('  [4] Submit Flag')
        print('  [5] Mark Challenge as Solved / Unsolved')
        print('  [0] Exit')
        print('='*75)
        choice = input('Select option (0-5): ').strip()

        if choice == '0':
            break
        elif choice == '1':
            dash.render_tree()
        elif choice == '2':
            q = input('Enter Challenge ID or Name: ').strip()
            target = next((c for c in challs if str(c.get('id')) == q or q.lower() in c.get('name', '').lower()), None)
            if not target:
                Logger.error('Challenge not found.')
                continue
            print("\n" + '-'*60)
            print(f"📌 Challenge: {target.get('name')} (ID: {target.get('id')})")
            print(f"Category: {target.get('category')} | Points: {target.get('points')} | Solves: {target.get('solves_count', '-')}")
            if target.get('connection_info'):
                print(f"Connection: {target.get('connection_info')}")
            readme_p = os.path.join(target.get('_folder', ''), 'README.md')
            if os.path.exists(readme_p):
                print(f"\n--- Description from {os.path.basename(readme_p)} ---")
                with open(readme_p, 'r', encoding='utf-8') as rf:
                    print(rf.read()[:1000])
            print('-'*60)
        elif choice == '3':
            try:
                mgr = InstanceManager(workspace, cookie=cookie, token=token)
                q = input('Enter Challenge ID or Name: ').strip()
                target = mgr.find_challenge(challenge_id=q, challenge_name=q)
                if not target:
                    Logger.error('Challenge not found.')
                    continue
                cid = target.get('id')
                print(f"\nActions for {target.get('name')} (ID: {cid}):")
                print('  [1] Start / Renew Container')
                print('  [2] Check Container Status')
                print('  [3] Extend Container Lifetime')
                print('  [4] Stop / Destroy Container')
                cact = input('Choice (1-4): ').strip()
                if cact == '1':
                    mgr.start_instance(cid)
                elif cact == '2':
                    st = mgr.get_status(cid)
                    Logger.info(f'Status: {st}')
                elif cact == '3':
                    mgr.extend_instance(cid)
                elif cact == '4':
                    mgr.stop_instance(cid)
            except Exception as e:
                Logger.error(f'Instance error: {e}')
        elif choice == '4':
            q = input('Enter Challenge ID or Name: ').strip()
            flag_str = input('Enter Flag: ').strip()
            if not flag_str:
                Logger.warning('Empty flag.')
                continue
            try:
                sub = FlagSubmitter(workspace_dir=workspace, cookie=cookie, token=token)
                sub.submit_single_flag(challenge_id=q if q.isdigit() else None, challenge_name=q if not q.isdigit() else None, flag_value=flag_str)
            except Exception as e:
                Logger.error(f'Submit error: {e}')
        elif choice == '5':
            q = input('Enter Challenge ID or Name: ').strip()
            target = next((c for c in challs if str(c.get('id')) == q or q.lower() in c.get('name', '').lower()), None)
            if not target:
                Logger.error('Challenge not found.')
                continue
            folder = target.get('_folder')
            readme_p = os.path.join(folder, 'README.md')
            if os.path.exists(readme_p):
                with open(readme_p, 'r', encoding='utf-8') as rf:
                    rtxt = rf.read()
                if '- [ ] Solved' in rtxt:
                    new_txt = rtxt.replace('- [ ] Solved', '- [x] Solved')
                    Logger.success(f"Marked {target.get('name')} as SOLVED ✅")
                else:
                    new_txt = rtxt.replace('- [x] Solved', '- [ ] Solved').replace('- [X] Solved', '- [ ] Solved')
                    Logger.info(f"Marked {target.get('name')} as UNSOLVED ⏳")
                with open(readme_p, 'w', encoding='utf-8') as wf:
                    wf.write(new_txt)
                dash = CTFDashboard(workspace)


def legacy_manage_main():
    parser = argparse.ArgumentParser(
        description='📊 CTF Workspace Challenge Manager & Dashboard 📊',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-w', '--workspace', default='.', help='Local CTF workspace directory')
    parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    parser.add_argument('-t', '--token', help='API token or Bearer token')
    parser.add_argument('-u', '--unsolved', action='store_true', help='Show only unsolved challenges')
    parser.add_argument('-s', '--solved', action='store_true', help='Show only solved challenges')
    parser.add_argument('-C', '--category', nargs='+', help='Filter specific categories (e.g. -C Web Pwn)')
    parser.add_argument('--container', action='store_true', help='Filter only dynamic container challenges')
    parser.add_argument('-A', '--all', action='store_true', help='Scan and display all CTF workspaces in ~/Workspace/CTF/')
    parser.add_argument('-i', '--interactive', action='store_true', help='Launch interactive challenge manager wizard')

    args = parser.parse_args()

    if args.all:
        default_ctf_dir = os.path.expanduser('~/Workspace/CTF')
        StatusService.scan_all_workspaces(default_ctf_dir)
        return

    workspace = os.path.abspath(args.workspace)
    dash = CTFDashboard(workspace)

    if args.interactive:
        _manage_interactive_mode(dash, workspace, args.cookie, args.token)
        return

    dash.render_tree(
        filter_cat=args.category,
        only_unsolved=args.unsolved,
        only_solved=args.solved,
        only_container=args.container
    )


# ------------------------------------------------------------------ #
# legacy instance (nguyên văn instance.py; --sync ->                  #
# InstanceService.sync_containers)                                    #
# ------------------------------------------------------------------ #

def legacy_instance_main():
    parser = argparse.ArgumentParser(
        description='🐳 CTF Dynamic Container / Instance Manager 🐳',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument('-w', '--workspace', default='.', help='Local CTF workspace directory')
    parser.add_argument('-c', '--cookie', help='Cookie string or path to cookie file')
    parser.add_argument('-t', '--token', help='API token or Bearer token')
    parser.add_argument('--id', help='Target challenge ID')
    parser.add_argument('-n', '--name', help='Target challenge name')

    action_group = parser.add_mutually_exclusive_group()
    action_group.add_argument('--start', action='store_true', help='Start or renew dynamic container instance')
    action_group.add_argument('--stop', action='store_true', help='Stop / destroy active container instance')
    action_group.add_argument('--extend', action='store_true', help='Extend container expiration countdown')
    action_group.add_argument('--status', action='store_true', help='Check status and active entry of container')
    action_group.add_argument('--sync', action='store_true', help='Scan and synchronize all container statuses in workspace')
    action_group.add_argument('-l', '--list', action='store_true', help='List all container challenges in workspace')
    action_group.add_argument('-i', '--interactive', action='store_true', help='Launch interactive container manager')

    args = parser.parse_args()

    cookie_val, token_val = AuthService.resolve(args.workspace, cookie_arg=args.cookie, token_arg=args.token)

    try:
        svc = InstanceService(args.workspace, cookie=cookie_val, token=token_val)
    except Exception as e:
        Logger.error(f'Initialization error: {e}')
        sys.exit(1)

    # 1. Sync all containers action
    if args.sync:
        svc.sync_containers()
        return

    # 1. List action
    if args.list:
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
            print(f"{str(c.get('id')):<8} | {c.get('category', 'Misc'):<12} | {c.get('name', 'Unknown')[:30]:<30} | {str(solves):<8}")
        print('='*75)
        return

    # 2. Interactive mode — wizard nằm ở InstanceService.interactive_pick
    if args.interactive:
        svc.interactive_pick()
        return

    # 3. Direct actions by ID / Name
    chall_id = None
    if args.id or args.name:
        target_chall = svc.find_challenge(challenge_id=args.id, challenge_name=args.name)
        if not target_chall:
            Logger.error(f'Challenge not found in workspace for ID={args.id}, Name={args.name}')
            sys.exit(1)
        chall_id = target_chall.get('id')
    else:
        parser.print_help()
        sys.exit(1)

    if args.start:
        svc.start_instance(chall_id)
    elif args.stop:
        svc.stop_instance(chall_id)
    elif args.extend:
        svc.extend_instance(chall_id)
    elif args.status:
        st = svc.get_status(chall_id)
        Logger.info(f'Status for ID {chall_id}:')
        for k, v in st.items():
            print(f'  {k}: {v}')
    else:
        svc.start_instance(chall_id)


# ------------------------------------------------------------------ #
# legacy rank (nguyên văn rank.py)                                    #
# ------------------------------------------------------------------ #

def legacy_rank_main():
    parser = argparse.ArgumentParser(
        description="🏆 CTF Scoreboard & Live Ranking Manager 🏆",
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("-w", "--workspace", default=".", help="CTF workspace directory (default: current dir)")
    parser.add_argument("-u", "--url", help="Platform base URL (if not using workspace)")
    parser.add_argument("-c", "--cookie", help="Cookie string or path to cookie file")
    parser.add_argument("-t", "--token", help="API token or Bearer token")
    parser.add_argument("-n", "--top", type=int, default=15, help="Number of top teams to display (default: 15)")
    parser.add_argument("--no-docs", action="store_true", help="Do not write/update RANKING.md or SUMMARY.md")

    args = parser.parse_args()

    cookie_val, token_val = AuthService.resolve(args.workspace, cookie_arg=args.cookie, token_arg=args.token)

    try:
        svc = RankService(
            workspace_path=args.workspace,
            url=args.url,
            cookie=cookie_val,
            token=token_val
        )
        svc.display_and_update(top_n=args.top, update_docs=not args.no_docs)
    except Exception as e:
        Logger.error(f"Error fetching ranking: {e}")
        sys.exit(1)
