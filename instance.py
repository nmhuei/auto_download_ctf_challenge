#!/usr/bin/env python3
import os
import sys
import argparse
from ctf_downloader.instance_manager import InstanceManager
from ctf_downloader.utils.logger import Logger

def main():
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
    action_group.add_argument('-l', '--list', action='store_true', help='List all container challenges in workspace')
    action_group.add_argument('-i', '--interactive', action='store_true', help='Launch interactive container manager')

    args = parser.parse_args()

    cookie_val = args.cookie
    if cookie_val and os.path.isfile(cookie_val):
        with open(cookie_val, 'r', encoding='utf-8') as f:
            cookie_val = f.read().strip()

    try:
        mgr = InstanceManager(args.workspace, cookie=cookie_val, token=args.token)
    except Exception as e:
        Logger.error(f'Initialization error: {e}')
        sys.exit(1)

    # 1. List action
    if args.list:
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
            print(f"{str(c.get('id')):<8} | {c.get('category', 'Misc'):<12} | {c.get('name', 'Unknown')[:30]:<30} | {str(solves):<8}")
        print('='*75)
        return

    # 2. Interactive mode
    if args.interactive:
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

    # 3. Direct actions by ID / Name
    target_chall = None
    if args.id or args.name:
        target_chall = mgr.find_challenge(challenge_id=args.id, challenge_name=args.name)
        if not target_chall:
            Logger.error(f'Challenge not found in workspace for ID={args.id}, Name={args.name}')
            sys.exit(1)
        chall_id = target_chall.get('id')
    else:
        if not args.list:
            parser.print_help()
            sys.exit(1)

    if args.start:
        mgr.start_instance(chall_id)
    elif args.stop:
        mgr.stop_instance(chall_id)
    elif args.extend:
        mgr.extend_instance(chall_id)
    elif args.status:
        st = mgr.get_status(chall_id)
        Logger.info(f'Status for ID {chall_id}:')
        for k, v in st.items():
            print(f'  {k}: {v}')
    else:
        mgr.start_instance(chall_id)

if __name__ == '__main__':
    main()
