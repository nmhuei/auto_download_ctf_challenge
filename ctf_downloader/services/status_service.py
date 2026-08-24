"""StatusService — đọc trạng thái workspace & in tổng quan.

Hợp nhất logic cũ của ``dashboard.CTFDashboard`` (scan + stats + render tree)
và 3 bản sao scan-all-workspaces (cli.py / manage.py / interactive_menu.py)
thành một nơi DUY NHẤT. Các facade (CTFDashboard) và các entrypoint chỉ
delegate vào đây.
"""
import os
from typing import Any, Dict, List, Optional

from ..storage.constants import CATEGORY_ICONS, FLAG_PLACEHOLDER, STATUS_ICONS
from ..storage.workspace_repo import WorkspaceRepo
from ..utils.writeup_assessor import assess_writeup

# Thứ bậc trục writeup cho nguyên tắc "chỉ nâng không hạ" khi áp heuristic
# (spec status-model §5: heuristic CHỈ ghi đè khi writeup_auto=True).
WRITEUP_RANK = {"none": 0, "skeleton": 1, "draft": 2, "complete": 3}


def _utcnow():
    """"Bây giờ" aware UTC — hàm riêng để test có thể patch deterministically."""
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc)


class StatusService:
    # Giá trị trục solve được tính là "đã giải" cho thống kê tiến độ
    # (giải bởi mình / team / người khác đều ăn điểm trên bảng).
    SOLVED_VALUES = ("solved_by_me", "solved_by_team", "solved_other")

    @staticmethod
    def compute_status(repo: WorkspaceRepo, meta_path) -> Dict[str, Any]:
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
        """
        status = repo.read_status(meta_path)
        return StatusService._apply_writeup_assessment(repo, meta_path, status)

    # ------------------------------------------------------------------ #
    # Writeup assessment wiring (GAP-03)
    # ------------------------------------------------------------------ #
    @classmethod
    def _apply_writeup_assessment(cls, repo: WorkspaceRepo,
                                  meta_path, status: Dict[str, Any]) -> Dict[str, Any]:
        if not status.get("writeup_auto", True):
            return status

        text = cls._read_writeup_text(meta_path.parent)
        if not text:
            return status

        flag_format = None
        try:
            data = repo.read_challenges()
            flag_format = ((data.get("ctf_info") or {}).get("flag_format")) or None
        except Exception:
            flag_format = None

        reference_template = None
        try:
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
    # Scan một workspace
    # ------------------------------------------------------------------ #
    @staticmethod
    def scan_local_challenges(repo: WorkspaceRepo) -> List[Dict[str, Any]]:
        """Đọc metadata mọi challenge trong workspace, gắn trạng thái solved."""
        workspace_path = repo.root
        results = []
        for meta_path in repo.iter_challenges():
            try:
                m = repo.read_metadata(meta_path)
                if not m:
                    continue
                root = meta_path.parent

                # Trạng thái đa chiều (normalize + migrate-on-read từ legacy:
                # bool solved_by_me / marker README / placeholder flag thay rồi /
                # instance_info.is_container) + tự đánh giá trục writeup
                status = StatusService.compute_status(repo, meta_path)
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
    # Render cây challenge
    # ------------------------------------------------------------------ #
    @staticmethod
    def render_tree(repo: WorkspaceRepo,
                    stats: Optional[Dict[str, Any]] = None,
                    filter_cat: Optional[List[str]] = None,
                    only_unsolved: bool = False,
                    only_solved: bool = False,
                    only_container: bool = False) -> None:
        if stats is None:
            stats = StatusService.summary_stats(repo)

        title = stats['title']
        platform = stats['platform'].upper()
        rate = stats['completion_rate']

        print('=' * 85)
        print(f" 🏆 CTF WORKSPACE: {title} [{platform}]")
        if stats['user'] or stats['team']:
            team_str = f" | Team: {stats['team']}" if stats['team'] else ''
            print(f" 👤 User: {stats['user']}{team_str}")
        print(f" 📊 Progress: {stats['solved_challenges']}/{stats['total_challenges']} Solved ({rate:.1f}%)")
        print(f" 💰 Points: {stats['earned_points']}/{stats['total_points']}"
              f" | 🏴 Hoarded: {stats.get('hoarded_flags', 0)}"
              f" | 📝 Drafts: {stats.get('writeup_drafts', 0)}"
              f" | 📦 Files: {stats.get('local_files', 0)}")
        window_str = StatusService._render_window(repo)
        if window_str:
            print(f" ⏱️ Window: {window_str}")

        bar_len = 30
        filled_len = int(bar_len * rate // 100)
        bar = '█' * filled_len + '░' * (bar_len - filled_len)
        print(f"    [{bar}] {rate:.1f}%")
        print('=' * 85)

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

            if not c_list:
                continue

            cat_icon = CATEGORY_ICONS.get(str(cat).lower(), '📁')
            c_rate = (data['solved'] / data['total'] * 100) if data['total'] > 0 else 0
            c_bar = '█' * int(10 * c_rate // 100) + '░' * (10 - int(10 * c_rate // 100))
            print(f"\n📁 {cat_icon} {cat} ({len(c_list)} challs, {data['points']} pts) [{c_bar}] {data['solved']}/{data['total']}")

            for idx, c in enumerate(c_list):
                is_last = (idx == len(c_list) - 1)
                prefix = '└── ' if is_last else '├── '

                status = c.get('_status') or {}
                solve = status.get('solve', 'unsolved')
                flag_state = (status.get('flag') or {}).get('state', 'none')
                writeup = status.get('writeup', 'none')
                container = StatusService._effective_container(repo, c, status)

                badge = (
                    f"[{STATUS_ICONS['solve'].get(solve, '·')}]"
                    f"[{STATUS_ICONS['flag'].get(flag_state, '∅')}]"
                    f"[{STATUS_ICONS['writeup'].get(writeup, '-')}]"
                )
                if container:
                    badge += f"[{STATUS_ICONS['container'][container]}]"

                c_id = c.get('id', '?')
                c_name = c.get('name', 'Unknown')
                c_pts = c.get('points', 0)
                solves = c.get('solves_count', c.get('solves', '-'))
                files_count = c.get('_local_files_count', 0)

                # Check container tag
                is_cont = repo.is_container(c)
                cont_str = ' [🐳 Container]' if is_cont else ''
                files_str = f' [{files_count} file(s)]' if files_count > 0 else ''

                print(f"  {prefix}{badge} {c_id:>3}. {c_name:<32} ({c_pts:>4} pts) - {str(solves):>3} solves{cont_str}{files_str}")

                notes = str(status.get('notes') or '').strip()
                if notes:
                    print(f"       └─ \"{notes}\"")

        print('\n' + '=' * 85)

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
        """⏱️ Window từ ``ctf_info.event_window`` (feature Event Window mirror):
        🔴 LIVE / ⏳ Countdown / ✅ Ended. Trả "" khi không có dữ liệu hợp lệ
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
            return (f"🔴 LIVE (còn {hrs}h{mins:02d}m"
                    f" — tới {cls._fmt_local(end)})")
        if now < start:
            remain = start - now
            days, hrs = remain.days, remain.seconds // 3600
            return (f"⏳ Countdown (bắt đầu sau {days}d {hrs}h"
                    f" — {cls._fmt_local(start)})")
        days = int((now - end).total_seconds() // 86400)
        return f"✅ Ended (kết thúc {days} ngày trước — {cls._fmt_local(end)})"

    # ------------------------------------------------------------------ #
    # Scan toàn bộ workspace trong một thư mục gốc (bản DUY NHẤT — cli/
    # manage/interactive_menu sẽ redirect về đây ở task sau)
    # ------------------------------------------------------------------ #
    @staticmethod
    def scan_all_workspaces(base_dir: str) -> List[Dict[str, Any]]:
        """In bảng tổng quát mọi workspace trong ``base_dir``.

        Trả về danh sách stats của các workspace có ít nhất 1 challenge.
        """
        from ..utils.logger import Logger

        base_dir = os.path.abspath(os.path.expanduser(base_dir))
        collected: List[Dict[str, Any]] = []
        print('=' * 85)
        print(f' 📁 SCANNING ALL CTF WORKSPACES IN: {base_dir}')
        print('=' * 85)
        print(f'{"CTF Competition":<35} | {"Platform":<10} | {"Solved/Total":<14} | {"Progress":<15}')
        print('=' * 85)

        if not os.path.exists(base_dir):
            Logger.warning(f'Directory {base_dir} does not exist.')
            return collected

        for entry in sorted(os.listdir(base_dir)):
            full_p = os.path.join(base_dir, entry)
            if os.path.isdir(full_p):
                repo = WorkspaceRepo(full_p)
                stats = StatusService.summary_stats(repo)
                if stats['total_challenges'] > 0:
                    collected.append(stats)
                    title = stats['title'][:35]
                    plat = stats['platform'][:10].upper()
                    solv_str = f"{stats['solved_challenges']}/{stats['total_challenges']}"
                    rate = stats['completion_rate']
                    bar = '█' * int(8 * rate // 100) + '░' * (8 - int(8 * rate // 100))
                    prog_str = f'[{bar}] {rate:.0f}%'
                    print(f'{title:<35} | {plat:<10} | {solv_str:<14} | {prog_str:<15}')
        print('=' * 85)
        return collected
