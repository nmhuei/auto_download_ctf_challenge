"""RankService — hiển thị + cập nhật bảng xếp hạng live (body cũ của
ranking.RankingManager). RankingManager trong ``ctf_downloader.ranking`` giờ
chỉ là facade mỏng delegate về đây.
"""
import datetime
import os
from typing import Any, Dict, Optional

from rich import box
from rich.console import Console, Group
from rich.table import Table
from rich.panel import Panel
from rich.text import Text

from ..platforms.detector import PlatformDetector
from ..services.session_factory import create_session
from ..storage.constants import LIVE_RANK_PREFIX
from ..storage.workspace_repo import WorkspaceRepo
from ..ui.diagnostics import Diagnostic, render as render_diagnostic
from ..ui.theme import ACCENT_DEEP, FG_BASE, load_theme
from ..utils.logger import Logger

#: Console riêng với theme PHOSPHOR FIELD KIT (spec §3) — các token
#: ``fg.base`` / ``accent.deep`` … chỉ resolve trên console có load_theme().
_rank_console = Console(theme=load_theme(None))

#: Glyph vị trí top-3 thay huy chương emoji (spec §6: không emoji).
_TOP3_GLYPH = "◆"

#: Chuỗi ``pos`` thuộc top-3 (platform có thể trả "1" lẫn "1st").
_POS_TOP3 = ("1", "1st", "2", "2nd", "3", "3rd")

#: Hint dùng chung cho vấn đề xác thực khi lấy scoreboard.
_COOKIE_HINTS = (
    "kiểm tra cookie/token còn hạn (lấy lại từ trình duyệt)",
)

#: Hint cho nền tảng không có bảng xếp hạng công khai.
_NO_SCOREBOARD_HINT = "platform này chưa có scoreboard công khai"


class RankService:
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

    def _render_auth_warning(self) -> None:
        """Diagnostic cảnh báo xác thực thất bại (pipeline vẫn chạy —
        scoreboard công khai vẫn lấy được, dữ liệu riêng của team thì không)."""
        render_diagnostic(Diagnostic(
            "warning",
            "Xác thực thất bại — scoreboard có thể bị ẩn hoặc rút gọn với khách",
            hints=_COOKIE_HINTS,
        ))

    def fetch_ranking(self) -> Dict[str, Any]:
        Logger.info("Đang tải bảng xếp hạng live từ platform...")
        try:
            auth_ok = self.platform.authenticate()
        except Exception:
            auth_ok = None   # giữ hành vi cũ: lỗi authenticate không chặn flow
        if auth_ok is False:
            self._render_auth_warning()

        fetcher = getattr(self.platform, "fetch_scoreboard", None)
        if not callable(fetcher):
            render_diagnostic(Diagnostic(
                "error",
                "Nền tảng không hỗ trợ bảng xếp hạng",
                cause=f"platform {type(self.platform).__name__} thiếu "
                      f"fetch_scoreboard()",
                hints=(_NO_SCOREBOARD_HINT,),
            ))
            raise AttributeError(
                f"{type(self.platform).__name__} does not support 'fetch_scoreboard'")

        # Capability nói rõ không có scoreboard → báo trước rồi đi tiếp như cũ
        # (base.fetch_scoreboard trả standings rỗng → panel "chưa có dữ liệu").
        # Nguồn đúng là PlatformInfo do detector gắn lên platform (``.info``).
        caps = getattr(getattr(self.platform, "info", None),
                       "capabilities", None) or {}
        if caps.get("scoreboard") is False:
            render_diagnostic(Diagnostic(
                "warning",
                "Nền tảng này không hỗ trợ scoreboard công khai",
                hints=(_NO_SCOREBOARD_HINT,
                       *_COOKIE_HINTS),
            ))

        try:
            return fetcher()
        except Exception as exc:
            render_diagnostic(Diagnostic(
                "error",
                "Lấy scoreboard thất bại",
                cause=f"{type(exc).__name__}: {exc}" if str(exc)
                      else type(exc).__name__,
                hints=("kiểm tra kết nối mạng đến platform",
                       *_COOKIE_HINTS),
            ))
            raise

    def display_and_update(self, top_n: int = 15, update_docs: bool = True) -> Dict[str, Any]:
        data = self.fetch_ranking()

        _rank_console.print(self._render_scoreboard(data, top_n=top_n))

        if not data.get("standings"):
            Logger.warning("Platform chưa có dữ liệu standings trên scoreboard.")
            return data

        if update_docs and self.workspace_path and os.path.exists(self.workspace_path):
            self._save_ranking_docs(data)

        return data

    @staticmethod
    def _render_scoreboard(data: Dict[str, Any], top_n: int = 15) -> Panel:
        """Render scoreboard PHOSPHOR FIELD KIT thành một :class:`Panel`:

        - Panel rounded viền ``accent.deep``, heading UPPERCASE faint
          ``BẢNG XẾP HẠNG · <giải>``.
        - Bảng Top-N ``box=None``: vị trí accent amber (top-3 thêm glyph
          ``◆`` thay huy chương emoji), tên ``fg.base`` — bold khi là team
          mình, điểm right-align, gap muted; hàng mình nền chip subtle
          ``on accent.deep``.
        - Footer tóm tắt ``rank X/Y · gap N pts`` muted.
        """
        title = data.get("title") or "CTF Competition"
        my_team = data.get("my_team")
        my_user = data.get("my_user")
        my_rank = data.get("my_rank")
        my_score = data.get("my_score")
        standings = data.get("standings", [])
        total_teams = data.get("total_teams") or len(standings)

        heading = Text(f" BẢNG XẾP HẠNG · {title} ".upper(), style="fg.faint")

        if not standings:
            body: Any = Text("chưa có dữ liệu", style="fg.faint")
        else:
            table = Table(box=None, show_header=True,
                          header_style="fg.faint", padding=(0, 2))
            table.add_column("#", justify="right", style="accent",
                             no_wrap=True)
            table.add_column("TEAM / USER", justify="left", min_width=25,
                             no_wrap=True)
            table.add_column("SCORE", justify="right", style="fg.base",
                             no_wrap=True)
            table.add_column("GAP", justify="right", style="fg.muted",
                             no_wrap=True)

            top_score = standings[0].get("score") or 0
            for idx, s in enumerate(standings[:top_n], 1):
                pos = s.get("pos") or idx
                name = s.get("name") or "Unknown"
                score = s.get("score") or 0

                gap_pts = top_score - score
                gap_str = "-" if gap_pts == 0 else f"-{gap_pts} pts"

                is_me = ((my_team and name == my_team)
                         or (my_user and name == my_user))
                pos_str = (f"{_TOP3_GLYPH} {pos}" if str(pos) in _POS_TOP3
                           else str(pos))
                name_cell = Text(
                    str(name),
                    style=f"bold {FG_BASE}" if is_me else "fg.base")

                table.add_row(
                    pos_str,
                    name_cell,
                    str(score),
                    gap_str,
                    style=f"on {ACCENT_DEEP}" if is_me else None,
                )

            footer_parts = []
            if my_rank:
                rank_str = (f"rank {my_rank}/{total_teams}" if total_teams
                            else f"rank {my_rank}")
                footer_parts.append(rank_str)
            if my_score is not None:
                gap_pts = max(0, top_score - my_score)
                footer_parts.append(f"gap {gap_pts} pts")
            body = (Group(table, Text(" · ".join(footer_parts),
                                      style="fg.muted"))
                    if footer_parts else table)

        return Panel(
            body,
            box=box.ROUNDED,
            border_style="accent.deep",
            title=heading,
            expand=False,
            padding=(0, 1),
        )

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

        Logger.info(f"Đã cập nhật bảng xếp hạng live: [bold cyan]{os.path.relpath(ranking_md_path, self.workspace_path)}[/bold cyan]")

        # 2. Update SUMMARY.md via WorkspaceRepo (chèn/thay dòng Live Rank)
        rank_badge = f"{LIVE_RANK_PREFIX} `#{my_rank}` / `{total_teams}` (Team: `{my_team}`)"
        self.repo.patch_summary_live_rank(rank_badge)
