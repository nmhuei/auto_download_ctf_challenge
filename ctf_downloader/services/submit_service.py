"""SubmitService — toàn bộ logic submit flag (body cũ của submitter.FlagSubmitter).

FlagSubmitter trong ``ctf_downloader.submitter`` giờ chỉ là facade mỏng delegate
về đây. Hằng message công khai (NO_FORMAT_MESSAGE...) được giữ nguyên vì test_sp1
assert trực tiếp trên chuỗi.
"""
import os
import re
import sys
import time
from typing import Optional, Union, Dict, Any, List, Tuple

from ..utils.logger import Logger
from ..services.session_factory import create_session
from ..utils.flag_format import extract_flag_format, validate_flag
from ..platforms.detector import PlatformDetector
from ..platforms.base import BasePlatform  # noqa: F401  (giữ kiểu tham chiếu cũ)
from ..platforms.registry import UnknownPlatformError, get_spec
from ..storage.workspace_repo import WorkspaceRepo

NO_FORMAT_MESSAGE = (
    "Chưa xác định được flag format cho giải này. "
    "Hãy nhập bằng --flag-format hoặc nhập tay khi được hỏi."
)

# Throttle: khoảng cách tối thiểu giữa 2 lần submit trong cùng process (giây).
# Nguồn chân lý duy nhất là PLATFORMS[key].throttle (registry); giá trị fallback
# khi platform type không nằm trong registry.
DEFAULT_THROTTLE = 5.0

# Tiền tố flag phổ biến dùng cho auto-scan
BASE_KNOWN_PREFIXES = ["FLAG", "CTF", "PTITCTF"]

# Body placeholder -> bỏ qua khi scan
_PLACEHOLDER_BODIES = {"...", "..", "…", "xxx", "<flag>", "<...>", "flag_here", "your_flag"}


class SubmitService:
    def __init__(
        self,
        url: Optional[str] = None,
        cookie: Optional[str] = None,
        token: Optional[str] = None,
        workspace_dir: Optional[str] = None,
        timeout: int = 30,
        flag_format: Optional[str] = None,
    ):
        self.workspace_dir = os.path.abspath(workspace_dir) if workspace_dir else None
        self.repo = WorkspaceRepo(self.workspace_dir) if self.workspace_dir else None
        self.url = url or self._resolve_url_from_workspace()
        self.cookie = cookie
        self.token = token
        self.timeout = timeout
        # Format truyền trực tiếp từ CLI (--flag-format) — độ ưu tiên cao nhất
        self.explicit_flag_format = flag_format.strip() if flag_format else None

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

        # Lịch sử submit chống trùng lặp / blacklist flag sai
        self.submit_history: List[Dict[str, Any]] = []
        self._load_submit_history()

        # Throttle state
        self._last_submit_monotonic: Optional[float] = None

    def _resolve_url_from_workspace(self) -> Optional[str]:
        # WorkspaceRepo hợp nhất ctf_info.url + fallback submit_endpoint
        if not self.workspace_dir or not os.path.exists(self.workspace_dir):
            return None
        return self.repo.resolve_platform_url()

    def _load_challenges(self):
        """
        Loads challenge map from local challenges.json or fetches live.

        Hành vi frozen: file challenges.json TỒN TẠI là đủ để dùng cache —
        kể cả khi mảng ``challenges`` rỗng (không rơi xuống fetch live).
        Chỉ fetch live khi không đọc được file (thiếu / lỗi đọc).
        """
        # Try local challenges.json first
        if self.workspace_dir:
            if self.repo.challenges_path.exists():
                data = self.repo.read_challenges()
                challs = data.get("challenges", [])
                if challs:
                    for c in challs:
                        cid = c.get("id")
                        name = c.get("name", "")
                        self.challenges_cache[str(cid)] = c
                        self.challenges_cache[name.lower().strip()] = c
                return

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

    # ------------------------------------------------------------------
    # Flag-format resolution & workspace cache
    # ------------------------------------------------------------------

    @staticmethod
    def _stdout_isatty() -> bool:
        try:
            return sys.stdout.isatty()
        except Exception:
            return False

    def _cached_flag_format(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Đọc flag format + nguồn từ cache trong challenges.json (ctf_info).
        """
        if not self.repo:
            return None, None
        ctf_info = self.repo.read_challenges().get("ctf_info") or {}
        fmt = ctf_info.get("flag_format")
        source = ctf_info.get("flag_format_source")
        if fmt and str(fmt).strip():
            return str(fmt).strip(), source
        return None, None

    def _save_flag_format_to_cache(self, fmt_regex: str, source: str) -> bool:
        """
        Lưu flag format + nguồn vào challenges.json (ctf_info) để lần sau không phải hỏi lại.
        """
        if not self.repo:
            return False
        try:
            self.repo.update_ctf_info(flag_format=fmt_regex, flag_format_source=source)
            return True
        except Exception as e:
            Logger.warning(f"Không thể lưu flag format vào cache: {e}")
            return False

    def resolve_flag_format(self) -> Tuple[Optional[str], str]:
        """
        Thứ tự resolve flag format:
          1. Tham số flag_format truyền trực tiếp (--flag-format)
          2. Cache trong challenges.json (ctf_info.flag_format)
          3. Tự gọi platform.fetch_rules() + extract_flag_format() (lưu cache nếu tìm được)
          4. Nếu stdout là tty: hỏi user nhập 1 lần rồi lưu cache
          5. Không có -> (None, "")
        Trả về (fmt_regex, source) với source ∈ cli|cache|rules|manual|"".
        """
        if self.explicit_flag_format:
            return self.explicit_flag_format, "cli"

        cached, _src = self._cached_flag_format()
        if cached:
            return cached, "cache"

        rules_text = None
        try:
            rules_text = self.platform.fetch_rules()
        except Exception as e:
            Logger.warning(f"Lỗi khi fetch rules: {e}")
        if rules_text:
            fmt = extract_flag_format(rules_text)
            if fmt:
                Logger.success(f"Đã suy ra flag format từ rules: [bold yellow]{fmt}[/bold yellow]")
                self._save_flag_format_to_cache(fmt, "rules")
                return fmt, "rules"

        if self._stdout_isatty():
            try:
                Logger.info(
                    "Chưa tự xác định được flag format. "
                    "Nhập regex định dạng flag (vd: ^PTITCTF\\{.+\\}$), bỏ trống để bỏ qua:"
                )
                val = input("Flag format regex: ").strip()
            except (EOFError, KeyboardInterrupt):
                val = ""
            if val:
                self._save_flag_format_to_cache(val, "manual")
                return val, "manual"

        return None, ""

    # ------------------------------------------------------------------
    # Submit history / blacklist
    # ------------------------------------------------------------------

    def _load_submit_history(self):
        """
        Load submit_history.json. File hỏng (kể cả JSON hợp lệ nhưng không phải
        dict) -> coi như rỗng; WorkspaceRepo đã lưu backup .bak.
        """
        if not self.repo:
            self.submit_history = []
            return
        hist = self.repo.load_submit_history()
        self.submit_history = [e for e in hist.get("entries", []) if isinstance(e, dict)]

    def _save_submit_history(self):
        if not self.repo:
            return
        try:
            self.repo.save_submit_history({"entries": self.submit_history})
        except Exception as e:
            Logger.warning(f"Không thể lưu submit_history.json: {e}")

    def _find_history_entry(self, flag: str) -> Optional[Dict[str, Any]]:
        fl = (flag or "").strip()
        for e in self.submit_history:
            if str(e.get("flag", "")).strip() == fl:
                return e
        return None

    def _record_submit_result(self, flag: str, challenge_id: Any, result: str):
        entry = self._find_history_entry(flag)
        timestamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if entry:
            entry["result"] = result
            entry["challenge_id"] = challenge_id
            entry["timestamp"] = timestamp
        else:
            self.submit_history.append({
                "flag": flag,
                "challenge_id": challenge_id,
                "result": result,
                "timestamp": timestamp,
            })
        self._save_submit_history()

    # ------------------------------------------------------------------
    # Throttle — đọc khoảng cách tối thiểu từ platform registry (spec.throttle)
    # ------------------------------------------------------------------

    def _platform_type(self) -> str:
        return getattr(getattr(self.platform, "ctf_info", None), "platform_type", "") or ""

    def _resolve_min_gap(self) -> Tuple[float, str]:
        ptype = self._platform_type()
        try:
            return float(get_spec(ptype).throttle), ptype
        except UnknownPlatformError:
            # Chỉ fallback khi platform type không có trong registry; lỗi khác
            # (bug thật) phải lan lên thay vì bị nuốt thành throttle mặc định.
            return DEFAULT_THROTTLE, ptype

    def _throttle(self):
        min_gap, ptype = self._resolve_min_gap()
        now = time.monotonic()
        if self._last_submit_monotonic is not None:
            elapsed = now - self._last_submit_monotonic
            if elapsed < min_gap:
                wait = min_gap - elapsed
                Logger.info(f"Throttle ({ptype}): chờ {wait:.1f}s trước khi submit tiếp...")
                time.sleep(wait)
        self._last_submit_monotonic = time.monotonic()

    # ------------------------------------------------------------------
    # Core submit
    # ------------------------------------------------------------------

    def submit(self, challenge_identifier: Union[int, str], flag: str, force: bool = False) -> Tuple[bool, str]:
        """
        Submits a flag for a given challenge, với:
          - Gate flag format (bắt buộc khớp mới submit)
          - Blacklist chống submit trùng / flag đã sai
          - Throttle theo platform
        """
        flag = flag.strip()
        if not flag:
            return False, "Flag cannot be empty."

        cid, name = self.resolve_challenge_id(challenge_identifier)
        if cid is None:
            return False, f"Could not resolve challenge: '{challenge_identifier}'"

        # ---- Gate 1: flag format ----
        fmt, fmt_source = self.resolve_flag_format()
        if not fmt:
            Logger.warning(NO_FORMAT_MESSAGE)
            return False, NO_FORMAT_MESSAGE
        if not validate_flag(flag, fmt):
            msg = f"Flag không khớp định dạng của giải ({fmt}; nguồn: {fmt_source or 'unknown'})."
            Logger.error(msg)
            return False, msg

        # ---- Gate 2: blacklist / chống submit trùng ----
        entry = self._find_history_entry(flag)
        if entry:
            prev_result = entry.get("result")
            prev_cid = entry.get("challenge_id")
            if prev_result == "correct":
                if str(prev_cid) == str(cid):
                    Logger.info("Bỏ qua: flag này đã submit ĐÚNG cho chính challenge này trước đó (already solved).")
                    return False, "⏭️ Already solved: flag này đã đúng cho challenge này."
            elif prev_result == "incorrect" and not force:
                Logger.warning(f"Flag này đã submit SAI trước đó (challenge {prev_cid}). Dùng --force để vẫn submit.")
                return False, f"🚫 Blacklisted: flag này đã submit SAI trước đó (challenge {prev_cid})."

        Logger.info(f"Submitting flag for [bold cyan]{name}[/bold cyan] (ID: {cid})...")
        Logger.info(f"Flag: [bold yellow]{flag}[/bold yellow]")

        # Authenticate if needed
        try:
            self.platform.authenticate()
        except Exception as e:
            Logger.warning(f"Authenticate warning: {e}")

        self._throttle()

        success, message = self.platform.submit_flag(cid, flag)

        verdict = getattr(self.platform, "last_verdict", None) or ("correct" if success else "unknown")

        # Rate-limited: KHÔNG ghi lịch sử / blacklist
        if verdict == "ratelimited":
            Logger.warning(f"Bị rate-limit — không ghi lịch sử/blacklist: {message}")
            return success, message

        # Unknown: hỏi user (nếu có tty) trước khi ghi kết quả
        if verdict == "unknown" and self._stdout_isatty():
            try:
                ans = input(f"Không xác định được kết quả chấm. Flag '{flag}' có ĐÚNG không? [y/N]: ").strip().lower()
            except (EOFError, KeyboardInterrupt):
                ans = ""
            if ans.startswith("y"):
                verdict = "correct"
            elif ans.startswith("n"):
                verdict = "incorrect"

        self._record_submit_result(flag, cid, verdict)

        if verdict == "correct":
            Logger.success(f"Result: {message}")
            if self.workspace_dir:
                self._update_local_workspace(cid, name, flag)
        elif verdict == "incorrect":
            Logger.error(f"Result: {message}")
        else:
            Logger.warning(f"Result: {message} (không rõ đúng/sai — chưa blacklist)")

        return success, message

    # ------------------------------------------------------------------
    # Auto scan & submit
    # ------------------------------------------------------------------

    @staticmethod
    def _known_prefixes(fmt: Optional[str]) -> List[str]:
        prefixes = list(BASE_KNOWN_PREFIXES)
        if fmt:
            m = re.match(r'\^?\(?([A-Za-z][A-Za-z0-9_]{0,24})(?:\\\{|\{)', fmt)
            if m and m.group(1) not in prefixes:
                prefixes.insert(0, m.group(1))
        return prefixes

    def _candidate_pattern(self, fmt: Optional[str]) -> re.Pattern:
        prefixes = self._known_prefixes(fmt)
        alternation = "|".join(re.escape(p) for p in prefixes)
        return re.compile(r'\b(?:' + alternation + r')\{([^{}\n]{1,256})\}')

    def _is_placeholder_candidate(self, body: str) -> bool:
        b = body.strip().lower()
        if not b or b in _PLACEHOLDER_BODIES:
            return True
        if ".." in b:
            return True
        return False

    def _classify_auto_result(self, success: bool, message: str) -> str:
        """
        Phân loại kết quả submit trong auto-scan để tổng kết thống kê.
        """
        if "Already solved" in message:
            return "skipped_solved"
        if "Blacklisted" in message:
            return "skipped_blacklisted"
        if NO_FORMAT_MESSAGE in message or "không khớp định dạng" in message:
            return "skipped_by_format"
        if success:
            return "submitted_ok"
        return "failed"

    def auto_scan_and_submit(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Scans workspace directory for filled flags in README.md or flag.txt and submits them.
        Regex candidate bị siết: chỉ tiền tố rõ ràng (FLAG|CTF|PTITCTF hoặc prefix đọc
        được từ flag format của giải). Mỗi candidate phải qua gate format + blacklist.
        """
        if not self.workspace_dir or not os.path.exists(self.workspace_dir):
            Logger.error("Workspace directory not found for auto-scan.")
            return []

        Logger.info(f"Scanning workspace for unsubmitted flags: [bold cyan]{self.workspace_dir}[/bold cyan]")

        stats = {
            "submitted_ok": 0,
            "skipped_by_format": 0,
            "skipped_blacklisted": 0,
            "skipped_solved": 0,
            "failed": 0,
        }
        results: List[Dict[str, Any]] = []

        fmt, _fmt_source = self.resolve_flag_format()
        flag_pattern = self._candidate_pattern(fmt)

        def extract_full_matches(text: str) -> List[str]:
            found = []
            for m in flag_pattern.finditer(text):
                body = m.group(1)
                if self._is_placeholder_candidate(body):
                    continue
                found.append(m.group(0))
            return found

        for meta_path in self.repo.iter_challenges():
            root = meta_path.parent

            try:
                meta = self.repo.read_metadata(meta_path)
            except Exception:
                continue
            if not meta:
                continue

            chall_id = meta.get("id")
            chall_name = meta.get("name")
            is_solved = meta.get("solved_by_me", False)

            if is_solved:
                continue

            found_flags = set()

            # Check flag.txt if exists
            flag_txt_path = root / "flag.txt"
            if flag_txt_path.exists():
                with open(flag_txt_path, "r", encoding="utf-8") as f:
                    found_flags.update(extract_full_matches(f.read()))

            # Check README.md or writeup/README.md
            for r_candidate in [root / "writeup" / "README.md", root / "README.md", root / "challenge" / "README.md"]:
                if r_candidate.exists():
                    with open(r_candidate, "r", encoding="utf-8") as f:
                        found_flags.update(extract_full_matches(f.read()))

            for flag in sorted(found_flags):
                succ, msg = self.submit(chall_id, flag, force=force)
                category = self._classify_auto_result(succ, msg)
                stats[category] += 1
                results.append({
                    "id": chall_id,
                    "name": chall_name,
                    "flag": flag,
                    "success": succ,
                    "message": msg,
                    "category": category,
                })

        Logger.print_table(
            title=f"Auto-scan Submit Summary ({len(results)} candidate(s))",
            columns=["Kết quả", "Số lượng"],
            rows=[[k, str(v)] for k, v in stats.items()]
        )
        Logger.success(
            f"Auto-scan complete: {stats['submitted_ok']} submitted_ok, "
            f"{stats['skipped_by_format']} skipped_by_format, "
            f"{stats['skipped_blacklisted']} skipped_blacklisted, "
            f"{stats['skipped_solved']} skipped_solved, "
            f"{stats['failed']} failed."
        )
        return results

    def _update_local_workspace(self, challenge_id: Any, challenge_name: str, flag: str):
        """
        Marks challenge as solved in metadata.json, README.md, writeup/README.md, and SUMMARY.md.
        """
        if not self.workspace_dir:
            return

        for meta_path in self.repo.iter_challenges():
            meta = self.repo.read_metadata(meta_path)
            if not meta or str(meta.get("id")) != str(challenge_id):
                continue

            try:
                meta["solved_by_me"] = True
                meta["submitted_flag"] = flag
                self.repo.write_metadata(meta_path, meta)

                root = meta_path.parent
                r_candidates = [root / "writeup" / "README.md", root / "README.md"]
                # Marker solved + placeholder flag trong README
                existing = [r for r in r_candidates if r.exists()]
                self.repo.write_solved_state(existing, solved=True)
                for r_candidate in existing:
                    with open(r_candidate, "r", encoding="utf-8") as f:
                        r_text = f.read()
                    if "FLAG{...}" in r_text:
                        with open(r_candidate, "w", encoding="utf-8") as f:
                            f.write(r_text.replace("FLAG{...}", flag))

                Logger.success(f"Updated local documentation for [bold cyan]{challenge_name}[/bold cyan] -> Solved ✅")
                break
            except Exception:
                pass

    def submit_single_flag(
        self,
        challenge_id: Optional[Union[int, str]] = None,
        challenge_name: Optional[str] = None,
        flag_value: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[bool, str]:
        target = challenge_id if challenge_id is not None else challenge_name
        if not target:
            Logger.error("Please specify a challenge ID or challenge Name.")
            return False, "Missing challenge identifier"
        if not flag_value:
            Logger.error("Please specify the flag to submit.")
            return False, "Missing flag value"
        return self.submit(target, flag_value, force=force)

    def auto_submit_all(self, force: bool = False) -> List[Dict[str, Any]]:
        return self.auto_scan_and_submit(force=force)

    def interactive_submit(self, force: bool = False):
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
            self.submit(cid, flag, force=force)
