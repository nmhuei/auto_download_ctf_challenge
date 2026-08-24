import os
import json
import datetime
from typing import Optional, Dict, Any, List

from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from .storage.constants import LIVE_RANK_PREFIX
from .storage.workspace_repo import WorkspaceRepo
from .utils.logger import Logger, console
from .services.session_factory import create_session
from .platforms.detector import PlatformDetector

class RankingManager:
    def __init__(
        self,
        workspace_path: Optional[str] = None,
        url: Optional[str] = None,
        cookie: Optional[str] = None,
        token: Optional[str] = None,
        timeout: int = 30
    ):
        self.workspace_path = os.path.abspath(workspace_path) if workspace_path else None
        self.cookie = cookie
        self.token = token
        self.timeout = timeout
        self.repo = WorkspaceRepo(self.workspace_path) if self.workspace_path else None
        self.url = url or self._resolve_url()

        if not self.url:
            raise ValueError("Platform URL not specified and could not be detected from workspace.")

        self.session = create_session(cookie=self.cookie, token=self.token, timeout=self.timeout)
        self.platform = PlatformDetector.detect_platform(self.url, self.session)

    def _resolve_url(self) -> Optional[str]:
        if not self.workspace_path or not os.path.exists(self.workspace_path):
            return None
        return self.repo.resolve_platform_url()

    def fetch_ranking(self) -> Dict[str, Any]:
        Logger.info("Fetching live leaderboard and ranking from CTF platform...")
        try:
            self.platform.authenticate()
        except Exception:
            pass
        return self.platform.fetch_scoreboard()

    def display_and_update(self, top_n: int = 15, update_docs: bool = True) -> Dict[str, Any]:
        data = self.fetch_ranking()
        title = data.get("title") or "CTF Competition"
        my_team = data.get("my_team")
        my_user = data.get("my_user")
        my_rank = data.get("my_rank")
        my_score = data.get("my_score")
        total_teams = data.get("total_teams") or len(data.get("standings", []))
        standings = data.get("standings", [])

        # Display Terminal Panel
        header_text = Text()
        header_text.append(f"🏆 {title}\n", style="bold yellow")
        if my_team:
            header_text.append("Team: ", style="bold white")
            header_text.append(f"{my_team}  ", style="bold cyan")
        if my_user:
            header_text.append("User: ", style="bold white")
            header_text.append(f"{my_user}  ", style="bold green")
        if my_rank:
            header_text.append("Rank: ", style="bold white")
            header_text.append(f"#{my_rank} ", style="bold magenta")
            if total_teams:
                header_text.append(f"/ {total_teams} teams  ", style="dim")
        if my_score is not None:
            header_text.append("Score: ", style="bold white")
            header_text.append(f"{my_score} pts", style="bold yellow")

        console.print(Panel(header_text, border_style="cyan", title="[bold]LIVE SCOREBOARD[/bold]"))

        if not standings:
            Logger.warning("No standings data available on the platform scoreboard.")
            return data

        # Leaderboard Table
        table = Table(title=f"Top {min(top_n, len(standings))} Leaderboard Standings", show_header=True, header_style="bold blue")
        table.add_column("Rank", justify="center", style="bold", width=8)
        table.add_column("Team / User", justify="left", style="white", min_width=25)
        table.add_column("Points", justify="right", style="bold yellow", width=12)
        table.add_column("Gap to #1", justify="right", style="dim", width=12)

        top_score = standings[0].get("score", 0) if standings else 0

        for idx, s in enumerate(standings[:top_n], 1):
            pos = s.get("pos") or idx
            name = s.get("name") or "Unknown"
            score = s.get("score") or 0
            
            if str(pos) in ["1", "1st"]:
                rank_str = "🥇 #1"
            elif str(pos) in ["2", "2nd"]:
                rank_str = "🥈 #2"
            elif str(pos) in ["3", "3rd"]:
                rank_str = "🥉 #3"
            else:
                rank_str = f"#{pos}"

            gap_pts = top_score - score
            gap_str = "-" if gap_pts == 0 else f"-{gap_pts} pts"

            is_me = (my_team and name == my_team) or (my_user and name == my_user)
            if is_me:
                table.add_row(
                    f"[bold cyan]👉 {rank_str}[/bold cyan]",
                    f"[bold cyan]{name} (You)[/bold cyan]",
                    f"[bold yellow]{score}[/bold yellow]",
                    f"[bold cyan]{gap_str}[/bold cyan]",
                    style="on grey23"
                )
            else:
                table.add_row(rank_str, name, str(score), gap_str)

        console.print(table)

        if update_docs and self.workspace_path and os.path.exists(self.workspace_path):
            self._save_ranking_docs(data)

        return data

    def _save_ranking_docs(self, data: Dict[str, Any]):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        title = data.get("title") or "CTF Competition"
        my_team = data.get("my_team") or "-"
        my_user = data.get("my_user") or "-"
        my_rank = data.get("my_rank") or "-"
        my_score = data.get("my_score") if data.get("my_score") is not None else "-"
        total_teams = data.get("total_teams") or len(data.get("standings", []))
        standings = data.get("standings", [])

        # 1. Write RANKING.md
        ranking_md_path = os.path.join(self.workspace_path, "RANKING.md")
        lines = [
            f"# 🏆 Live Ranking & Scoreboard: {title}\n",
            f"- **Last Updated**: `{now_str}`",
            f"- **Team**: `{my_team}`",
            f"- **User**: `{my_user}`",
            f"- **Current Rank**: `#{my_rank}` / `{total_teams} teams`",
            f"- **Total Points**: `{my_score} pts`\n",
            "## 📊 Top Standings\n",
            "| Rank | Team / Player | Points |",
            "| :---: | :--- | :---: |"
        ]

        for idx, s in enumerate(standings[:30], 1):
            pos = s.get("pos") or idx
            name = s.get("name") or "Unknown"
            score = s.get("score") or 0
            is_me = (my_team and name == my_team) or (my_user and name == my_user)
            if is_me:
                lines.append(f"| **#{pos}** | **{name} (You)** 🎯 | **{score}** |")
            else:
                lines.append(f"| #{pos} | {name} | {score} |")

        lines.append("")
        with open(ranking_md_path, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        Logger.info(f"Updated live ranking document: [bold cyan]{os.path.relpath(ranking_md_path, self.workspace_path)}[/bold cyan]")

        # 2. Update SUMMARY.md via WorkspaceRepo (chèn/thay dòng Live Rank)
        rank_badge = f"{LIVE_RANK_PREFIX} `#{my_rank}` / `{total_teams}` (Team: `{my_team}`)"
        self.repo.patch_summary_live_rank(rank_badge)
