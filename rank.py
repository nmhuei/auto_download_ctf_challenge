#!/usr/bin/env python3
import os
import sys
import argparse

from ctf_downloader.ranking import RankingManager
from ctf_downloader.cli import get_auth_for_workspace
from ctf_downloader.utils.logger import Logger

def main():
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
        Logger.error(f"Error fetching ranking: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
