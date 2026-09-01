"""StatusService — đọc trạng thái workspace & in tổng quan.

Hợp nhất logic cũ của ``dashboard.CTFDashboard`` (scan + stats + render tree)
và 3 bản sao scan-all-workspaces (cli.py / manage.py / interactive_menu.py)
thành một nơi DUY NHẤT. Các facade (CTFDashboard) và các entrypoint chỉ
delegate vào đây.
"""
import os
import re
import shutil
import sys
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple, Union

from rich import box
from rich.cells import cell_len
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..storage.constants import FLAG_PLACEHOLDER
from ..storage.workspace_repo import WorkspaceRepo, is_superseded
from ..platforms.registry import display_label
from ..ui.theme import FG_BASE, FG_MUTED, load_theme
from ..utils.logger import Logger
from ..utils.writeup_assessor import assess_writeup

#: Console render cho toàn bộ surface status/workspaces: ghi ra stdout (giữ
#: hợp đồng test redirect_stdout + pipe), mang theme semantic của ui.theme
#: (div_line / hi_fg / title / solved / unsolved ...) — KHÔNG hardcode màu
#: ngoài widget meter (widget nhận RGB thuần theo thiết kế btop).
#: Console resolve ``sys.stdout`` lúc in nên vẫn hoạt động dưới redirect_stdout.
_status_console = Console(theme=load_theme(None))

# Thứ bậc trục writeup cho nguyên tắc "chỉ nâng không hạ" khi áp heuristic
# (spec status-model §5: heuristic CHỈ ghi đè khi writeup_auto=True).
WRITEUP_RANK = {"none": 0, "skeleton": 1, "draft": 2, "complete": 3}

#: Sentinel cho kwarg chưa được truyền (khác None hợp lệ — flag_format=None
#: nghĩa là "đã tra challenges.json và KHÔNG có flag_format").
_UNSET = object()

# Tag/label hợp lệ: lowercase [a-z0-9-], dài tối đa 24 ký tự.
TAG_PATTERN = re.compile(r'^[a-z0-9-]{1,24}$')
TAG_MAX_LEN = 24

# Solve progress has its own multi-color semantic ramp. Other generic meters
# keep AMBER_RAMP in ui.widgets.
from ..ui.widgets import SOLVE_RAMP as _SOLVE_RAMP

# Glyph ngữ nghĩa thay emoji (spec §4.3): state / draft / container / file.
ROW_GLYPHS = {
    'solve': {
        'unsolved': ('·', 'fg.faint'),
        'working': ('◆', 'warn'),
        'solved_by_me': ('✔', 'solved'),
        'solved_by_team': ('✔', 'solved'),
        'solved_other': ('✔', 'solved'),
    },
    'draft_badge': '✎',
    'container_badge': '⛁',
    'file_badge': '⎘',
}

# ChallengeRow schema TOÀN MÀN (codex-r2 P1): một lưới duy nhất cho mọi
# category — id>3, name≤24 cell (truncate ellipsis, đếm theo cell_len nên
# emoji 2-cell như 🐧🪟 không lệch cột), pts>4, solves>3, badge cố định.
CHALLENGE_NAME_MAX_CELLS = 24

# Compact status overview: one responsive panel aligned with category frames.
# Activity/window data is appended only when it actually exists; no empty
# secondary panel or fake zero sparkline is rendered.
OVERVIEW_MAX_TOTAL_COLS = 88
OVERVIEW_MIN_TOTAL_COLS = 40


class ChallengeNotFoundError(Exception):
    """Không có challenge nào khớp identifier."""


class AmbiguousChallengeError(Exception):
    """Nhiều challenge khớp partial-match — KHÔNG chọn âm thầm.

    Thuộc tính ``matches`` là list metadata dict (kèm ``_meta_path``) để caller
    liệt kê bắt user nhập chính xác hơn.
    """

    def __init__(self, matches: List[dict]):
        self.matches = matches
        names = ", ".join(f"{m.get('id')}. {m.get('name')}" for m in matches)
        super().__init__(f"Ambiguous challenge — {len(matches)} matches: {names}")


def _utcnow():
    """"Bây giờ" aware UTC — hàm riêng để test có thể patch deterministically."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


class StatusService:
    # Giá trị trục solve được tính là "đã giải" cho thống kê tiến độ
    # (giải bởi mình / team / người khác đều ăn điểm trên bảng).
    SOLVED_VALUES = ("solved_by_me", "solved_by_team", "solved_other")

    @staticmethod
    def compute_status(repo: WorkspaceRepo, meta_path,
                       meta: Optional[dict] = None,
                       flag_format=_UNSET) -> Dict[str, Any]:
        """Trạng thái đa chiều đã normalize/migrate của một challenge.

        Ngoài read_status (normalize + migrate-on-read), hàm còn tự đánh giá
        trục ``writeup`` qua ``assess_writeup`` trên nội dung writeup trên đĩa:

        - Bỏ qua hoàn toàn khi ``status.writeup_auto == False`` (user set tay).
        - File đọc theo thứ tự: ``<chal>/writeup/README.md`` (layout mới),
          fallback ``<chal>/README.md`` (layout phẳng cũ — file này chính là
          template writeup).
        - ``flag_format`` lấy từ cache ``challenges.json → ctf_info.flag_format``
          (không gọi mạng).
        - Nguyên tắc CHỈ NÂNG: kết quả assess chỉ ghi khi cao hơn giá trị hiện
          có; điểm 0 mà không khớp guard-skeleton (template chưa đụng tới ở
          layout không sinh được reference) thì không nâng lên gì cả.
        - Tính toán thuần (không ghi file) — scan/render luôn tái suy ra từ
          nội dung writeup hiện tại.

        ``meta`` / ``flag_format`` (tùy chọn): metadata.json và flag_format từ
        challenges.json đã đọc trước — caller quét toàn workspace truyền vào
        để tránh parse lại JSON mỗi challenge (hotspot O(N) trên workspace
        500+ bài). ``meta`` cũng được truyền xuyên suốt xuống ``read_status``
        (tránh double-read metadata.json mỗi challenge); repo giả lập
        cần nhận kwarg ``meta=None`` khớp ``WorkspaceRepo.read_status``.
        """
        if meta is None:
            meta = repo.read_metadata(meta_path)
        status = repo.read_status(meta_path, meta=meta)
        return StatusService._apply_writeup_assessment(
            repo, meta_path, status, meta=meta, flag_format=flag_format)

    # ------------------------------------------------------------------ #
    # Writeup assessment wiring (GAP-03)
    # ------------------------------------------------------------------ #
    @classmethod
    def _apply_writeup_assessment(cls, repo: WorkspaceRepo,
                                  meta_path, status: Dict[str, Any],
                                  meta: Optional[dict] = None,
                                  flag_format=_UNSET) -> Dict[str, Any]:
        if not status.get("writeup_auto", True):
            return status

        text = cls._read_writeup_text(meta_path.parent)
        if not text:
            return status

        if flag_format is _UNSET:
            flag_format = None
            try:
                data = repo.read_challenges()
                flag_format = ((data.get("ctf_info") or {}).get("flag_format")) or None
            except Exception:
                flag_format = None

        reference_template = None
        try:
            if meta is None:
                meta = repo.read_metadata(meta_path) or {}
            reference_template = cls._reference_template(meta)
        except Exception:
            reference_template = None

        try:
            result = assess_writeup(text, flag_format, reference_template)
        except Exception:
            return status

        assessed = str(result.get("status") or "none")
        signals = result.get("signals") or {}
        score = int(result.get("score") or 0)
        skeleton_guarded = (
            signals.get("template_similarity") is not None
            and float(signals["template_similarity"]) >= 0.95
        )
        if FLAG_PLACEHOLDER in text and not skeleton_guarded:
            # Placeholder flag chưa được thay → người viết chưa đụng vào
            # writeup. Bỏ qua điểm "noise" từ nội dung mô tả đề bài (layout
            # phẳng cũ) — chỉ guard-skeleton mới được nâng lên `skeleton`.
            return status
        if score <= 0 and not skeleton_guarded:
            # Chưa có tín hiệu nội dung nào — không nâng trục writeup.
            return status

        current = str(status.get("writeup") or "none")
        if WRITEUP_RANK.get(assessed, 0) > WRITEUP_RANK.get(current, 0):
            merged = dict(status)
            merged["writeup"] = assessed
            return merged
        return status

    @staticmethod
    def _read_writeup_text(challenge_dir):
        """Nội dung writeup của challenge: ``writeup/README.md`` trước, fallback
        ``README.md`` ở thư mục gốc challenge (layout phẳng cũ)."""
        for rel in ("writeup/README.md", "README.md"):
            path = challenge_dir / rel
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if text.strip():
                return text
        return None

    @staticmethod
    def _reference_template(meta: Dict[str, Any]) -> Optional[str]:
        """Sinh lại template writeup từ metadata để guard-skeleton của assessor
        hoạt động. Trả None nếu không sinh được (assessor tự bỏ qua guard)."""
        try:
            from ..generator.workspace_builder import WorkspaceBuilder
            from ..models import Challenge

            raw = meta.get("raw") if isinstance(meta.get("raw"), dict) else {}

            # points có thể là float('inf') (literal Infinity từ platform API):
            # int(inf) raise OverflowError -> mất hẳn guard-skeleton -> description
            # độc tự nâng writeup. Sanitize thay vì để vỡ.
            raw_points = meta.get("points")
            try:
                points = int(raw_points)
            except (TypeError, ValueError, OverflowError):
                points = 0

            chall = Challenge(
                id=meta.get("id"),
                name=str(meta.get("name") or ""),
                category=str(meta.get("category") or "Misc"),
                points=points,
                description=meta.get("description") or raw.get("description") or "",
                author=meta.get("author"),
                connection_info=meta.get("connection_info"),
                solves_count=meta.get("solves_count"),
            )
            return WorkspaceBuilder._generate_writeup_template(chall)
        except Exception:
            return None

    # ------------------------------------------------------------------ #
    # Notes & Tags (P1-6 — memory của người chơi: "đã thử SSTI, bị chặn")
    # ------------------------------------------------------------------ #
    @staticmethod
    def _local_challenges(repo: WorkspaceRepo) -> List[Tuple[Any, dict]]:
        """(meta_path, metadata) của mọi challenge ĐÃ download (có metadata.json).
        Note/tag ghi vào metadata.status nên chỉ challenge local resolve được.
        Bản tombstone superseded_by bị loại — không resolve/note vào thư mục
        chết (review-6 HIGH)."""
        out = []
        for meta_path in repo.iter_challenges():
            meta = repo.read_metadata(meta_path)
            if meta and not is_superseded(meta):
                out.append((meta_path, meta))
        return out

    @classmethod
    def resolve_challenge(cls, repo: WorkspaceRepo,
                          target) -> Tuple[Optional[Any], Optional[dict]]:
        """Resolve challenge theo cùng tier với ``WorkspaceRepo.find_challenge``
        (exact id → exact name → substring name) nhưng trên metadata LOCAL:

        - Exact id / exact name khớp 1 → chọn ngay.
        - Substring khớp đúng 1 → chọn.
        - Nhiều khớp → raise ``AmbiguousChallengeError`` (liệt kê, KHÔNG partial
          match âm thầm); không khớp → raise ``ChallengeNotFoundError``.

        Trả về ``(meta_path, meta)`` khi thành công.
        """
        q = str(target).strip()
        locals_ = cls._local_challenges(repo)
        if not q:
            raise ChallengeNotFoundError("Empty challenge identifier.")

        id_hits = [(p, m) for p, m in locals_ if str(m.get('id')) == q]
        if len(id_hits) == 1:
            return id_hits[0]

        q_low = q.lower()
        name_hits = [(p, m) for p, m in locals_
                     if str(m.get('name', '')).strip().lower() == q_low]
        if len(name_hits) == 1:
            return name_hits[0]
        if len(name_hits) > 1:
            raise AmbiguousChallengeError([m for _, m in name_hits])

        sub_hits = [(p, m) for p, m in locals_ if q_low in str(m.get('name', '')).lower()]
        if len(sub_hits) == 1:
            return sub_hits[0]
        if len(sub_hits) > 1:
            raise AmbiguousChallengeError([m for _, m in sub_hits])
        raise ChallengeNotFoundError(f"Challenge not found: '{target}'")

    @staticmethod
    def _prompt_multiline() -> str:
        """Prompt nhập note multi-line: đọc đến dòng trống (Enter 2 lần để kết thúc)."""
        print("Nhập note (kết thúc bằng dòng trống):")
        lines: List[str] = []
        try:
            while True:
                line = input()
                if not line.strip():
                    break
                lines.append(line)
        except (EOFError, OSError):
            # stdin đóng/không đọc được (EOF, pipe kín, output bị capture):
            # coi như hết input, trả những dòng đã nhập.
            pass
        return "\n".join(lines).strip()

    @classmethod
    def set_note(cls, repo: WorkspaceRepo, target,
                 text: Optional[str] = None, remove: bool = False) -> bool:
        """Ghi/xoá ``status.notes`` của một challenge (atomic + flock qua
        ``repo.update_status``).

        - ``remove=True`` → xoá note.
        - ``text`` rỗng/None → prompt nhập multi-line đến dòng trống.
        Trả True khi thành công; lỗi đã log + trả False.
        """
        try:
            meta_path, meta = cls.resolve_challenge(repo, target)
        except ChallengeNotFoundError as e:
            Logger.error(str(e))
            return False
        except AmbiguousChallengeError as e:
            Logger.error(str(e))
            cls._print_matches(e.matches)
            return False

        name = meta.get('name', target)

        def _mut(st):
            # Closure trễ ràng buộc: đọc ``content`` LÚC GỌI (đã gán từ argv
            # hoặc prompt ở dưới) — dùng ``text`` gốc sẽ lưu None/rỗng.
            st["notes"] = "" if remove else content
            return st

        try:
            content = "" if remove else (text or "").strip()
            if not remove:
                if not content:
                    content = cls._prompt_multiline()
                if not content:
                    Logger.error("Note is empty — nothing saved.")
                    return False
            res = repo.update_status(meta_path, _mut)
            if getattr(res, "noop", False):
                # Review 3e0fbcc-F2: giá trị cũ == giá trị mới — không có gì
                # để ghi. Thông điệp trung tính, không success giả cũng
                # không coi là lỗi.
                Logger.info(
                    f"Không có gì thay đổi — note của "
                    f"[bold][info]{escape(str(name))}[/info][/bold] giữ nguyên.",
                    markup=True)
            elif not getattr(res, "persisted", True):
                # Ghi bị SKIP (thư mục/metadata biến mất trên đĩa — chống
                # zombie BUG-C16-1): KHÔNG được in ✔ success.
                Logger.error(
                    f"Không lưu được note cho "
                    f"[bold][info]{escape(str(name))}[/info][/bold]: thư mục "
                    f"workspace không còn trên đĩa ({meta_path}) — ghi bị "
                    f"bỏ qua.")
                return False
            elif remove:
                Logger.success(f"🗑️ Removed note from [bold][info]{escape(str(name))}[/info][/bold].", markup=True)
            else:
                Logger.success(f"📝 Note saved for [bold][info]{escape(str(name))}[/info][/bold].", markup=True)
            return True
        except Exception as e:
            Logger.warning(f"Không thể lưu note: {e}")
            return False

    @classmethod
    def update_tags(cls, repo: WorkspaceRepo, target,
                    tags: List[str], remove: bool = False) -> Tuple[bool, List[str]]:
        """Thêm/xoá labels trong ``status.labels``.

        Tag được lowercase rồi validate theo ``TAG_PATTERN`` ([a-z0-9-], ≤24);
        tag sai định dạng bị TỪ CHỐI (trả về trong danh sách ``rejected``, không
        ghi nửa vời). Trả ``(ok, rejected_tags)``.
        """
        tags = [str(t) for t in (tags or []) if str(t).strip()]
        if not tags:
            Logger.error("Usage: ctf tag <challenge> <tag...> [-r]")
            return False, []

        normalized: List[str] = []
        rejected: List[str] = []
        for t in tags:
            low = t.strip().lower()
            if TAG_PATTERN.match(low):
                if low not in normalized:
                    normalized.append(low)
            else:
                rejected.append(t)

        if rejected:
            Logger.error(
                f"Invalid tag(s): {', '.join(rejected)} — chỉ chấp nhận "
                f"chữ thường a-z, số 0-9 và dấu gạch ngang (-), "
                f"tối đa {TAG_MAX_LEN} ký tự.")
            return False, rejected

        try:
            meta_path, meta = cls.resolve_challenge(repo, target)
        except ChallengeNotFoundError as e:
            Logger.error(str(e))
            return False, rejected
        except AmbiguousChallengeError as e:
            Logger.error(str(e))
            cls._print_matches(e.matches)
            return False, rejected

        name = meta.get('name', target)

        def _mut(st):
            current = [str(x) for x in (st.get("labels") or [])]
            if remove:
                merged = [x for x in current if x not in normalized]
            else:
                merged = current + [x for x in normalized if x not in current]
            st["labels"] = merged
            return st

        action = 'Removed' if remove else 'Added'
        try:
            final = repo.update_status(meta_path, _mut)
            labels_str = ", ".join(final.get("labels") or []) or "(none)"
            if getattr(final, "noop", False):
                # Review 3e0fbcc-F2: giá trị cũ == giá trị mới — trung tính.
                Logger.info(
                    f"Không có gì thay đổi — tags của "
                    f"[bold][info]{escape(str(name))}[/info][/bold] giữ nguyên: "
                    f"{escape(labels_str)}", markup=True)
            elif not getattr(final, "persisted", True):
                # Ghi bị SKIP (thư mục/metadata biến mất trên đĩa): KHÔNG
                # in 🏷️ success.
                Logger.error(
                    f"Không cập nhật được tags cho "
                    f"[bold][info]{escape(str(name))}[/info][/bold]: thư mục "
                    f"workspace không còn trên đĩa ({meta_path}) — ghi bị "
                    f"bỏ qua.")
                return False, rejected
            else:
                Logger.success(
                    f"🏷️ {action} tag(s) for [bold][info]{escape(str(name))}[/info][/bold]: {escape(labels_str)}", markup=True)
            return True, rejected
        except Exception as e:
            Logger.warning(f"Không thể cập nhật tags: {e}")
            return False, rejected

    @staticmethod
    def _print_matches(matches: List[dict]) -> None:
        """Liệt kê các candidate khớp để user chọn chính xác hơn."""
        Logger.info("Multiple challenges matched — hãy nhập chính xác hơn:")
        for m in matches:
            print(f"   - {m.get('id')}. {m.get('name')} "
                  f"({m.get('category', 'Misc')}, {m.get('points', 0)} pts)")

    # ------------------------------------------------------------------ #
    # Scan một workspace
    # ------------------------------------------------------------------ #
    @staticmethod
    def scan_local_challenges(repo: WorkspaceRepo) -> List[Dict[str, Any]]:
        """Đọc metadata mọi challenge trong workspace, gắn trạng thái solved.

        flag_format từ challenges.json được đọc MỘT lần cho cả scan và metadata
        của từng challenge được truyền xuyên suộc compute_status — không parse
        lại JSON mỗi bài (hotspot O(N) khi workspace lớn).
        """
        workspace_path = repo.root
        try:
            challenges_data = repo.read_challenges()
            flag_format = (((challenges_data or {}).get("ctf_info") or {})
                           .get("flag_format")) or None
        except Exception:
            flag_format = None
        results = []
        for meta_path in repo.iter_challenges():
            try:
                m = repo.read_metadata(meta_path)
                if not m or is_superseded(m):
                    # Review-6 HIGH: tombstone không hiện trong scan/stats
                    # (tránh đếm đôi + render thư mục chết).
                    continue
                root = meta_path.parent

                # Trạng thái đa chiều (normalize + migrate-on-read từ legacy:
                # bool solved_by_me / marker README / placeholder flag thay rồi /
                # instance_info.is_container) + tự đánh giá trục writeup
                status = StatusService.compute_status(
                    repo, meta_path, meta=m, flag_format=flag_format)
                m['_status'] = status

                m['solved_by_me'] = status['solve'] == 'solved_by_me'
                m['_folder'] = str(root)
                m['_rel_folder'] = os.path.relpath(root, workspace_path)

                # Count local files in challenge/ subdirectory or root
                c_subdir = root / 'challenge'
                if c_subdir.is_dir():
                    local_files = [f for f in os.listdir(c_subdir) if f not in ['__pycache__', '.git']]
                else:
                    local_files = [f for f in os.listdir(root) if f not in ['metadata.json', 'README.md', 'solve.py', 'challenge', 'solver', 'writeup', 'script', 'scripts', '__pycache__']]
                m['_local_files_count'] = len(local_files)

                results.append(m)
            except Exception:
                pass
        return results

    @staticmethod
    def _is_solved(chall: Dict[str, Any]) -> bool:
        st = chall.get('_status') or {}
        solve = st.get('solve')
        if solve:
            return solve in StatusService.SOLVED_VALUES
        return bool(chall.get('solved_by_me'))

    @staticmethod
    def summary_stats(repo: WorkspaceRepo,
                      challenges: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
        """Tổng hợp thống kê giải từ repo (dùng lại ``challenges`` nếu đã scan)."""
        challs = challenges if challenges is not None else StatusService.scan_local_challenges(repo)
        workspace_path = repo.root

        total_challs = len(challs)
        solved_challs = sum(1 for c in challs if StatusService._is_solved(c))
        total_points = sum((c.get('points') or 0) for c in challs)
        earned_points = sum((c.get('points') or 0) for c in challs if StatusService._is_solved(c))

        hoarded = sum(1 for c in challs
                      if ((c.get('_status') or {}).get('flag') or {}).get('state') in ('hoarded', 'submitted_correct'))
        drafts = sum(1 for c in challs
                     if (c.get('_status') or {}).get('writeup') in ('draft', 'complete'))

        by_cat: Dict[str, Dict[str, Any]] = {}
        for c in challs:
            cat = c.get('category', 'Misc')
            pts = c.get('points') or 0
            if cat not in by_cat:
                by_cat[cat] = {'total': 0, 'solved': 0, 'points': 0, 'earned': 0, 'challenges': []}
            by_cat[cat]['total'] += 1
            by_cat[cat]['points'] += pts
            if StatusService._is_solved(c):
                by_cat[cat]['solved'] += 1
                by_cat[cat]['earned'] += pts
            by_cat[cat]['challenges'].append(c)

        total_files = sum(c.get('_local_files_count', 0) for c in challs)

        challenges_data = repo.read_challenges()
        ctf_info = challenges_data.get('ctf_info', {}) if isinstance(challenges_data, dict) else {}
        return {
            'title': ctf_info.get('title') or os.path.basename(workspace_path),
            'url': ctf_info.get('url', ''),
            'platform': ctf_info.get('platform', 'generic'),
            'user': ctf_info.get('user', ''),
            'team': ctf_info.get('team', ''),
            'total_challenges': total_challs,
            'solved_challenges': solved_challs,
            'unsolved_challenges': total_challs - solved_challs,
            'total_points': total_points,
            'earned_points': earned_points,
            'hoarded_flags': hoarded,
            'writeup_drafts': drafts,
            'local_files': total_files,
            'completion_rate': (solved_challs / total_challs * 100) if total_challs > 0 else 0,
            'categories': by_cat
        }

    # ------------------------------------------------------------------ #
    # Render primitives (btop box aesthetic qua rich)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _gradient_enabled() -> bool:
        """Bật meter gradient chỉ khi stdout là TTY thật và terminal đủ rộng
        (terminal hẹp < 60 cols hoặc non-TTY → fallback plain)."""
        try:
            if not sys.stdout.isatty():
                return False
            return shutil.get_terminal_size().columns >= 60
        except Exception:
            return False

    @classmethod
    def _tty_columns(cls) -> int:
        """Số cột terminal cho logic degrade.

        - TTY thật → đo cửa sổ thật (``shutil.get_terminal_size``).
        - Non-TTY (pipe/redirect) → KHÔNG còn cửa sổ thật để đo: mặc định
          **80 cols** thay vì số cực đại cũ (10**6) — trước đây pipe ra
          terminal hẹp vẫn nhận layout quá rộng (synthesis uiv2 #9). Env
          ``COLUMNS``
          set rõ vẫn được tôn trọng qua ``shutil.get_terminal_size``
          (vd ``COLUMNS=120 python3 main.py status | cat``) để capture/
          script giữ quyền ép độ rộng.
        - Thông tin không mất theo width: ngưỡng narrow là ``< 80`` nên
          mặc định 80 vẫn GIỮ đầy đủ cột solves/badges của ChallengeRow;
          pipe chỉ bớt chrome tương tác (footer) đúng hợp đồng
          ``ui.widgets.footer_bar``.
        """
        try:
            return shutil.get_terminal_size().columns
        except Exception:
            return 80

    @classmethod
    def _meter_only(cls, rate: float, width: int) -> Text:
        """Meter amber thuần (không prefix/suffix) dạng ``rich.text.Text``.

        - TTY đủ rộng → ``ui.widgets.meter`` per-cell với ``SOLVE_RAMP``
          3 mốc spec §3.3 (#6B4300/#FFB000/#FFE49A — codex-r3 #1, không
          nội suy thêm).
        - Terminal hẹp / non-TTY → ``ui.widgets.plain_meter`` ▰▱ không màu
          (SPEC UI v2 §M1 — một nguồn truth, vẫn Text để caller không bị
          rich parse markup nhầm dấu ``[``).
        """
        from ..ui.widgets import meter, plain_meter
        if cls._gradient_enabled():
            return meter(rate, width, _SOLVE_RAMP)
        return plain_meter(rate, width)

    @staticmethod
    def _emit(line: Union[str, Text, Table, Panel]) -> None:
        """In một dòng/widget qua console status.

        str được bọc trong ``Text`` (không bao giờ parse markup — tên
        challenge/note có thể chứa ``[...]``), và in với soft_wrap để dòng dài
        không bị ngắt giữa các cột ở terminal hẹp.
        """
        if isinstance(line, str):
            line = Text(line)
        _status_console.print(line, soft_wrap=True)

    # ------------------------------------------------------------------ #
    # Render cây challenge
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # StatusDashboard (design-system spec §4.2 — PHOSPHOR FIELD KIT)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _solve_pulse(repo: WorkspaceRepo) -> Tuple[List[float], int]:
        """Nhịp giải 24h cho sparkline ``NHỊP GIẢI`` (spec §4.2).

        Chỉ AGGREGATE dữ liệu có sẵn trong ``submit_history.json`` lúc render
        (không thêm tính năng): 24 bucket theo giờ, flag đếm theo entry
        ``result == 'correct'`` trong cửa sổ 24h qua. Trả ``(24 giá trị giờ,
        tổng flag trong window)``.
        """
        import datetime as _dt
        try:
            entries = repo.load_submit_history().get('entries') or []
        except Exception:
            entries = []
        if not entries:
            return [], 0

        now = _utcnow()

        def _parse_ts(v):
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                try:
                    return _dt.datetime.fromtimestamp(float(v), _dt.timezone.utc)
                except (OverflowError, OSError, ValueError):
                    return None
            s = str(v or '').strip()
            if not s:
                return None
            try:
                d = _dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
                return d if d.tzinfo else d.replace(tzinfo=_dt.timezone.utc)
            except ValueError:
                return None

        buckets = [0.0] * 24
        total = 0
        for e in entries:
            if str(e.get('result') or '') != 'correct':
                continue
            ts = _parse_ts(e.get('timestamp'))
            if ts is None:
                continue
            age_h = (now - ts).total_seconds() / 3600.0
            if 0 <= age_h <= 24:
                buckets[min(23, int(age_h))] += 1
                total += 1
        return buckets, total

    @staticmethod
    def _pad_cells(line: Text, width: int) -> Text:
        """Đệm dòng tới đúng ``width`` cell (không cắt khi dài hơn) để mọi
        dòng trong panel cùng bề ngang — viền panel đóng thẳng hàng mà không
        phụ thuộc semantics ``Panel.width`` của rich cũ (bị bỏ qua khi
        ``expand=False``). Đo bằng ``cell_len`` (emoji 2-cell không lệch)."""
        pad = width - cell_len(line.plain)
        if pad > 0:
            line.append(" " * pad)
        return line

    @staticmethod
    def _truncate_cells_plain(s: str, limit: int) -> str:
        """Cắt chuỗi plain xuống ≤ ``limit`` cell (ellipsis), đo bằng
        ``cell_len`` — dùng cho subtitle biên panel để không làm nở khung."""
        if cell_len(s) <= limit:
            return s
        out: List[str] = []
        width = 0
        for ch in s:
            cw = cell_len(ch)
            if width + cw > limit - 1:
                break
            out.append(ch)
            width += cw
        return "".join(out) + "…"

    @staticmethod
    def _lines_panel(lines: List[Text], inner_width: int, title: str,
                     subtitle: Optional[Text] = None) -> Panel:
        """Panel ROUNDED viền ``accent.deep`` từ các dòng content đã căn:
        padding ``(0, 1)`` → tổng rộng = ``inner_width + 4`` (2 biên + 2
        padding). Tiêu đề nằm trên biên trên, bold fg.base; subtitle (tuỳ
        chọn) nằm trên biên dưới, muted."""
        grid = Table.grid()
        for ln in lines:
            grid.add_row(StatusService._pad_cells(ln, inner_width))
        return Panel(
            grid,
            box=box.ROUNDED,
            border_style="accent.deep",
            title=Text(f" {title} ", style=f"bold {FG_BASE}"),
            subtitle=subtitle,
            expand=False,
            padding=(0, 1),
        )

    @classmethod
    def _identity_subtitle(cls, stats: Dict[str, Any], inner_width: int) -> Text:
        """Compact overview identity: event title + platform only."""
        parts = [
            str(stats['title']),
            str(stats['platform']).lower(),
        ]
        joined = " · ".join(part for part in parts if part)
        return Text(
            cls._truncate_cells_plain(joined, inner_width),
            style="fg.muted",
        )

    @classmethod
    def _progress_lines(cls, stats: Dict[str, Any]) -> List[Text]:
        """One dense progress row: meter, solve rate, points, hoard and drafts."""
        from ..ui.widgets import meter as _meter

        rate = stats['completion_rate']
        solved_n = stats['solved_challenges']
        total_n = stats['total_challenges']

        if cls._gradient_enabled():
            bar = _meter(rate, 22, _SOLVE_RAMP)
        else:
            bar = cls._meter_only(rate, 22)

        row = Text()
        row.append_text(bar)
        row.append(" ")
        row.append(f"{solved_n}/{total_n}", style=f"bold {FG_BASE}")
        row.append(" · ", style="fg.muted")
        row.append(f"{rate:.1f}%", style="fg.base")
        row.append("   ")
        row.append(str(stats['earned_points']), style="accent.hi")
        row.append(f"/{stats['total_points']} pts", style="fg.muted")
        row.append(
            f" · hoarded {stats.get('hoarded_flags', 0)}"
            f" · drafts {stats.get('writeup_drafts', 0)}",
            style="fg.muted",
        )
        return [row]

    @classmethod
    def _solve_lines(cls, repo: WorkspaceRepo) -> List[Text]:
        """Optional event/activity rows; emit nothing for a zero-signal state."""
        from ..ui.widgets import braille_graph

        lines: List[Text] = []
        window_str = StatusService._render_window(repo)
        if window_str:
            win_style = (
                "error" if window_str.startswith("LIVE")
                else "warn" if window_str.startswith("Countdown")
                else "fg.muted"
            )
            lines.append(Text(window_str, style=win_style))

        pulse, pulse_total = cls._solve_pulse(repo)
        if pulse_total > 0:
            row = Text()
            row.append(f"+{pulse_total} flags 24h ", style="fg.muted")
            spark = braille_graph(pulse, 12)
            spark.stylize("accent")
            row.append_text(spark)
            lines.append(row)
        return lines

    @classmethod
    def _render_overview(cls, repo: WorkspaceRepo,
                         stats: Dict[str, Any]) -> None:
        """Render one responsive overview panel with only meaningful data."""
        lines = cls._progress_lines(stats) + cls._solve_lines(repo)
        cols = cls._tty_columns()
        total_width = min(
            OVERVIEW_MAX_TOTAL_COLS,
            max(OVERVIEW_MIN_TOTAL_COLS, cols),
        )
        inner_width = max(20, total_width - 4)

        fitted: List[Text] = []
        for line in lines:
            item = line.copy()
            if cell_len(item.plain) > inner_width:
                item.truncate(inner_width, overflow="ellipsis", pad=False)
            fitted.append(item)

        identity_budget = max(8, inner_width - cell_len("TIẾN ĐỘ · "))
        identity = cls._identity_subtitle(stats, identity_budget).plain
        title = cls._truncate_cells_plain(
            f"TIẾN ĐỘ · {identity}",
            max(8, inner_width - 2),
        )
        cls._emit(
            cls._lines_panel(
                fitted,
                inner_width,
                title,
            )
        )

    @classmethod
    def _category_heading(cls, cat: str, solved: int, total: int,
                          earned_pts: int, total_pts: int) -> Text:
        """CategorySection tile (SPEC UI v2 §L1 — corner-glyph frame):

        ``┌┐ NAME ────đẩy tới cột tail────  d/d [meter10] earned/total``

        Cặp ``┌┐`` accent.deep (pattern ``shortcut_title``), TÊN bold
        fg.base UPPERCASE, divider ``─`` accent.deep fill tự động theo độ
        rộng terminal (cap 88 cho pipe/hẹp), tail giữ nguyên: đếm bold,
        mini-meter 10 ô, điểm muted.
        """
        head = Text()
        head.append("┌┐ ", style="accent.deep")
        head.append(str(cat).upper(), style=f"bold {FG_BASE}")
        tail = Text()
        tail.append("  ")   # khoảng thở giữa divider và số liệu (frame spec)
        tail.append(f"{solved}/{total} ", style=f"bold {FG_BASE}")
        tail.append_text(cls._meter_only(
            (solved / total * 100) if total > 0 else 0, 10))
        tail.append(" ")
        tail.append(f"{earned_pts}/{total_pts}", style="fg.muted")
        target = min(88, max(40, cls._tty_columns() - 4))
        pad = target - cell_len(head.plain) - cell_len(tail.plain)
        # 1 space sau NAME + divider fill accent.deep tới cột tail; tail mở
        # đầu bằng 2 space → đúng nhịp frame ``┌┐ WEB ─────  3/8``.
        head.append(" ", style="accent.deep")
        head.append("─" * max(1, pad), style="accent.deep")
        head.append_text(tail)
        return head

    @classmethod
    def render_tree(cls, repo: WorkspaceRepo,
                    stats: Optional[Dict[str, Any]] = None,
                    filter_cat: Optional[List[str]] = None,
                    only_unsolved: bool = False,
                    only_solved: bool = False,
                    only_container: bool = False,
                    filter_labels: Optional[List[str]] = None,
                    search: Optional[str] = None) -> None:
        if stats is None:
            stats = StatusService.summary_stats(repo)

        cols = cls._tty_columns()
        narrow = cols < 80

        # Compact shell: một overview panel duy nhất; activity/window chỉ
        # xuất hiện khi có tín hiệu thật. Masthead và footer thuộc cli.py.
        cls._render_overview(repo, stats)

        # ChallengeRow schema TOÀN MÀN (codex-r2 P1): gom row của MỌI category
        # rồi căn qua MỘT lưới duy nhất — vị trí pts/solves/badge không đổi
        # giữa các section (trước đây mỗi category tự co giãn theo tên dài).
        sections: List[Tuple[Text, List[List[Text]]]] = []

        categories = stats['categories']
        for cat, data in sorted(categories.items()):
            if filter_cat and cat.lower() not in [c.lower() for c in filter_cat]:
                continue

            c_list = data['challenges']
            if only_unsolved:
                c_list = [c for c in c_list if not StatusService._is_solved(c)]
            elif only_solved:
                c_list = [c for c in c_list if StatusService._is_solved(c)]

            if only_container:
                c_list = [c for c in c_list if repo.is_container(c)]

            if filter_labels:
                # AND: challenge phải mang TẤT CẢ label chỉ định.
                def _has_all_labels(c):
                    labels = {str(x) for x in ((c.get('_status') or {}).get('labels') or [])}
                    return all(str(lbl) in labels for lbl in filter_labels)
                c_list = [c for c in c_list if _has_all_labels(c)]

            if search:
                q_low = str(search).strip().lower()
                if q_low:
                    def _matches_search(c, query=q_low):
                        st = c.get('_status') or {}
                        haystack = ' '.join([
                            str(c.get('name', '')),
                            str(st.get('notes') or ''),
                        ]).lower()
                        return query in haystack
                    c_list = [c for c in c_list if _matches_search(c)]

            if not c_list:
                continue

            sections.append((
                cls._category_heading(
                    cat, data['solved'], data['total'],
                    data.get('earned', 0), data.get('points', 0)),
                cls._challenge_cells(repo, c_list),
            ))

        all_rows = [row for _, rows in sections for row in rows]
        aligns = ['left'] * len(all_rows[0]) if all_rows else []
        # Cột id + pts + solves căn phải (spec §4.3).
        if aligns:
            aligns[1] = 'right'   # id
            aligns[3] = 'right'   # pts
            if not narrow:
                aligns[5] = 'right'   # solves
        # Khoảng cách theo capture §4.3: số sát đơn vị, rộng hơn trước badge.
        gaps = ([2, 2, 2, 1, 3, 1, 3, 2] if not narrow
                else [2, 2, 2, 1, 3, 2])
        aligned = StatusService._aligned_grid(all_rows, aligns, gaps=gaps)

        idx = 0
        for heading, rows in sections:
            cls._emit(heading)
            for _line in aligned[idx:idx + len(rows)]:
                indented = Text("  ")
                indented.append_text(_line)
                cls._emit(indented)
            idx += len(rows)

    @staticmethod
    def _aligned_grid(rows: List[List[Text]],
                      aligns: List[str],
                      gap: str = "  ",
                      gaps: Optional[List[int]] = None) -> List[Text]:
        """Grid ẩn viền căn cột THẲNG HÀNG kiểu btop: mỗi cột rộng bằng cell
        dài nhất, đệm bằng khoảng trắng (không dùng rich Table để bảng KHÔNG
        bị nén/truncate theo chiều rộng console — terminal hẹp tự overflow
        giữ nguyên thông tin, đúng hành vi print() cũ).

        ``aligns[i]`` là 'left' | 'right'; độ rộng đo bằng ``cell_len``.
        ``gaps[k]`` đè khoảng cách SAU cột k (spec §4.3: số sát đơn vị
        ``500 pts``, rộng hơn trước badge); thiếu thì dùng ``gap`` chung.
        """
        if not rows:
            return []
        n_cols = max(len(r) for r in rows)
        widths = [0] * n_cols
        for r in rows:
            for i, cell in enumerate(r):
                widths[i] = max(widths[i], cell_len(cell.plain))
        lines: List[Text] = []
        for r in rows:
            line = Text()
            for i, cell in enumerate(r):
                pad = " " * (widths[i] - cell_len(cell.plain))
                if aligns[i] == 'right':
                    line.append(pad)
                    line.append_text(cell)
                else:
                    line.append_text(cell)
                    line.append(pad)
                if i < len(r) - 1:
                    line.append(" " * (gaps[i] if gaps and i < len(gaps)
                                       else len(gap)))
            line.rstrip()  # rich cũ: mutate tại chỗ (trả None)
            lines.append(line)
        return lines

    @staticmethod
    def _truncate_cells(name: str,
                        limit: int = CHALLENGE_NAME_MAX_CELLS) -> str:
        """Cắt tên challenge xuống ≤ ``limit`` cell hiển thị, kết thúc bằng
        ellipsis. Đo bằng ``cell_len`` nên emoji rộng 2 cell (🐧 🪟) không
        làm lệch lưới ChallengeRow."""
        from rich.cells import cell_len as _cell_len
        if _cell_len(name) <= limit:
            return name
        out: List[str] = []
        width = 0
        for ch in name:
            cw = _cell_len(ch)
            if width + cw > limit - 1:
                break
            out.append(ch)
            width += cw
        return "".join(out) + "…"

    @classmethod
    def _challenge_cells(cls, repo: WorkspaceRepo,
                         c_list: List[Dict[str, Any]]) -> List[List[Text]]:
        """ChallengeRow (spec §4.3, PHOSPHOR FIELD KIT) — trả ROW THÔ (list
        cell ``Text``), việc căn cột do :meth:`render_tree` gộp toàn màn:

        ``[state] [id>3] [name≤24] [pts>4] pts [solves>3] giải [badges ✎⛁⎘] [note]``

        - Glyph ngữ nghĩa thay emoji: ``✔`` solved / ``◆`` working / ``·``
          unsolved; badge mỗi trục 1 glyph hoặc rỗng: ``✎`` draft · ``⛁``
          container · ``⎘`` file (chrome màu fg.faint).
        - Màu theo state: solved → tên muted + pts accent.hi; unsolved → tên
          fg.base + số muted; working → ◆ warn. (firstblood ``◆`` đỏ chỉ khi
          có dữ liệu nguồn — hiện chưa track nên không bao giờ tự render.)
        - Cột solves/files ẩn khi terminal hẹp (<80 cols); non-TTY giữ đầy đủ.
        """
        G = ROW_GLYPHS
        rows: List[List[Text]] = []
        narrow = cls._tty_columns() < 80
        for c in c_list:
            status = c.get('_status') or {}
            solve = status.get('solve', 'unsolved')
            writeup = status.get('writeup', 'none')
            container = StatusService._effective_container(repo, c, status)
            files_count = c.get('_local_files_count', 0)

            glyph, glyph_style = G['solve'].get(solve, G['solve']['unsolved'])
            state_cell = Text(glyph, style=glyph_style)
            is_solved = solve in StatusService.SOLVED_VALUES

            name_st = FG_MUTED if is_solved else FG_BASE
            labels = [str(x) for x in (status.get('labels') or [])]
            name_cell = Text(cls._truncate_cells(str(c.get('name', 'Unknown'))),
                             style=name_st)
            if labels:
                name_cell.append(" #" + ",".join(labels), style="fg.muted")

            pts_st = ("accent.hi" if is_solved
                      else "warn" if solve == 'working' else "fg.muted")

            badges = Text(style="fg.faint")
            if writeup in ('skeleton', 'draft', 'complete'):
                badges.append(G['draft_badge'] + " ")
            if container:
                badges.append(G['container_badge'] + " ")
            if files_count > 0:
                badges.append(G['file_badge'])

            notes = str(status.get('notes') or '').strip()
            note_cell = (Text(f'"{notes}"', style="fg.muted")
                         if notes else Text())

            solves = c.get('solves_count', c.get('solves', '-'))

            row = [
                state_cell,
                Text(str(c.get('id', '?')), style="fg.faint"),
                name_cell,
                Text(str(c.get('points', 0)), style=pts_st),
                Text("pts", style="fg.faint"),
            ]
            if not narrow:
                row += [
                    Text(str(solves), style="fg.muted"),
                    Text("giải", style="fg.faint"),
                ]
            row += [badges, note_cell]
            rows.append(row)
        return rows

    # ------------------------------------------------------------------ #
    # Helpers render đa chiều
    # ------------------------------------------------------------------ #
    @staticmethod
    def _effective_container(repo: WorkspaceRepo,
                             chall: Dict[str, Any],
                             status: Dict[str, Any]) -> Optional[str]:
        """Trạng thái container hiệu lực: status.container nếu có, suy ra từ
        dấu hiệu container legacy nếu chưa có (is_container → stopped)."""
        val = status.get('container')
        if val in ('running', 'stopped'):
            return val
        inst = chall.get('instance_info')
        if isinstance(inst, dict) and inst.get('status') in ('running', 'stopped'):
            return inst.get('status')
        if repo.is_container(chall):
            return 'stopped'
        return None

    @staticmethod
    def _fmt_local(dt) -> str:
        """Giờ local ngắn gọn cho mốc thời gian (spec event-window: aware +
        hiển thị theo timezone máy user)."""
        try:
            return dt.astimezone().strftime("%H:%M %d/%m")
        except Exception:
            return ""

    @classmethod
    def _render_window(cls, repo: WorkspaceRepo) -> str:
        """Window từ ``ctf_info.event_window`` (feature Event Window mirror):
        LIVE / Countdown / Ended (không emoji — spec PHOSPHOR §6). Trả "" khi
        không có dữ liệu hợp lệ
        (workspace cũ giữ nguyên output — không in dòng).

        Parse qua ``normalize_epoch_to_utc`` (đơn vị ms/giây + ISO string,
        tz-aware UTC); so sánh trong UTC, mốc tuyệt đối hiển thị giờ local.
        """
        from ..platforms.base import normalize_epoch_to_utc

        data = repo.read_challenges()
        win = ((data.get('ctf_info') or {}).get('event_window') or {})
        start = normalize_epoch_to_utc(win.get('start'))
        end = normalize_epoch_to_utc(win.get('end'))
        if start is None or end is None or start >= end:
            return ""

        now = _utcnow()
        if start <= now <= end:
            remain = int((end - now).total_seconds())
            hrs, rem_sec = divmod(remain, 3600)
            mins = rem_sec // 60
            return (f"LIVE (còn {hrs}h{mins:02d}m"
                    f" — tới {cls._fmt_local(end)})")
        if now < start:
            remain = start - now
            days, hrs = remain.days, remain.seconds // 3600
            return (f"Countdown (bắt đầu sau {days}d {hrs}h"
                    f" — {cls._fmt_local(start)})")
        days = int((now - end).total_seconds() // 86400)
        return f"Ended (kết thúc {days} ngày trước — {cls._fmt_local(end)})"

    # ------------------------------------------------------------------ #
    # Scan toàn bộ workspace trong một thư mục gốc (bản DUY NHẤT — cli/
    # manage/interactive_menu sẽ redirect về đây ở task sau)
    # ------------------------------------------------------------------ #
    @staticmethod
    def scan_all_workspaces(base_dir: str) -> List[Dict[str, Any]]:
        """Bảng rich mọi workspace trong ``base_dir`` (PHOSPHOR FIELD KIT):
        Workspace | Platform | Progress meter | Challs.

        - Hàng workspace đã kết thúc (Event Window) thêm suffix ``· ended``
          muted (không emoji — spec §6).
        - Viền panel accent.deep, nhãn cột fg.faint UPPERCASE; footer dim
          tổng kết: số workspace / challs solved-toàn-cục.
        Trả về danh sách stats của các workspace có ít nhất 1 challenge.
        """
        from ..platforms.base import normalize_epoch_to_utc
        from ..utils.logger import Logger

        base_dir = os.path.abspath(os.path.expanduser(base_dir))
        Logger.info(f'Scanning all CTF workspaces in [info]{escape(base_dir)}[/]', markup=True)

        if not os.path.exists(base_dir):
            Logger.warning(f'Directory {base_dir} does not exist.')
            return []

        collected: List[Dict[str, Any]] = []
        now = _utcnow()
        for entry in sorted(os.listdir(base_dir)):
            full_p = os.path.join(base_dir, entry)
            if not os.path.isdir(full_p):
                continue
            repo = WorkspaceRepo(full_p)
            try:
                stats = StatusService.summary_stats(repo)
            except Exception:
                # Workspace không đọc được (vd systemd-private*, thư mục của
                # user khác) → bỏ qua thay vì sập cả lượt scan.
                continue
            if stats['total_challenges'] > 0:
                stats['_ended'] = False
                stats['_dir'] = entry
                try:
                    win = ((repo.read_challenges().get('ctf_info') or {})
                           .get('event_window') or {})
                    end = normalize_epoch_to_utc(win.get('end'))
                    stats['_ended'] = end is not None and end <= now
                except Exception:
                    stats['_ended'] = False
                collected.append(stats)

        table = Table(
            box=box.ROUNDED, border_style="accent.deep",
            header_style="fg.faint", padding=(0, 1))
        table.add_column("WORKSPACE", no_wrap=True)
        table.add_column("PLATFORM", no_wrap=True)
        table.add_column("PROGRESS", no_wrap=True)
        table.add_column("CHALLS", justify="right", no_wrap=True)

        total_solved = 0
        total_challs = 0
        # N1 (synthesis-v6): title CTF có thể trùng giữa 2 workspace — khi đó
        # gắn thêm dirname faint để hàng còn phân biệt được.
        title_counts = Counter(str(s['title']) for s in collected)
        for stats in collected:
            total_solved += stats['solved_challenges']
            total_challs += stats['total_challenges']
            name_cell = Text(str(stats['title'])[:35], style="fg.base")
            if stats['_ended']:
                name_cell.append(" · ended", style="fg.muted")
            if title_counts[str(stats['title'])] > 1 and stats.get('_dir'):
                name_cell.append(f" · {stats['_dir']}", style="fg.faint")

            rate = stats['completion_rate']
            progress_cell = StatusService._meter_only(rate, 10)
            progress_cell.append(f" {rate:.0f}%", style="fg.muted")

            challs_cell = Text()
            challs_cell.append(str(stats['solved_challenges']), style="solved")
            challs_cell.append(f"/{stats['total_challenges']}",
                               style="fg.muted")

            table.add_row(
                name_cell,
                Text(display_label(str(stats['platform'])), style="fg.muted"),
                progress_cell,
                challs_cell,
            )

        StatusService._emit(table)
        footer = Text(
            f"{len(collected)} workspaces · {total_solved}/{total_challs} "
            f"challs solved", style="fg.muted")
        StatusService._emit(footer)
        return collected
