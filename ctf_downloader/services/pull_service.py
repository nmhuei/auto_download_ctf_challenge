"""PullService — pipeline tải workspace từ một nền tảng CTF.

Chứa thân logic cũ của ``core.CTFDownloader.run`` với một sửa lỗi:
thay vì chia sẻ DUY NHẤT một ``requests.Session`` cho mọi worker thread
(bug §8.8 spec — requests.Session không thread-safe), các worker nhận
session riêng qua ``session_factory.thread_local_sessions``: session master
được dùng trên main thread cho detect + authenticate, sau đó mỗi worker
copy cookies/headers từ master đúng 1 lần và tái sử dụng trong suốt thread.
"""
import os
import time
from typing import Any, Callable, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.markup import escape
from rich.progress import (Progress, ProgressColumn, BarColumn, TextColumn,
                           TimeElapsedColumn, MofNCompleteColumn)
from rich.text import Text

from ..config import DownloaderConfig
from ..utils.logger import Logger
from ..ui import SPINNER, err_console, ok_summary
from ..ui.diagnostics import Diagnostic, render as render_diagnostic
from ..platforms.detector import PlatformDetector
from ..platforms.base import Challenge
from ..storage.constants import SOLVE_RANK
from ..extractors.link_extractor import LinkExtractor
from ..downloaders.manager import DownloadManager
from ..generator.workspace_builder import WorkspaceBuilder
from ..generator.summary_generator import SummaryGenerator
from .session_factory import create_session, thread_local_sessions


class _BrailleSpinnerColumn(ProgressColumn):
    """Spinner braille dùng đúng bộ frame từ ``ui.style.SPINNER``."""

    def __init__(self, speed: float = 1.0) -> None:
        super().__init__()
        self.speed = speed

    def render(self, task):
        elapsed = task.get_time() * self.speed
        frame = round(elapsed * 10) % len(SPINNER)
        return Text(SPINNER[frame], style="progress.spinner")


# Hint dùng chung cho mọi vấn đề xác thực / truy cập danh sách đề.
_DOCTOR_HINTS = (
    "chạy 'ctf doctor -u <url>' để kiểm tra cookie/token",
)


class PullService:
    # ------------------------------------------------------------------ #
    # UI helpers — output discipline theo layer ctf_downloader.ui
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fetch_challenges_ui(platform: Any) -> List[Any]:
        """Bọc ``fetch_challenges`` trong spinner braille transient trên
        err_console: khi xong spinner biến mất, chỉ còn một dòng
        ``ok_summary`` ("Đã tải N challenges trong X.XXs")."""
        start = time.time()
        with Progress(
            _BrailleSpinnerColumn(),
            TextColumn("[dim]{task.description}[/dim]"),
            console=err_console,
            transient=True,
        ) as progress:
            progress.add_task("Đang tải danh sách đề...", total=None)
            challenges = platform.fetch_challenges()
        secs = time.time() - start
        err_console.print(ok_summary("tải", len(challenges), "challenge", secs))
        return challenges

    @staticmethod
    def _render_detect_failure(config: DownloaderConfig, start_time: float,
                               exc: Exception) -> Dict[str, Any]:
        """Render Diagnostic cho lỗi phát hiện nền tảng + trả dict thất bại."""
        render_diagnostic(Diagnostic(
            "error",
            "Không phát hiện được nền tảng CTF",
            cause=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
            hints=("kiểm tra URL giải (đúng domain, có https://)",
                   *_DOCTOR_HINTS),
        ))
        return {"ok": False, "output_dir": config.output_dir,
                "summary_file": None, "total_files": 0,
                "challenges_processed": 0,
                "elapsed_seconds": time.time() - start_time}

    @staticmethod
    def _render_auth_warning() -> None:
        """Render Diagnostic cảnh báo xác thực thất bại (pipeline vẫn chạy)."""
        render_diagnostic(Diagnostic(
            "warning",
            "Xác thực thất bại — tiếp tục với tư cách khách chưa đăng nhập",
            hints=_DOCTOR_HINTS,
        ))

    @staticmethod
    def _render_no_challenges() -> None:
        """Render Diagnostic cho danh sách đề rỗng / không truy cập được."""
        render_diagnostic(Diagnostic(
            "error",
            "Không tìm thấy challenge nào hoặc không truy cập được danh sách đề",
            hints=("kiểm tra cookie/token còn hạn và URL đúng giải",
                   *_DOCTOR_HINTS),
        ))

    @staticmethod
    def _render_workspace_write_failure(exc: Exception) -> None:
        """Render Diagnostic khi không tạo/ghi được thư mục workspace."""
        render_diagnostic(Diagnostic(
            "error",
            "Không ghi được workspace",
            cause=f"{type(exc).__name__}: {exc}",
            hints=("kiểm tra quyền ghi và dung lượng đĩa trống của thư mục đích",
                   "chọn thư mục output khác nếu đường dẫn hiện tại bị khoá"),
        ))

    @staticmethod
    def _render_total_download_failure(failed: int, total: int) -> None:
        """Render Diagnostic khi TOÀN BỘ challenge trong lượt pull thất bại
        (lỗi per-challenge vẫn log riêng như cũ — đây là tổng kết nghiêm trọng)."""
        render_diagnostic(Diagnostic(
            "error",
            f"Tải thất bại trên toàn bộ {failed}/{total} challenge",
            hints=("kiểm tra kết nối mạng và cookie đăng nhập",
                   *_DOCTOR_HINTS,
                   "thử chạy lại với '-j 1' nếu platform rate-limit"),
        ))

    @staticmethod
    def run(config: DownloaderConfig,
            session: Optional[Any] = None) -> Dict[str, Any]:
        """Tải toàn bộ challenge của giải về workspace.

        Args:
            config: cấu hình downloader (đã/một phần điền).
            session: session master tùy chọn; nếu bỏ trống sẽ tự tạo.
                Chỉ được dùng trên main thread (detect/authenticate) —
                các worker thread luôn nhận bản sao riêng.

        Returns:
            dict kết quả: ``ok``, ``output_dir``, ``summary_file``,
            ``total_files``, ``challenges_processed``, ``elapsed_seconds``.
        """
        config.validate()
        start_time = time.time()
        Logger.banner()
        Logger.info(f"Target URL: [bold blue]{escape(config.url)}[/bold blue]", markup=True)

        # Session master: chỉ main thread dùng (detect platform + authenticate)
        master = session or create_session(
            cookie=config.cookie,
            token=config.token,
            custom_headers=config.custom_headers,
            timeout=config.timeout,
        )

        # 1. Detect Platform
        try:
            platform = PlatformDetector.detect_platform(config.url, master)
        except Exception as exc:
            return PullService._render_detect_failure(config, start_time, exc)

        # 2. Authenticate
        auth_success = platform.authenticate()
        if not auth_success:
            PullService._render_auth_warning()

        # 3. Fetch Challenges (spinner transient + ok_summary)
        challenges = PullService._fetch_challenges_ui(platform)
        if not challenges:
            PullService._render_no_challenges()
            return {
                "ok": False,
                "output_dir": config.output_dir,
                "summary_file": None,
                "total_files": 0,
                "challenges_processed": 0,
                "elapsed_seconds": time.time() - start_time,
            }

        # Auto-determine output_dir under ~/Workspace/CTF/<CTF_Title> if not explicitly specified
        from ..utils.sanitize import sanitize_ctf_title
        if not config.output_dir:
            ctf_title = platform.ctf_info.title or ""
            folder_name = sanitize_ctf_title(ctf_title, fallback_domain=config.url)
            base_ctf_dir = os.path.expanduser("~/Workspace/CTF")
            config.output_dir = os.path.abspath(os.path.join(base_ctf_dir, folder_name))

        Logger.info(f"Output Directory: [bold yellow]{escape(config.output_dir)}[/bold yellow]", markup=True)

        # Filter categories if specified
        if config.categories:
            cats = [c.lower() for c in config.categories]
            challenges = [c for c in challenges if c.category.lower() in cats]

        if config.exclude_categories:
            ex_cats = [c.lower() for c in config.exclude_categories]
            challenges = [c for c in challenges if c.category.lower() not in ex_cats]

        # Display found summary — ok_summary đã in bởi _fetch_challenges_ui;
        # giữ nguyên bảng overview theo category.
        categories_dict = {}
        for c in challenges:
            categories_dict[c.category] = categories_dict.get(c.category, 0) + 1

        rows = [[cat, str(count)] for cat, count in sorted(categories_dict.items())]
        Logger.print_table("CTF Challenges Overview", ["Category", "Count"], rows)

        # 4. Process Each Challenge
        try:
            os.makedirs(config.output_dir, exist_ok=True)
        except OSError as exc:
            PullService._render_workspace_write_failure(exc)
            return {"ok": False, "output_dir": config.output_dir,
                    "summary_file": None, "total_files": 0,
                    "challenges_processed": 0,
                    "elapsed_seconds": time.time() - start_time}
        all_download_results: Dict[Any, List[Dict[str, Any]]] = {}
        failed_challenges = 0

        with thread_local_sessions(master) as get_session:
            with Progress(
                _BrailleSpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=err_console,
                transient=False
            ) as progress:
                task_id = progress.add_task("Đang tải đề & dựng workspace...",
                                            total=len(challenges))

                def process_single_challenge(chall: Challenge) -> tuple:
                    # Session riêng của thread này (copy cookie/header từ master);
                    # DownloadManager gắn với session đó -> an toàn đa luồng.
                    dl_results = PullService._full_process(config, get_session(), chall)
                    return (chall.id, dl_results)

                # Use ThreadPoolExecutor for concurrent challenge downloads
                with ThreadPoolExecutor(max_workers=config.threads) as executor:
                    future_to_chall = {
                        executor.submit(process_single_challenge, chall): chall for chall in challenges
                    }

                    for future in as_completed(future_to_chall):
                        chall = future_to_chall[future]
                        try:
                            chall_id, results = future.result()
                            all_download_results[chall_id] = results
                        except Exception as exc:
                            Logger.error(f"Error processing '{chall.name}': {exc}")
                            all_download_results[chall.id] = []
                            failed_challenges += 1
                        finally:
                            progress.advance(task_id)

            if failed_challenges and failed_challenges == len(challenges):
                PullService._render_total_download_failure(
                    failed_challenges, len(challenges))

        # 5. Generate Top-level Summary
        Logger.info("Generating global SUMMARY.md and challenges.json...")
        summary_file = SummaryGenerator.generate_summary(
            base_output_dir=config.output_dir,
            ctf_info=platform.ctf_info,
            all_results=all_download_results
        )

        elapsed = time.time() - start_time
        total_files = sum(sum(1 for f in res if f.get("success")) for res in all_download_results.values())

        # 6. Sync solve attribution từ server (spec §4): server báo solved mà
        # local chưa → nâng solve + stamp synced_at. KHÔNG BAO GIỜ hạ trạng thái.
        try:
            synced = PullService.sync_solve_attribution(platform, config.output_dir)
            if synced:
                Logger.info(f"🔄 Đã đồng bộ solve attribution cho {synced} challenge(s).")
        except Exception:
            pass

        Logger.success(f"[bold green]✨ ALL DONE in {elapsed:.2f}s! ✨[/bold green]", markup=True)
        Logger.info(f"📁 Workspace: [bold yellow]{escape(config.output_dir)}[/bold yellow]", markup=True)
        Logger.info(f"📊 Summary: [bold cyan]{escape(str(summary_file))}[/bold cyan]", markup=True)
        Logger.info(f"📦 Total files downloaded: [bold green]{total_files}[/bold green]", markup=True)

        # Event Window (spec event-window §4/§6): lần đầu pull thành công mà
        # workspace chưa có .ctf/config.json → chạy wizard 3 câu hỏi (chỉ khi
        # tty) + nhận diện window (platform > CTFtime) + mirror challenges.json.
        try:
            from .watch_service import maybe_run_event_window_wizard
            maybe_run_event_window_wizard(config.output_dir, platform=platform)
        except Exception:
            pass

        return {
            "ok": True,
            "output_dir": config.output_dir,
            "summary_file": summary_file,
            "total_files": total_files,
            "challenges_processed": len(all_download_results),
            "elapsed_seconds": elapsed,
        }

    # ------------------------------------------------------------------ #
    # Full processing pipeline cho 1 challenge (dùng chung full-pull và
    # incremental --update/--refresh-meta cho challenge MỚI / cần tải lại)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _full_process(config: DownloaderConfig,
                      download_session: Any,
                      chall: Challenge) -> List[Dict[str, Any]]:
        """Extract links → tải attachment → dựng workspace cho 1 challenge.

        ``download_session`` là session RIÊNG của thread gọi hàm này (xem
        ``thread_local_sessions``). Trả về danh sách download result dict.
        LƯU Ý: đường này đi qua ``WorkspaceBuilder.create_challenge_workspace``
        vốn GHI ĐÈ metadata.json — chỉ dùng cho challenge mới hoặc khi caller
        đã snapshot/phục hồi các field user-owned (status, submitted_flag, ...).
        """
        download_manager = DownloadManager(
            session=download_session,
            timeout=config.timeout,
            force=config.force_redownload,
            size_limit_bytes=config.size_limit_bytes
        )

        # Extract links & connection info
        combined_text = f"{chall.description}\n{chall.connection_info or ''}"
        extracted_links = LinkExtractor.extract_links_and_files(combined_text, base_url=config.url)
        connections = LinkExtractor.extract_connection_info(combined_text)

        # Determine challenge directory to place files
        from ..utils.sanitize import sanitize_folder_name
        clean_category = sanitize_folder_name(chall.category, default="Misc")
        clean_name = sanitize_folder_name(chall.name, default=f"chall_{chall.id}")
        chall_dest_dir = os.path.join(config.output_dir, clean_category, clean_name)
        challenge_sub_dir = os.path.join(chall_dest_dir, "challenge")
        os.makedirs(challenge_sub_dir, exist_ok=True)

        # Download files directly into challenge/ subdirectory
        dl_results = download_manager.download_challenge_files(
            files=chall.files,
            extracted_links=extracted_links,
            dest_dir=challenge_sub_dir,
            download_third_party=config.download_third_party
        )

        # Build workspace
        WorkspaceBuilder.create_challenge_workspace(
            base_output_dir=config.output_dir,
            challenge=chall,
            extracted_links=extracted_links,
            connections=connections,
            download_results=dl_results,
            create_solve_template=config.create_solve_template
        )

        return dl_results

    # ------------------------------------------------------------------ #
    # Sync solve attribution (spec challenge-status-model §4)
    # ------------------------------------------------------------------ #
    @staticmethod
    def sync_solve_attribution(platform: Any, output_dir: str,
                               on_error: Optional[Callable[[str], None]] = None
                               ) -> int:
        """Hỏi platform ``fetch_solve_attribution`` cho mọi challenge local và
        nâng trạng thái solve theo nguyên tắc chỉ-nâng. Trả về số challenge
        được cập nhật. Platform không hỗ trợ → 0.

        ``on_error``: callback tùy chọn nhận mô tả lỗi khi fetch raise —
        caller (vd. watch tick) dùng để log cảnh báo; mặc định None giữ
        hành vi cũ im-lặng-và-0."""
        from ..storage.workspace_repo import WorkspaceRepo

        repo = WorkspaceRepo(output_dir)
        fetcher = getattr(platform, "fetch_solve_attribution", None)
        if not callable(fetcher):
            return 0

        metas = []
        for meta_path in repo.iter_challenges():
            m = repo.read_metadata(meta_path)
            if m and m.get("id") is not None:
                metas.append((meta_path, m))
        if not metas:
            return 0

        try:
            attr_map = fetcher([m.get("id") for _p, m in metas]) or {}
        except Exception as exc:
            if on_error is not None:
                try:
                    on_error(f"fetch_solve_attribution: {exc}")
                except Exception:
                    pass
            return 0
        if not isinstance(attr_map, dict):
            return 0

        updated = 0
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        for meta_path, _m in metas:
            attr = attr_map.get(_m.get("id"))
            if attr is None:
                continue
            if not isinstance(attr, dict):
                # SolveAttribution dataclass (hoặc obj tương đương) → dict
                attr = {"by_me": bool(getattr(attr, "by_me", False)),
                        "by_team": bool(getattr(attr, "by_team", False))}
            target = ("solved_by_me" if attr.get("by_me", False)
                      else "solved_by_team" if attr.get("by_team", False)
                      else "solved_other")

            # Review finding: chỉ GHI khi solve rank thực sự được nâng —
            # fetch trả cache cũ giống hệt (TTL chưa hết) thì không đụng
            # status.json, không stamp synced_at giả "tươi" mỗi tick.
            try:
                before_solve = repo.read_status(meta_path)["solve"]
            except Exception:
                continue
            if SOLVE_RANK.get(target, 0) <= SOLVE_RANK.get(before_solve, 0):
                continue

            def _mut(st, _target=target):
                if SOLVE_RANK.get(_target, 0) > SOLVE_RANK.get(st["solve"], 0):
                    st["solve"] = _target
                    # Stamp synced_at CHỈ khi dữ liệu thật sự thay đổi.
                    st["synced_at"] = now_str
                return st

            try:
                after = repo.update_status(meta_path, _mut)["solve"]
                if after != before_solve:
                    updated += 1
            except Exception:
                continue
        return updated

    # ------------------------------------------------------------------ #
    # Incremental pull (--update / --refresh-meta)
    # ------------------------------------------------------------------ #
    # Field do instance_service quản lý TRÊN ĐỊA — platform KHÔNG được đè
    # khi cập nhật metadata động của challenge đã có.
    _LOCAL_INSTANCE_KEYS = ("is_container", "status", "active_instance",
                            "last_entry", "remaining_time", "last_updated")
    # Field user-owned trong metadata.json mà incremental update không bao
    # giờ ghi đè (phục hồi sau khi WorkspaceBuilder viết lại toàn bộ file).
    # ``instance_info`` do instance_service quản TRÊN ĐỊA (is_container/
    # active_instance/remaining_time) — platform không biết gì về trạng thái
    # container local nên refresh-meta/redownload phải giữ nguyên (C9-03).
    _USER_OWNED_META_KEYS = ("status", "submitted_flag", "instance_info")

    @staticmethod
    def run_update(config: DownloaderConfig,
                   session: Optional[Any] = None,
                   refresh_meta: bool = False) -> Dict[str, Any]:
        """Pull tăng dần: chỉ xử lý đầy đủ challenge MỚI, các challenge đã có
        chỉ cập nhật metadata động (points/solves/connection/instance + solve
        attribution raise-only). ``refresh_meta=True`` cho phép tải lại
        attachment của challenge đã có khi file thiếu trên đĩa.

        Challenge biến mất khỏi API: giữ nguyên local, đánh dấu
        ``removed_from_server=true`` (ghi cả trong block ``status`` lẫn mirror
        top-level metadata.json — block status bị normalize_status cắt field
        lạ ở lần ``update_status`` kế tiếp nên mirror top-level mới bền).
        """
        # Cho phép bật chế độ qua config (--refresh-meta/--update từ CLI)
        # thay vì bắt buộc truyền kwarg.
        refresh_meta = bool(refresh_meta or getattr(config, "refresh_meta", False))
        config.validate()
        start_time = time.time()
        Logger.banner()
        mode_label = "--refresh-meta" if refresh_meta else "--update"
        Logger.info(f"Incremental pull ({mode_label}): "
                    f"[bold blue]{escape(config.url)}[/bold blue]", markup=True)

        master = session or create_session(
            cookie=config.cookie,
            token=config.token,
            custom_headers=config.custom_headers,
            timeout=config.timeout,
        )

        # 1. Detect + Authenticate + Fetch (giống full pull)
        try:
            platform = PlatformDetector.detect_platform(config.url, master)
        except Exception as exc:
            return PullService._render_detect_failure(config, start_time, exc)
        if not platform.authenticate():
            PullService._render_auth_warning()
        challenges = PullService._fetch_challenges_ui(platform)
        if not challenges:
            PullService._render_no_challenges()
            return {"ok": False, "output_dir": config.output_dir,
                    "summary_file": None, "total_files": 0,
                    "new": 0, "updated": 0, "skipped": 0, "missing": 0,
                    "challenges_processed": 0,
                    "elapsed_seconds": time.time() - start_time}

        from ..utils.sanitize import sanitize_ctf_title
        if not config.output_dir:
            ctf_title = platform.ctf_info.title or ""
            folder_name = sanitize_ctf_title(ctf_title, fallback_domain=config.url)
            base_ctf_dir = os.path.expanduser("~/Workspace/CTF")
            config.output_dir = os.path.abspath(os.path.join(base_ctf_dir, folder_name))
        output_dir = config.output_dir
        try:
            os.makedirs(output_dir, exist_ok=True)
        except OSError as exc:
            PullService._render_workspace_write_failure(exc)
            return {"ok": False, "output_dir": output_dir,
                    "summary_file": None, "total_files": 0,
                    "new": 0, "updated": 0, "skipped": 0, "missing": 0,
                    "challenges_processed": 0,
                    "elapsed_seconds": time.time() - start_time}

        # generate_summary duyệt ctf_info.challenges — platform thật tự gắn;
        # platform giả/mock có thể bỏ trống nên bảo đảm danh sách khớp API.
        try:
            if not list(getattr(platform.ctf_info, "challenges", None) or []):
                platform.ctf_info.challenges = list(challenges)
        except Exception:
            pass

        # 2. Phân loại: new / existing / missing (đều tôn trọng filter category)
        def _in_scope(category: Any) -> bool:
            cat = str(category or "").lower()
            if config.categories and cat not in [c.lower() for c in config.categories]:
                return False
            if config.exclude_categories and cat in [c.lower() for c in config.exclude_categories]:
                return False
            return True

        scoped = [c for c in challenges if _in_scope(c.category)]

        from ..storage.workspace_repo import WorkspaceRepo
        repo = WorkspaceRepo(output_dir)
        local_index: Dict[str, tuple] = {}
        for meta_path in repo.iter_challenges():
            m = repo.read_metadata(meta_path)
            cid = m.get("id")
            if cid is not None:
                local_index.setdefault(str(cid), (meta_path, m))

        api_ids = {str(c.id) for c in scoped}
        new_challs = [c for c in scoped if str(c.id) not in local_index]
        existing_pairs = [(c,) + local_index[str(c.id)]
                          for c in scoped if str(c.id) in local_index]
        missing_items = [(cid, mp, (_m.get("name") or str(cid)))
                         for cid, (mp, _m) in local_index.items()
                         if cid not in api_ids and _in_scope(_m.get("category"))]

        # --refresh-meta: challenge đã có nhưng attachment thiếu trên đĩa →
        # đưa vào hàng tải lại (full pipeline).
        redownload: List[Challenge] = []
        if refresh_meta:
            for chall, _mp, m in existing_pairs:
                for df in (m.get("downloaded_files") or []):
                    sp = df.get("saved_path") if isinstance(df, dict) else None
                    if df.get("success") and sp and not os.path.isfile(sp):
                        redownload.append(chall)
                        break

        all_results: Dict[Any, List[Dict[str, Any]]] = {}
        failed_downloads = 0

        # 3. Full pipeline (threaded) cho challenge mới + cần tải lại
        to_download = [(c, "new") for c in new_challs] + \
                      [(c, "redownload") for c in redownload]
        fresh_ids: set = set()
        if to_download:
            with thread_local_sessions(master) as get_session:
                with Progress(
                    _BrailleSpinnerColumn(),
                    TextColumn("[progress.description]{task.description}"),
                    BarColumn(),
                    MofNCompleteColumn(),
                    TimeElapsedColumn(),
                    console=err_console,
                    transient=False
                ) as progress:
                    task_id = progress.add_task("Đang tải đề & dựng workspace...",
                                                total=len(to_download))

                    def _one(item):
                        chall, _kind = item
                        return PullService._full_process(config, get_session(), chall)

                    with ThreadPoolExecutor(max_workers=max(1, config.threads)) as executor:
                        future_map = {executor.submit(_one, item): item
                                      for item in to_download}
                        for future in as_completed(future_map):
                            chall, kind = future_map[future]
                            try:
                                dl_results = future.result()
                                all_results[chall.id] = dl_results
                                fresh_ids.add(str(chall.id))
                                if kind == "redownload":
                                    # Builder vừa viết lại metadata.json — phục hồi
                                    # các field user-owned từ snapshot trước đó.
                                    pass   # snapshot merge làm dưới bước 4
                            except Exception as exc:
                                Logger.error(f"Error processing '{chall.name}': {exc}")
                                all_results[chall.id] = []
                                failed_downloads += 1
                            finally:
                                progress.advance(task_id)

            if failed_downloads and failed_downloads == len(to_download):
                PullService._render_total_download_failure(
                    failed_downloads, len(to_download))

        # 4. Cập nhật metadata động cho mọi challenge đã có (sequential, rẻ)
        attr_map = PullService._fetch_attribution_map(
            platform, [c.id for c, _mp, _m in existing_pairs])
        updated = skipped = 0
        for chall, mp, old_meta in existing_pairs:
            was_fresh = str(chall.id) in fresh_ids
            if was_fresh:
                # Tải lại: builder đã ghi metadata mới; khôi phục field user-owned.
                PullService._restore_user_fields(repo, mp, old_meta)
            changed = PullService._refresh_existing_metadata(repo, mp, chall, attr_map)
            updated += 1 if (changed or was_fresh) else 0
            skipped += 0 if (changed or was_fresh) else 1
            # all_results cho summary: kết quả tươi nếu vừa tải, nếu không giữ
            # downloaded_files hiện có trong metadata.
            if not was_fresh:
                cur = repo.read_metadata(mp)
                all_results[chall.id] = cur.get("downloaded_files") or []

        # 5. Challenge biến mất khỏi API: đánh dấu, KHÔNG xoá gì
        for _cid, mp, _name in missing_items:
            PullService._mark_removed_from_server(repo, mp)

        # Tổng kết diff (alphabetical): ` + name` green — bài mới,
        # ` - name` red — bài biến mất khỏi server (removed_from_server).
        diff_entries = [(c.name or str(c.id), "+") for c in new_challs] \
            + [(_name, "-") for _cid, _mp, _name in missing_items]
        for name, sign in sorted(diff_entries, key=lambda e: (e[0].lower(), e[1])):
            style = "green" if sign == "+" else "red"
            err_console.print(f"[{style}]{sign} {name}[/{style}]")

        # 6. Regenerate SUMMARY.md + challenges.json phản ánh danh sách mới
        Logger.info("Regenerating global SUMMARY.md and challenges.json...")
        summary_file = SummaryGenerator.generate_summary(
            base_output_dir=output_dir,
            ctf_info=platform.ctf_info,
            all_results=all_results
        )

        elapsed = time.time() - start_time

        rows = [["new", str(len(new_challs))],
                ["updated", str(updated)],
                ["skipped", str(skipped)],
                ["missing", str(len(missing_items))]]
        Logger.print_table(f"Incremental Update ({mode_label})",
                           ["Metric", "Count"], rows)
        Logger.success(f"📊 new={len(new_challs)} updated={updated} "
                       f"skipped={skipped} missing={len(missing_items)}")

        # Event Window wizard: tự skip nếu workspace đã có .ctf/config.json
        # (run_event_window_wizard trả None ngay khi store.exists()).
        try:
            from .watch_service import maybe_run_event_window_wizard
            maybe_run_event_window_wizard(output_dir, platform=platform)
        except Exception:
            pass

        return {
            "ok": True,
            "output_dir": output_dir,
            "summary_file": summary_file,
            "total_files": sum(
                sum(1 for f in res if f.get("success"))
                for res in all_results.values()),
            "challenges_processed": len(all_results),
            "new": len(new_challs),
            "updated": updated,
            "skipped": skipped,
            "missing": len(missing_items),
            "elapsed_seconds": elapsed,
        }

    # ------------------------------------------------------------------ #
    # Helpers incremental update
    # ------------------------------------------------------------------ #
    @staticmethod
    def _fetch_attribution_map(platform: Any, ids: List[Any]) -> Dict[Any, Any]:
        """Gọi ``fetch_solve_attribution`` nếu platform hỗ trợ; lỗi → {}."""
        fetcher = getattr(platform, "fetch_solve_attribution", None)
        if not callable(fetcher) or not ids:
            return {}
        try:
            attr_map = fetcher(ids) or {}
        except Exception:
            return {}
        return attr_map if isinstance(attr_map, dict) else {}

    @staticmethod
    def _refresh_existing_metadata(repo: Any, meta_path: Any, chall: Challenge,
                                   attr_map: Dict[Any, Any]) -> bool:
        """Cập nhật metadata ĐỘNG của 1 challenge đã có qua WorkspaceRepo
        (atomic + flock): points/solves_count/connection_info/instance_info/
        submit_endpoint + solved state raise-only. KHÔNG đụng solver/writeup/
        README/status user. Trả về True nếu có gì đó thực sự thay đổi."""
        changed = [False]
        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

        def _mut(meta: dict) -> dict:
            meta = dict(meta or {})
            dynamic = {
                "points": chall.points,
                "solves_count": chall.solves_count,
                "connection_info": chall.connection_info,
                "submit_endpoint": chall.submit_endpoint,
            }
            for k, v in dynamic.items():
                if meta.get(k) != v:
                    meta[k] = v
                    changed[0] = True
            # instance_info: merge platform keys nhưng GIỮ nguyên trạng thái
            # container do instance_service quản trên địa.
            plat_inst = chall.instance_info if isinstance(chall.instance_info, dict) else {}
            inst = dict(meta.get("instance_info") or {})
            for k, v in plat_inst.items():
                if k in PullService._LOCAL_INSTANCE_KEYS:
                    continue
                if inst.get(k) != v:
                    inst[k] = v
                    changed[0] = True
            if inst:
                meta["instance_info"] = inst
            # Challenge trở lại sau khi từng bị mark removed → gỡ flag.
            if meta.pop("removed_from_server", None) is not None:
                changed[0] = True
            st = meta.get("status")
            if isinstance(st, dict) and st.pop("removed_from_server", None) is not None:
                meta["status"] = st
                changed[0] = True
            return meta

        try:
            repo.update_metadata(meta_path, _mut)
        except Exception:
            return False

        # Solved state raise-only (spec §4) qua fetch_solve_attribution.
        attr = attr_map.get(chall.id)
        if attr is not None:
            if not isinstance(attr, dict):
                attr = {"by_me": bool(getattr(attr, "by_me", False)),
                        "by_team": bool(getattr(attr, "by_team", False))}
            target = ("solved_by_me" if attr.get("by_me", False)
                      else "solved_by_team" if attr.get("by_team", False)
                      else "solved_other")

            def _st_mut(st: dict) -> dict:
                if SOLVE_RANK.get(target, 0) > SOLVE_RANK.get(st["solve"], 0):
                    st["solve"] = target
                    changed[0] = True
                st["synced_at"] = now_str
                return st

            try:
                repo.update_status(meta_path, _st_mut)
            except Exception:
                pass

        return changed[0]

    @staticmethod
    def _mark_removed_from_server(repo: Any, meta_path: Any) -> None:
        """Đánh dấu challenge biến mất khỏi API: ``status.removed_from_server``
        + mirror top-level (mirror là bản bền — normalize_status cắt field lạ
        trong block status ở lần update_status kế tiếp). Không xoá gì."""

        def _mut(meta: dict) -> dict:
            meta = dict(meta or {})
            meta["removed_from_server"] = True
            st = dict(meta.get("status") or {}) if isinstance(meta.get("status"), dict) else {}
            st["removed_from_server"] = True
            meta["status"] = st
            return meta

        try:
            repo.update_metadata(meta_path, _mut)
        except Exception:
            pass

    @staticmethod
    def _restore_user_fields(repo: Any, meta_path: Any, snapshot: dict) -> None:
        """Sau khi WorkspaceBuilder viết lại TOÀN BỘ metadata.json (full pipeline),
        khôi phục các field user-owned (status, submitted_flag, ...) từ snapshot
        metadata đọc TRƯỚC khi tải lại — giữ flag/solve/writeup state của user."""

        def _mut(meta: dict) -> dict:
            meta = dict(meta or {})
            snap = snapshot if isinstance(snapshot, dict) else {}
            for k in PullService._USER_OWNED_META_KEYS:
                if k in snap and snap[k] is not None:
                    meta[k] = snap[k]
            return meta

        try:
            repo.update_metadata(meta_path, _mut)
        except Exception:
            pass

    # ------------------------------------------------------------------ #
    # Sync 2 chiều (backlog P2-1): re-fetch metadata từ platform GIỮ NGUYÊN
    # local state + verify drift solve. KHÔNG wire CLI trong backlog này —
    # xem docstring ``sync_workspace`` cho cách gọi sau khi cli.py sẵn sàng.
    # ------------------------------------------------------------------ #
    @staticmethod
    def sync_workspace(repo: Any, platform: Any) -> Dict[str, Any]:
        """Đồng bộ 2 chiều giữa workspace local và platform (backlog P2-1).

        Nguyên tắc: LOCAL STATE LÀ CHỦ. Với mỗi challenge đã có local, chỉ
        merge metadata ĐỘNG từ server (points/solves_count/connection_info/
        submit_endpoint/instance_info) + stamp ``status.synced_at``; giữ nguyên
        TUYỆT ĐỐI block ``status`` (trừ synced_at), ``submitted_flag`` và mọi
        file trong ``challenge/``, ``solver/``, ``writeup/`` (không tải lại,
        không dựng lại gì).

        - Challenge MỚI trên server: chỉ liệt kê vào ``new_on_server`` —
          KHÔNG tự tạo workspace (user chạy ``--update`` để pull tăng dần).
        - Drift solve (server báo solved mà local chưa): KHÔNG tự đổi trạng
          thái — ``PullService.verify`` liệt kê kèm tên người giải để user
          tự quyết.

        Cách gọi sau khi CLI được wire (cli.py do agent khác sở hữu)::

            from ctf_downloader.storage.workspace_repo import WorkspaceRepo
            repo = WorkspaceRepo(output_dir)
            result = PullService.sync_workspace(repo, platform)
            # result: {"ok", "updated", "new", "new_on_server", "drift",
            #          "unsolved_locally_solved_remotely", "total_local",
            #          "total_server"} — drift == unsolved_locally_solved_
            #          remotely (danh sách chi tiết từng bài lệch solve).

        Kết quả được in dạng bảng: updated=N · new=X · drift=Y (+ chi tiết
        từng bài drift).
        """
        fetcher = getattr(platform, "fetch_challenges", None)
        challenges: List[Any] = []
        if callable(fetcher):
            try:
                challenges = list(fetcher() or [])
            except Exception:
                challenges = []
        if not challenges:
            return {"ok": False, "updated": 0, "new": 0, "new_on_server": [],
                    "drift": [], "unsolved_locally_solved_remotely": [],
                    "total_local": 0, "total_server": 0}

        local_index: Dict[str, tuple] = {}
        for meta_path in repo.iter_challenges():
            m = repo.read_metadata(meta_path)
            cid = m.get("id")
            if cid is not None:
                local_index.setdefault(str(cid), (meta_path, m))

        now_str = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        new_on_server: List[Dict[str, Any]] = []
        updated = 0

        for chall in challenges:
            entry = local_index.get(str(chall.id))
            if entry is None:
                # Challenge mới trên server: KHÔNG tạo gì — để --update xử lý.
                new_on_server.append({"id": chall.id, "name": chall.name,
                                      "category": chall.category})
                continue
            meta_path, _snapshot = entry
            if PullService._merge_dynamic_metadata(repo, meta_path, chall):
                updated += 1
            # Stamp synced_at — field DUY NHẤT của block status bị đụng tới;
            # solve/flag/notes/labels/writeup giữ nguyên.
            try:
                repo.update_status(
                    meta_path, lambda st: {**st, "synced_at": now_str})
            except Exception:
                pass

        verdict = PullService.verify(repo, platform)
        drift = verdict["unsolved_locally_solved_remotely"]

        result = {
            "ok": True,
            "updated": updated,
            "new": len(new_on_server),
            "new_on_server": new_on_server,
            "drift": drift,
            "unsolved_locally_solved_remotely": drift,
            "total_local": len(local_index),
            "total_server": len(challenges),
        }

        # Bảng kết quả: updated=N · new=X · drift=Y (+ chi tiết drift).
        Logger.print_table("Workspace Sync", ["Metric", "Count"],
                           [["updated", str(updated)],
                            ["new", str(len(new_on_server))],
                            ["drift", str(len(drift))]])
        if new_on_server:
            Logger.info("🆕 Challenge mới trên server (chạy --update để tải): "
                        + ", ".join(str(c["name"]) for c in new_on_server))
        if drift:
            d_rows = [[f"{d.get('name')} ({d.get('category')})",
                       "me" if d["by_me"] else "team",
                       ", ".join(d["solver_names"]) or "(không rõ)"]
                      for d in drift]
            Logger.print_table("Drift — solved trên server, local chưa",
                               ["Challenge", "By", "Solvers"], d_rows)
            Logger.warning("⚠️ KHÔNG tự đổi trạng thái — user quyết định qua "
                           "'status set' hoặc submit flag.")
        return result

    @staticmethod
    def verify(repo: Any, platform: Any) -> Dict[str, Any]:
        """So trạng thái solved-local vs server attribution (spec §4, P2-1).

        Challenge server báo đã giải (``by_me``/``by_team``) mà local còn ở
        mức dưới ``solved_other`` (unsolved/working) → liệt kê vào
        ``unsolved_locally_solved_remotely`` kèm tên người giải lấy từ
        ``SolveAttribution.solver_names``. KHÔNG BAO GIỜ tự sửa trạng thái —
        user quyết. Platform không hỗ trợ ``fetch_solve_attribution`` → rỗng.

        Returns:
            {"ok", "checked", "unsolved_locally_solved_remotely": [
                {"id", "name", "category", "by_me", "by_team",
                 "solver_names", "local_solve", "path"}]}
        """
        empty = {"ok": True, "checked": 0, "unsolved_locally_solved_remotely": []}
        metas: List[tuple] = []
        for meta_path in repo.iter_challenges():
            m = repo.read_metadata(meta_path)
            if m and m.get("id") is not None:
                metas.append((meta_path, m))
        if not metas:
            return empty

        attr_map = PullService._fetch_attribution_map(
            platform, [m.get("id") for _p, m in metas])
        if not attr_map:
            return {"ok": True, "checked": len(metas),
                    "unsolved_locally_solved_remotely": []}

        threshold = SOLVE_RANK.get("solved_other", 2)
        drift: List[Dict[str, Any]] = []
        for meta_path, m in metas:
            raw = attr_map.get(m.get("id"))
            if raw is None:
                continue
            if isinstance(raw, dict):
                attr = {"by_me": bool(raw.get("by_me")),
                        "by_team": bool(raw.get("by_team")),
                        "solver_names": list(raw.get("solver_names") or [])}
            else:
                # SolveAttribution dataclass (hoặc obj tương đương) → dict
                attr = {"by_me": bool(getattr(raw, "by_me", False)),
                        "by_team": bool(getattr(raw, "by_team", False)),
                        "solver_names": list(getattr(raw, "solver_names", None) or [])}
            if not (attr["by_me"] or attr["by_team"]):
                continue
            try:
                st = repo.read_status(meta_path)
            except Exception:
                continue
            if SOLVE_RANK.get(st.get("solve"), 0) >= threshold:
                continue   # local đã biết là solved (self/team/other) — không drift
            drift.append({"id": m.get("id"), "name": m.get("name"),
                          "category": m.get("category"),
                          "by_me": attr["by_me"], "by_team": attr["by_team"],
                          "solver_names": attr["solver_names"],
                          "local_solve": st.get("solve"),
                          "path": str(meta_path)})

        return {"ok": True, "checked": len(metas),
                "unsolved_locally_solved_remotely": drift}

    @staticmethod
    def _merge_dynamic_metadata(repo: Any, meta_path: Any, chall: Challenge) -> bool:
        """Merge metadata ĐỘNG của một challenge đã có (P2-1): points/
        solves_count/connection_info/submit_endpoint + instance_info (bỏ qua
        các key local-owned) qua ``repo.update_metadata`` (atomic + flock).
        KHÔNG đụng block ``status``, ``submitted_flag`` hay bất kỳ file nào.
        Trả về True nếu có gì thực sự thay đổi."""
        changed = [False]

        def _mut(meta: dict) -> dict:
            meta = dict(meta or {})
            for k, v in (("points", chall.points),
                         ("solves_count", chall.solves_count),
                         ("connection_info", chall.connection_info),
                         ("submit_endpoint", chall.submit_endpoint)):
                if meta.get(k) != v:
                    meta[k] = v
                    changed[0] = True
            # instance_info: merge key platform nhưng GIỮ trạng thái container
            # do instance_service quản trên địa.
            plat_inst = chall.instance_info if isinstance(chall.instance_info, dict) else {}
            inst = dict(meta.get("instance_info") or {})
            for k, v in plat_inst.items():
                if k in PullService._LOCAL_INSTANCE_KEYS:
                    continue
                if inst.get(k) != v:
                    inst[k] = v
                    changed[0] = True
            if inst:
                meta["instance_info"] = inst
            # Challenge trở lại server sau khi từng mark removed → gỡ flag.
            if meta.pop("removed_from_server", None) is not None:
                changed[0] = True
            st = meta.get("status")
            if isinstance(st, dict) and st.pop("removed_from_server", None) is not None:
                meta["status"] = st
                changed[0] = True
            return meta

        try:
            repo.update_metadata(meta_path, _mut)
        except Exception:
            return False
        return changed[0]
