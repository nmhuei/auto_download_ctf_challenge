#!/usr/bin/env python3
import os
import sys
import argparse
from ctf_downloader.dashboard import CTFDashboard
from ctf_downloader.instance_manager import InstanceManager
from ctf_downloader.submitter import FlagSubmitter
from ctf_downloader.utils.logger import Logger

def scan_all_workspaces(base_dir):
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

def interactive_mode(dash, workspace, cookie, token):
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

def main():
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
        scan_all_workspaces(default_ctf_dir)
        return

    workspace = os.path.abspath(args.workspace)
    dash = CTFDashboard(workspace)

    if args.interactive:
        interactive_mode(dash, workspace, args.cookie, args.token)
        return

    dash.render_tree(
        filter_cat=args.category,
        only_unsolved=args.unsolved,
        only_solved=args.solved,
        only_container=args.container
    )

if __name__ == '__main__':
    main()
