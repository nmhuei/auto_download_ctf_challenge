import os
import time
from typing import List, Dict, Any, Optional
from concurrent.futures import ThreadPoolExecutor, as_completed
from rich.progress import Progress, SpinnerColumn, BarColumn, TextColumn, TimeElapsedColumn, MofNCompleteColumn

from .config import DownloaderConfig
from .utils.logger import Logger, console
from .utils.http_client import create_session
from .platforms.detector import PlatformDetector
from .platforms.base import Challenge, CTFInfo
from .extractors.link_extractor import LinkExtractor
from .downloaders.manager import DownloadManager
from .generator.workspace_builder import WorkspaceBuilder
from .generator.summary_generator import SummaryGenerator

class CTFDownloader:
    def __init__(self, config: DownloaderConfig):
        self.config = config
        self.config.validate()
        self.session = create_session(
            cookie=config.cookie,
            token=config.token,
            custom_headers=config.custom_headers,
            timeout=config.timeout
        )
        self.download_manager = DownloadManager(
            session=self.session,
            timeout=config.timeout,
            force=config.force_redownload,
            size_limit_bytes=config.size_limit_bytes
        )

    def run(self) -> bool:
        start_time = time.time()
        Logger.banner()
        Logger.info(f"Target URL: [bold blue]{self.config.url}[/bold blue]")

        # 1. Detect Platform
        platform = PlatformDetector.detect_platform(self.config.url, self.session)
        
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
            return False

        # Auto-determine output_dir under ~/Workspace/CTF/<CTF_Title> if not explicitly specified
        from .utils.sanitize import sanitize_ctf_title
        if not self.config.output_dir:
            ctf_title = platform.ctf_info.title or ""
            folder_name = sanitize_ctf_title(ctf_title, fallback_domain=self.config.url)
            base_ctf_dir = os.path.expanduser("~/Workspace/CTF")
            self.config.output_dir = os.path.abspath(os.path.join(base_ctf_dir, folder_name))

        Logger.info(f"Output Directory: [bold yellow]{self.config.output_dir}[/bold yellow]")

        # Filter categories if specified
        if self.config.categories:
            cats = [c.lower() for c in self.config.categories]
            challenges = [c for c in challenges if c.category.lower() in cats]

            
        if self.config.exclude_categories:
            ex_cats = [c.lower() for c in self.config.exclude_categories]
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
        os.makedirs(self.config.output_dir, exist_ok=True)
        all_download_results: Dict[Any, List[Dict[str, Any]]] = {}

        Logger.info(f"Starting workspace build & asset downloads (Threads: {self.config.threads})...")

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
                # Extract links & connection info
                combined_text = f"{chall.description}\n{chall.connection_info or ''}"
                extracted_links = LinkExtractor.extract_links_and_files(combined_text, base_url=self.config.url)
                connections = LinkExtractor.extract_connection_info(combined_text)

                # Determine challenge directory to place files
                from .utils.sanitize import sanitize_folder_name
                clean_category = sanitize_folder_name(chall.category, default="Misc")
                clean_name = sanitize_folder_name(chall.name, default=f"chall_{chall.id}")
                chall_dest_dir = os.path.join(self.config.output_dir, clean_category, clean_name)
                challenge_sub_dir = os.path.join(chall_dest_dir, "challenge")
                os.makedirs(challenge_sub_dir, exist_ok=True)

                # Download files directly into challenge/ subdirectory
                dl_results = self.download_manager.download_challenge_files(
                    files=chall.files,
                    extracted_links=extracted_links,
                    dest_dir=challenge_sub_dir,
                    download_third_party=self.config.download_third_party
                )

                # Build workspace
                WorkspaceBuilder.create_challenge_workspace(
                    base_output_dir=self.config.output_dir,
                    challenge=chall,
                    extracted_links=extracted_links,
                    connections=connections,
                    download_results=dl_results,
                    create_solve_template=self.config.create_solve_template
                )

                return (chall.id, dl_results)

            # Use ThreadPoolExecutor for concurrent challenge downloads
            with ThreadPoolExecutor(max_workers=self.config.threads) as executor:
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
            base_output_dir=self.config.output_dir,
            ctf_info=platform.ctf_info,
            all_results=all_download_results
        )

        elapsed = time.time() - start_time
        total_files = sum(sum(1 for f in res if f.get("success")) for res in all_download_results.values())

        Logger.success(f"[bold green]✨ ALL DONE in {elapsed:.2f}s! ✨[/bold green]")
        Logger.info(f"📁 Workspace: [bold yellow]{self.config.output_dir}[/bold yellow]")
        Logger.info(f"📊 Summary: [bold cyan]{summary_file}[/bold cyan]")
        Logger.info(f"📦 Total files downloaded: [bold green]{total_files}[/bold green]")
        return True
