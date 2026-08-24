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
from typing import Any, Dict, List, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed

from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn

from ..config import DownloaderConfig
from ..utils.logger import Logger, console
from ..platforms.detector import PlatformDetector
from ..platforms.base import Challenge
from ..storage.constants import SOLVE_RANK
from ..extractors.link_extractor import LinkExtractor
from ..downloaders.manager import DownloadManager
from ..generator.workspace_builder import WorkspaceBuilder
from ..generator.summary_generator import SummaryGenerator
from .session_factory import create_session, thread_local_sessions


class PullService:
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
        Logger.info(f"Target URL: [bold blue]{config.url}[/bold blue]")

        # Session master: chỉ main thread dùng (detect platform + authenticate)
        master = session or create_session(
            cookie=config.cookie,
            token=config.token,
            custom_headers=config.custom_headers,
            timeout=config.timeout,
        )

        # 1. Detect Platform
        platform = PlatformDetector.detect_platform(config.url, master)

        # 2. Authenticate
        Logger.info("Authenticating with platform...")
        auth_success = platform.authenticate()
        if not auth_success:
            Logger.warning("Authentication failed or proceeding as unauthenticated guest.")

        # 3. Fetch Challenges
        Logger.info("Fetching challenge lists and details...")
        challenges = platform.fetch_challenges()
        if not challenges:
            Logger.error("No challenges found or could not access challenge list. Please check your cookies/token/URL.")
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

        Logger.info(f"Output Directory: [bold yellow]{config.output_dir}[/bold yellow]")

        # Filter categories if specified
        if config.categories:
            cats = [c.lower() for c in config.categories]
            challenges = [c for c in challenges if c.category.lower() in cats]

        if config.exclude_categories:
            ex_cats = [c.lower() for c in config.exclude_categories]
            challenges = [c for c in challenges if c.category.lower() not in ex_cats]

        # Display found summary
        Logger.success(f"Successfully retrieved [bold green]{len(challenges)}[/bold green] challenges!")

        # Create summary table of challenges found
        categories_dict = {}
        for c in challenges:
            categories_dict[c.category] = categories_dict.get(c.category, 0) + 1

        rows = [[cat, str(count)] for cat, count in sorted(categories_dict.items())]
        Logger.print_table("CTF Challenges Overview", ["Category", "Count"], rows)

        # 4. Process Each Challenge
        os.makedirs(config.output_dir, exist_ok=True)
        all_download_results: Dict[Any, List[Dict[str, Any]]] = {}

        Logger.info(f"Starting workspace build & asset downloads (Threads: {config.threads})...")

        with thread_local_sessions(master) as get_session:
            with Progress(
                SpinnerColumn(),
                TextColumn("[progress.description]{task.description}"),
                BarColumn(),
                MofNCompleteColumn(),
                TimeElapsedColumn(),
                console=console
            ) as progress:
                task_id = progress.add_task("[cyan]Processing Challenges...", total=len(challenges))

                def process_single_challenge(chall: Challenge) -> tuple:
                    # Session riêng của thread này (copy cookie/header từ master);
                    # DownloadManager gắn với session đó -> an toàn đa luồng.
                    download_manager = DownloadManager(
                        session=get_session(),
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
                        finally:
                            progress.advance(task_id)

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

        Logger.success(f"[bold green]✨ ALL DONE in {elapsed:.2f}s! ✨[/bold green]")
        Logger.info(f"📁 Workspace: [bold yellow]{config.output_dir}[/bold yellow]")
        Logger.info(f"📊 Summary: [bold cyan]{summary_file}[/bold cyan]")
        Logger.info(f"📦 Total files downloaded: [bold green]{total_files}[/bold green]")

        return {
            "ok": True,
            "output_dir": config.output_dir,
            "summary_file": summary_file,
            "total_files": total_files,
            "challenges_processed": len(all_download_results),
            "elapsed_seconds": elapsed,
        }

    # ------------------------------------------------------------------ #
    # Sync solve attribution (spec challenge-status-model §4)
    # ------------------------------------------------------------------ #
    @staticmethod
    def sync_solve_attribution(platform: Any, output_dir: str) -> int:
        """Hỏi platform ``fetch_solve_attribution`` cho mọi challenge local và
        nâng trạng thái solve theo nguyên tắc chỉ-nâng. Trả về số challenge
        được cập nhật. Platform không hỗ trợ → 0."""
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
        except Exception:
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

            def _mut(st):
                target = ("solved_by_me" if attr.get("by_me", False)
                          else "solved_by_team" if attr.get("by_team", False)
                          else "solved_other")
                if SOLVE_RANK.get(target, 0) > SOLVE_RANK.get(st["solve"], 0):
                    st["solve"] = target
                st["synced_at"] = now_str
                return st

            try:
                before = repo.read_status(meta_path)["solve"]
                after = repo.update_status(meta_path, _mut)["solve"]
                if after != before:
                    updated += 1
            except Exception:
                continue
        return updated
