#!/usr/bin/env python3
"""
CTF Automated Flag Submitter
Submit flags to CTFd, GZCTF, or rCTF platforms automatically from CLI or local workspace.
"""
import os
import sys
import json
import argparse
from rich.prompt import Prompt

from ctf_downloader.submitter import FlagSubmitter
from ctf_downloader.utils.logger import Logger, console

def parse_args():
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

def interactive_wizard(flag_format: str = None):
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

    submitter = FlagSubmitter(url=url, cookie=cookie, workspace_dir=workspace, flag_format=flag_format)

    console.print("\n[dim]Choose Action:[/dim]")
    console.print(" [bold green]1[/bold green]. Submit flag for a specific challenge")
    console.print(" [bold green]2[/bold green]. Auto-scan workspace and submit all filled flags")

    choice = Prompt.ask("[bold cyan]Select action[/bold cyan]", choices=["1", "2"], default="1")

    if choice == "1":
        chall_input = Prompt.ask("[bold cyan]Enter Challenge Name or ID[/bold cyan]").strip()
        flag_input = Prompt.ask("[bold cyan]Enter Flag string[/bold cyan]").strip()
        submitter.submit(chall_input, flag_input)
    else:
        submitter.auto_scan_and_submit()

def main():
    args = parse_args()

    if args.interactive or (not args.flag and not args.auto and not args.url):
        interactive_wizard(flag_format=args.flag_format)
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

    submitter = FlagSubmitter(
        url=url,
        cookie=cookie,
        token=args.token,
        workspace_dir=args.workspace,
        flag_format=args.flag_format
    )

    if args.auto:
        submitter.auto_scan_and_submit(force=args.force)
    elif args.flag:
        chall = args.id if args.id is not None else args.name
        if not chall:
            Logger.error("Please specify target challenge with --id <ID> or --name <NAME>.")
            sys.exit(1)
        succ, msg = submitter.submit(chall, args.flag, force=args.force)
        if not succ:
            sys.exit(1)
    else:
        Logger.error("Please provide a flag to submit with -f <FLAG> or use --auto.")
        sys.exit(1)

if __name__ == "__main__":
    main()
