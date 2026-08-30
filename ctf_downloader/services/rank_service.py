"""RankService — hiển thị + cập nhật bảng xếp hạng live (body cũ của
ranking.RankingManager). RankingManager trong ``ctf_downloader.ranking`` giờ
chỉ là facade mỏng delegate về đây.
"""
import datetime
import os
import re
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
from ..utils.sanitize import md_cell, strip_ansi

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


def _score_int(value) -> int:
    """[hunt-c18 LOW] Ép điểm do server trả về về int một cách an toàn.

    Scoreboard JSON có thể mang score dạng chuỗi ("300", "12.9") — đưa
    NGUYÊN vào max()/phép trừ thì so lexicographic ("9" > "1000") hoặc
    TypeError ngay chỗ tính gap. Chuỗi số nguyên parse trực tiếp; số thập
    phân cắt cụt về int; mọi thứ không parse được (None/"abc"/inf) fallback
    0 thay vì làm nổ cả panel."""
    if value is None:
        return 0
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        pass
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


#: Run backtick trong dữ liệu server — dùng tính delimiter code-span.
_MD_BACKTICK_RUN_RE = re.compile(r"`+")


def _md_code_span(text) -> str:
    """[review-c18-2 LOW] Bọc giá trị ĐÃ sanitize (qua :func:`md_cell`) vào
    MỘT code-span GFM không vỡ khi text chứa backtick — tên team
    ``we`rt`eam`` từng đóng span sớm tại `` `rt` `` làm hỏng dòng bullet
    RANKING.md / badge SUMMARY.md.

    CommonMark: delimiter N backtick chỉ bị đóng bởi run >= N backtick ->
    chọn N = run dài nhất trong text + 1; khi mép là backtick, pad một
    space hai bên (CommonMark strip đúng một space mỗi mép khi render).
    Text sạch giữ nguyên dạng `` `text` `` như trước (byte-identical với
    template legacy — test_rank_repo chốt bytes)."""
    text = str(text)
    longest = max((len(m.group(0))
                   for m in _MD_BACKTICK_RUN_RE.finditer(text)), default=0)
    delim = "`" * (longest + 1)
    pad = " " if (text.startswith("`") or text.endswith("`")) else ""
    return f"{delim}{pad}{text}{pad}{delim}"


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

        self.session = create_session(
            cookie=self.cookie, token=self.token, timeout=self.timeout,
            base_url=self.url,
        )
        self.platform = PlatformDetector.detect_platform(self.url, self.session)

    def _resolve_url(self) -> Optional[str]:
        if not self.workspace_path or not os.path.exists(self.workspace_path):
            return None
        if self.repo is None:
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
            data = fetcher() or {}
            if not isinstance(data, dict):
                raise TypeError(
                    f"fetch_scoreboard() trả {type(data).__name__}, cần dict"
                )
            # Watch-mode adapters intentionally normalize transport/HTTP
            # failures into metadata fields so one bad tick does not crash the
            # loop. Rank is a one-shot command and has NO prior snapshot, so
            # those same responses must surface as errors rather than being
            # mislabeled "scoreboard chưa có standings".
            if data.get("_error"):
                raise RuntimeError(
                    f"scoreboard transport: {data.get('_error')}"
                )
            status = data.get("_http_status")
            try:
                status = int(status) if status is not None else None
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"scoreboard trả HTTP status không hợp lệ: {status!r}"
                )
            if status == 304 or data.get("_not_modified"):
                raise RuntimeError(
                    "scoreboard trả 304 nhưng lệnh rank không có snapshot trước để dùng"
                )
            if status == 429:
                retry_after = data.get("_retry_after")
                detail = (
                    f"; Retry-After={retry_after}"
                    if retry_after not in (None, "") else ""
                )
                raise RuntimeError(f"scoreboard HTTP 429{detail}")
            if status is not None and status >= 400:
                raise RuntimeError(f"scoreboard HTTP {status}")
            return data
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
        # Hunter-c14 BUG-C14-1: title/tên team do SERVER kiểm soát — strip
        # ANSI/control trước khi bọc Text() để ESC không đi nguyên vào
        # terminal (terminal injection: đổi màu/clear screen/OSC title).
        title = strip_ansi(data.get("title")) or "CTF Competition"
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

            # Hunter-c14 BUG-C14-7: standings có thể chưa sort từ server —
            # top_score lấy MAX thay vì standings[0], cộng clamp tại nguồn
            # để gap luôn >= 0 (không bao giờ in '--N pts'); footer phía
            # dưới đã clamp theo cùng cách.
            # Hunt-c18 LOW: score ép int an toàn trước khi max/trừ.
            top_score = max((_score_int(s.get("score")) for s in standings),
                            default=0)
            for idx, s in enumerate(standings[:top_n], 1):
                # Review c18-2 (MED): ``pos`` CŨNG do server trả — vào Rich
                # Table NGUYÊN thì OSC/CSI injection đi thẳng terminal
                # (``pos="\x1b]0;pwned\x07\x1b[31m9"`` đổi title/màu), cùng
                # họ BUG-C14-1 với title/name/my_rank đã strip ở footer.
                pos = strip_ansi(s.get("pos")) or idx
                name = strip_ansi(s.get("name") or "") or "Unknown"
                score = _score_int(s.get("score"))

                gap_pts = max(0, top_score - score)
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
                # Hunt-c18 (cùng họ C14-1): rank/total cũng do server kiểm
                # soát — strip control trước khi vào Text footer.
                rank_str = (f"rank {strip_ansi(my_rank)}/"
                            f"{strip_ansi(total_teams)}" if total_teams
                            else f"rank {strip_ansi(my_rank)}")
                footer_parts.append(rank_str)
            if my_score is not None:
                gap_pts = max(0, top_score - _score_int(my_score))
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

    def _save_ranking_docs(self, data: Dict[str, Any]):
        if self.repo is None or not self.workspace_path:
            raise RuntimeError("Không có workspace repo để ghi ranking docs.")
        repo = self.repo
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # Hunter-c14 BUG-C14-2: mọi giá trị server-control nhúng vào
        # RANKING.md đi qua md_cell — '|' sinh cột ảo, newline sinh hàng
        # bảng giả, ESC làm bẩn file. So sánh is_me dùng giá trị THÔ.
        title = md_cell(data.get("title")) or "CTF Competition"
        my_team = data.get("my_team") or "-"
        my_user = data.get("my_user") or "-"
        total_teams = data.get("total_teams") or len(data.get("standings", []))
        standings = data.get("standings", [])
        # Hunt-c18 BUG-1 (MED): my_rank/my_score/pos/score CŨNG là dữ liệu
        # server-control — trước đây nhúng NGUYÊN vào dòng bullet và ô bảng
        # (newline tách dòng, pipe vỡ bảng). Giữ biến raw để is_me/badge so
        # sánh đúng; mọi giá trị in ra đi qua md_cell NGAY TẠI SINK.
        raw_rank = data.get("my_rank")
        raw_score = data.get("my_score")
        # Review c18-2 (LOW): mọi giá trị nhúng vào CODE-SPAN đi qua
        # _md_code_span — md_cell không xử lý backtick, tên team chứa `` ` ``
        # đóng span sớm vỡ bullet/badge. Ô bảng (không nằm trong code-span)
        # giữ md_cell như trước.
        rank_cell = md_cell(raw_rank) or "-"
        points_cell = md_cell(raw_score) if raw_score is not None else "-"
        teams_cell = md_cell(total_teams)
        team_cell = md_cell(my_team)
        user_cell = md_cell(my_user)

        # 1. Write RANKING.md — qua WorkspaceRepo (atomic + flock), KHÔNG
        #    open() thô (spec-audit: mọi writer state đi qua storage layer).
        lines = [
            f"# 🏆 Live Ranking & Scoreboard: {title}\n",
            f"- **Last Updated**: `{now_str}`",
            f"- **Team**: {_md_code_span(team_cell)}",
            f"- **User**: {_md_code_span(user_cell)}",
            f"- **Current Rank**: {_md_code_span('#' + rank_cell)} / "
            f"{_md_code_span(teams_cell + ' teams')}",
            f"- **Total Points**: {_md_code_span(points_cell + ' pts')}\n",
            "## 📊 Top Standings\n",
            "| Rank | Team / Player | Points |",
            "| :---: | :--- | :---: |"
        ]

        for idx, s in enumerate(standings[:30], 1):
            pos_cell = md_cell(s.get("pos") or idx)
            name_raw = s.get("name") or "Unknown"
            row_score = s.get("score")
            score_cell = md_cell(0 if row_score is None else row_score)
            is_me = ((my_team and name_raw == my_team)
                     or (my_user and name_raw == my_user))
            if is_me:
                lines.append(f"| **#{pos_cell}** | "
                             f"**{md_cell(name_raw)} (You)** 🎯 | "
                             f"**{score_cell}** |")
            else:
                lines.append(f"| #{pos_cell} | {md_cell(name_raw)} | "
                             f"{score_cell} |")

        lines.append("")
        repo.write_ranking_md("\n".join(lines))
        ranking_md_path = str(repo.ranking_md_path)

        Logger.info(f"Đã cập nhật bảng xếp hạng live: [path]{os.path.relpath(ranking_md_path, self.workspace_path)}[/path]", markup=True)

        # 2. Update SUMMARY.md via WorkspaceRepo (chèn/thay dòng Live Rank)
        # Review-5 (M, follow-up BUG-C14-2): badge được patch_summary_live_rank
        # chèn NGUYÊN VĂN vào SUMMARY.md nên mọi giá trị server-control trên
        # dòng này phải qua md_cell như đường RANKING.md — ANSI/newline/pipe/
        # ngoặc vuông từ tên team không được lọt qua đường badge. Với dữ
        # liệu hợp lệ (số nguyên/tên sạch) md_cell là no-op.
        rank_badge = (
            f"{LIVE_RANK_PREFIX} {_md_code_span('#' + rank_cell)}"
            f" / {_md_code_span(teams_cell)}"
            f" (Team: {_md_code_span(team_cell)})"
        )
        repo.patch_summary_live_rank(rank_badge)
