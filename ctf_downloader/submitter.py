import os
import re
import json
from typing import Optional, Union, Dict, Any, List, Tuple
from rich.prompt import Prompt

from .config import DownloaderConfig
from .utils.logger import Logger, console
from .utils.http_client import create_session
from .platforms.detector import PlatformDetector
from .platforms.base import BasePlatform

class FlagSubmitter:
    def __init__(
        self,
        url: Optional[str] = None,
        cookie: Optional[str] = None,
        token: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        timeout: int = 30
    ):
        self.workspace_dir = os.path.abspath(workspace_dir) if workspace_dir else None
        self.url = url or self._resolve_url_from_workspace()
        self.cookie = cookie
        self.token = token
        self.timeout = timeout

        if not self.url:
            raise ValueError("Platform URL not specified and could not be detected from workspace.")
        
        self.session = create_session(
            cookie=cookie,
            token=token,
            timeout=timeout
        )
        self.platform = PlatformDetector.detect_platform(self.url, self.session)
        self.challenges_cache: Dict[str, Any] = {}
        self._load_challenges()

    def _resolve_url_from_workspace(self) -> Optional[str]:
        if not self.workspace_dir or not os.path.exists(self.workspace_dir):
            return None
        json_path = os.path.join(self.workspace_dir, "challenges.json")
        if os.path.exists(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("platform_url"):
                        return data.get("platform_url")
                    ctf_info = data.get("ctf_info", {})
                    if ctf_info.get("url"):
                        return ctf_info.get("url")
            except Exception:
                pass
        # Search metadata.json in subdirectories
        import glob
        for meta_f in glob.glob(os.path.join(self.workspace_dir, "*", "*", "metadata.json")):
            try:
                with open(meta_f, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if data.get("submit_endpoint"):
                        from urllib.parse import urlparse
                        p = urlparse(data["submit_endpoint"])
                        return f"{p.scheme}://{p.netloc}"
            except Exception:
                pass
        return None
        self.challenges_cache: Dict[str, Any] = {}
        self._load_challenges()

    def _load_challenges(self):
        """
        Loads challenge map from local challenges.json or fetches live.
        """
        # Try local challenges.json first
        if self.workspace_dir:
            json_path = os.path.join(self.workspace_dir, "challenges.json")
            if os.path.exists(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as f:
                        data = json.load(f)
                        for c in data.get("challenges", []):
                            cid = c.get("id")
                            name = c.get("name", "")
                            self.challenges_cache[str(cid)] = c
                            self.challenges_cache[name.lower().strip()] = c
                        return
                except Exception:
                    pass

        # Fetch live if not in local cache
        try:
            self.platform.authenticate()
            challs = self.platform.fetch_challenges()
            for c in challs:
                self.challenges_cache[str(c.id)] = {"id": c.id, "name": c.name, "category": c.category}
                self.challenges_cache[c.name.lower().strip()] = {"id": c.id, "name": c.name, "category": c.category}
        except Exception as e:
            Logger.warning(f"Could not load challenges list: {e}")

    def resolve_challenge_id(self, identifier: Union[int, str]) -> Tuple[Optional[Any], str]:
        """
        Resolves challenge ID and Name from ID or Challenge Name string.
        """
        if isinstance(identifier, int) or str(identifier).isdigit():
            cid = int(identifier)
            c_info = self.challenges_cache.get(str(cid), {})
            name = c_info.get("name", f"Challenge_{cid}")
            return cid, name

        ident_str = str(identifier).lower().strip()
        if ident_str in self.challenges_cache:
            c = self.challenges_cache[ident_str]
            return c.get("id"), c.get("name", str(identifier))

        # Partial match
        for key, val in self.challenges_cache.items():
            if ident_str in key:
                return val.get("id"), val.get("name", str(identifier))

        return identifier, str(identifier)

    def submit(self, challenge_identifier: Union[int, str], flag: str) -> Tuple[bool, str]:
        """
        Submits a flag for a given challenge.
        """
        flag = flag.strip()
        if not flag:
            return False, "Flag cannot be empty."

        cid, name = self.resolve_challenge_id(challenge_identifier)
        if cid is None:
            return False, f"Could not resolve challenge: '{challenge_identifier}'"

        Logger.info(f"Submitting flag for [bold cyan]{name}[/bold cyan] (ID: {cid})...")
        Logger.info(f"Flag: [bold yellow]{flag}[/bold yellow]")

        # Authenticate if needed
        self.platform.authenticate()
        
        success, message = self.platform.submit_flag(cid, flag)
        
        if success:
            Logger.success(f"Result: {message}")
            if self.workspace_dir:
                self._update_local_workspace(cid, name, flag)
        else:
            Logger.error(f"Result: {message}")

        return success, message

    def auto_scan_and_submit(self) -> List[Dict[str, Any]]:
        """
        Scans workspace directory for filled flags in README.md or flag.txt and submits them.
        """
        if not self.workspace_dir or not os.path.exists(self.workspace_dir):
            Logger.error("Workspace directory not found for auto-scan.")
            return []

        Logger.info(f"Scanning workspace for unsubmitted flags: [bold cyan]{self.workspace_dir}[/bold cyan]")
        results = []
        
        # Regex to find candidate flags
        flag_pattern = re.compile(r'(?:PTITCTF|FLAG|CTF|[a-zA-Z0-9_\-]+)\{[a-zA-Z0-9_\-!@#\$%\^&\*\(\)\+=~`|:;<>,\.\?/\\]+\}')

        for root, dirs, files in os.walk(self.workspace_dir):
            if "metadata.json" in files:
                meta_path = os.path.join(root, "metadata.json")
                readme_path = os.path.join(root, "README.md")
                flag_txt_path = os.path.join(root, "flag.txt")
                
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                except Exception:
                    continue

                chall_id = meta.get("id")
                chall_name = meta.get("name")
                is_solved = meta.get("solved_by_me", False)

                if is_solved:
                    continue

                found_flags = set()

                # Check flag.txt if exists
                if os.path.exists(flag_txt_path):
                    with open(flag_txt_path, "r", encoding="utf-8") as f:
                        txt = f.read().strip()
                        for m in flag_pattern.findall(txt):
                            if "..." not in m and "xxx" not in m.lower():
                                found_flags.add(m)

                # Check README.md
                if os.path.exists(readme_path):
                    with open(readme_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        # Extract flags
                        for m in flag_pattern.findall(content):
                            if "..." not in m and "FLAG{...}" not in m and "placeholder" not in m.lower():
                                found_flags.add(m)

                for flag in found_flags:
                    succ, msg = self.submit(chall_id, flag)
                    results.append({
                        "id": chall_id,
                        "name": chall_name,
                        "flag": flag,
                        "success": succ,
                        "message": msg
                    })

        Logger.info(f"Auto-scan complete. Processed {len(results)} flag submission(s).")
        return results

    def _update_local_workspace(self, challenge_id: Any, challenge_name: str, flag: str):
        """
        Marks challenge as solved in metadata.json, README.md, and SUMMARY.md.
        """
        if not self.workspace_dir:
            return

        for root, dirs, files in os.walk(self.workspace_dir):
            if "metadata.json" in files:
                meta_path = os.path.join(root, "metadata.json")
                readme_path = os.path.join(root, "README.md")
                try:
                    with open(meta_path, "r", encoding="utf-8") as f:
                        meta = json.load(f)
                    
                    if str(meta.get("id")) == str(challenge_id):
                        meta["solved_by_me"] = True
                        meta["submitted_flag"] = flag
                        with open(meta_path, "w", encoding="utf-8") as f:
                            json.dump(meta, f, indent=2, ensure_ascii=False)

                        if os.path.exists(readme_path):
                            with open(readme_path, "r", encoding="utf-8") as f:
                                r_text = f.read()
                            
                            r_text = r_text.replace("- [ ] Solved", "- [x] Solved")
                            if "FLAG{...}" in r_text:
                                r_text = r_text.replace("FLAG{...}", flag)
                                
                            with open(readme_path, "w", encoding="utf-8") as f:
                                f.write(r_text)

                        Logger.success(f"Updated local documentation for [bold cyan]{challenge_name}[/bold cyan] -> Solved ✅")
                        break
                except Exception:
                    pass

    def submit_single_flag(self, challenge_id: Optional[Union[int, str]] = None, challenge_name: Optional[str] = None, flag_value: Optional[str] = None) -> Tuple[bool, str]:
        target = challenge_id if challenge_id is not None else challenge_name
        if not target:
            Logger.error("Please specify a challenge ID or challenge Name.")
            return False, "Missing challenge identifier"
        if not flag_value:
            Logger.error("Please specify the flag to submit.")
            return False, "Missing flag value"
        return self.submit(target, flag_value)

    def auto_submit_all(self) -> List[Dict[str, Any]]:
        return self.auto_scan_and_submit()

    def interactive_submit(self):
        challs = []
        for k, v in self.challenges_cache.items():
            if isinstance(k, str) and k.isdigit():
                challs.append(v)
        
        if not challs:
            Logger.warning("No challenges loaded. Please enter challenge identifier manually.")
            cid = input("Enter Challenge ID: ").strip()
        else:
            print("\nSelect Challenge to submit flag:")
            for idx, c in enumerate(challs, 1):
                print(f"  [{idx:>2}] {c.get('name')} (ID: {c.get('id')}, {c.get('category', 'Misc')})")
            choice = input(f"Choice (1-{len(challs)}): ").strip()
            try:
                cid = challs[int(choice) - 1].get("id")
            except Exception:
                cid = choice
        
        flag = input("Enter Flag: ").strip()
        if flag:
            self.submit(cid, flag)
