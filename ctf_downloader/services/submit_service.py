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
from ..ui.diagnostics import Diagnostic, render as render_diagnostic
from rich.markup import escape
from ..services.session_factory import create_session
from ..utils.flag_format import extract_flag_format, validate_flag
from ..platforms.detector import PlatformDetector
from ..platforms.base import BasePlatform  # noqa: F401  (giữ kiểu tham chiếu cũ)
from ..platforms.registry import UnknownPlatformError, get_spec
from ..storage.constants import SOLVE_RANK
from ..storage.fileio import atomic_write_text, locked_path
from ..storage.workspace_repo import WorkspaceRepo, is_superseded

NO_FORMAT_MESSAGE = (
    "Chưa xác định được flag format cho giải này. "
    "Hãy nhập bằng --flag-format hoặc nhập tay khi được hỏi."
)

# Hint dùng chung cho mọi vấn đề xác thực / kết nối nền tảng (spec §4.6).
_DOCTOR_HINTS = (
    "chạy 'ctf doctor -u <url>' để kiểm tra cookie/token và kết nối nền tảng",
)


# ----------------------------------------------------------------------
# Diagnostic builders (ui/diagnostics.py) — lỗi nghiệp vụ submit.
# Các builder là hàm thuần để test assert trực tiếp trên hints.
# ----------------------------------------------------------------------

def diag_no_format() -> Diagnostic:
    """Gate 1 chặn submit vì chưa xác định được flag format."""
    return Diagnostic(
        "warning",
        NO_FORMAT_MESSAGE,
        hints=(
            "dùng --flag-format '<regex>' (vd: --flag-format '^PTITCTF\\{.+\\}$')",
            "hoặc chạy lại trong terminal tương tác và nhập tay khi được hỏi "
            "(regex sẽ được lưu vào challenges.json)",
            "xem thể lệ/trang chủ giải để biết định dạng flag",
        ),
    )


def diag_format_mismatch(fmt: str, source: str) -> Diagnostic:
    """Flag không khớp định dạng của giải."""
    return Diagnostic(
        "error",
        f"Flag không khớp định dạng của giải ({fmt}; nguồn: {source or 'unknown'}).",
        hints=(
            "kiểm tra lại flag đã copy đủ và đúng chữ hoa/thường chưa",
            f"nếu định dạng giải khác, truyền đè bằng --flag-format '<regex>' "
            f"(đang dùng: {fmt})",
            "chạy 'ctf doctor -u <url>' nếu nghi ngờ lấy phải rules của giải khác",
        ),
    )


def diag_blacklisted(prev_cid: Any) -> Diagnostic:
    """Blacklist chặn submit flag đã sai trước đó."""
    return Diagnostic(
        "warning",
        f"🚫 Bị blacklist: flag này đã submit SAI trước đó (challenge {prev_cid}).",
        hints=(
            "flag này đã sai — dùng --force để vẫn submit nếu chắc chắn đúng",
            "kiểm tra kỹ flag (copy trọn vẹn, không thừa/thiếu ký tự) hoặc tìm flag khác",
        ),
    )


def diag_auth_warning(exc: Exception) -> Diagnostic:
    """Authenticate thất bại nhưng pipeline vẫn tiếp tục (submit có thể vẫn chạy)."""
    return Diagnostic(
        "warning",
        "Xác thực với nền tảng thất bại — tiếp tục submit với phiên hiện tại",
        cause=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
        hints=(
            *_DOCTOR_HINTS,
            "kiểm tra cookie/token còn hạn và đúng tài khoản đăng nhập",
        ),
    )


def diag_detect_failure(exc: Exception) -> Diagnostic:
    """Không phát hiện được nền tảng CTF từ URL."""
    return Diagnostic(
        "error",
        "Không phát hiện được nền tảng CTF từ URL",
        cause=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
        hints=(
            "kiểm tra URL giải (đúng domain, có https://)",
            *_DOCTOR_HINTS,
            "nếu workspace thiếu challenges.json, chạy 'ctf pull -u <url>' trước",
        ),
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
            timeout=timeout,
            base_url=self.url,
        )
        try:
            self.platform = PlatformDetector.detect_platform(self.url, self.session)
        except Exception as exc:
            # Lỗi phát hiện nền tảng: render Diagnostic rồi lan raise như cũ
            # (caller giữ nguyên hành vi pipeline / exit code).
            render_diagnostic(diag_detect_failure(exc))
            raise
        self.challenges_cache: Dict[str, Any] = {}
        self._load_challenges()

        # Lịch sử submit chống trùng lặp / blacklist flag sai
        self.submit_history: List[Dict[str, Any]] = []
        self._load_submit_history()

        # Throttle state
        self._last_submit_monotonic: Optional[float] = None

    def _resolve_url_from_workspace(self) -> Optional[str]:
        # WorkspaceRepo hợp nhất ctf_info.url + fallback submit_endpoint
        if (not self.workspace_dir or not os.path.exists(self.workspace_dir)
                or self.repo is None):
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
        if self.workspace_dir and self.repo is not None:
            if self.repo.challenges_path.exists():
                data = self.repo.read_challenges()
                challs = data.get("challenges", [])
                if challs:
                    # Hai lượt chèn: id-key TRƯỚC, name-key SAU — khi một
                    # tên challenge trùng chuỗi với id của bài khác,
                    # NAME-KEY thắng đè (quy ước batch-3: tên toàn số ưu
                    # tiên trước id trùng). Một dict phẳng chèn xen kẽ thì
                    # thứ tự danh sách server quyết định ngầm ai thắng.
                    for c in challs:
                        cid = c.get("id")
                        self.challenges_cache[str(cid)] = c
                    for c in challs:
                        name = c.get("name", "")
                        self.challenges_cache[name.lower().strip()] = c
                return

        # Fetch live if not in local cache
        try:
            self.platform.authenticate()
            challs = self.platform.fetch_challenges()
            ids = {str(c.id): {"id": c.id, "name": c.name, "category": c.category}
                   for c in challs}
            names = {c.name.lower().strip(): {"id": c.id, "name": c.name,
                                              "category": c.category}
                     for c in challs}
            # name-key ghi sau -> thắng id-key trùng chuỗi (như nhánh trên)
            self.challenges_cache.update(ids)
            self.challenges_cache.update(names)
        except Exception as e:
            Logger.warning(f"Không tải được danh sách challenges: {e}")

    def resolve_challenge_id(self, identifier: Union[int, str]) -> Tuple[Optional[Any], str]:
        """
        Resolves challenge ID and Name from ID or Challenge Name string.

        Cache phẳng chứa CẢ id-key lẫn name-key (review-6): khi một tên
        challenge trùng chuỗi với id bài khác, name-key ghi sau thắng chỗ
        trong dict (quy ước batch-3 — consumer muốn theo tên đọc
        ``cache[name]``, xem ``cli_commands._hoard_identifier``). Nhánh
        id-route ở đây phải SO SÁNH CHỦ SỞ HỮU: entry dưới key không đúng
        id đích là của challenge khác, không được rò tên/id của nó vào
        kết quả.
        """
        if isinstance(identifier, int) or str(identifier).isdigit():
            cid = int(identifier)
            c_info = self.challenges_cache.get(str(cid), {})
            # Key bị name-key trùng chuỗi chiếm chỗ -> coi như không tra được
            if str(c_info.get("id")) != str(cid):
                c_info = {}
            name = c_info.get("name", f"Challenge_{cid}")
            return cid, name

        ident_str = str(identifier).lower().strip()
        if not ident_str:
            # Hunt-c18 BUG-8 (LOW): identifier rỗng/toàn khoảng trắng —
            # ``"" in key`` khớp partial-match ĐẦU TIÊN bừa bãi (nộp flag
            # vào challenge ngẫu nhiên). Trả None để caller báo lỗi rõ.
            return None, str(identifier)
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
                # Regex format là dữ liệu literal → token neutral (không vàng legacy).
                Logger.success(f"Đã suy ra flag format từ rules: [literal]{escape(fmt)}[/literal]", markup=True)
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

    def _submit_gate_path(self, challenge_id: Any):
        """Dedicated per-challenge lock key for cross-process submissions.

        It deliberately does NOT reuse submit_history.json's lock: the gate
        must stay held from the final blacklist/status re-check through the
        network POST and history/status commit, while history persistence may
        acquire its own lock internally.
        """
        if not self.repo:
            return None
        token = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(challenge_id)).strip("._")
        token = (token or "unknown")[:96]
        return self.repo.root / f".submit-gate-{token}"

    def _recheck_before_network_submit(self, cid: Any, flag: str, force: bool) -> Optional[str]:
        """Re-read mutable state after acquiring the cross-process gate."""
        self._load_submit_history()
        entry = self._find_history_entry(flag)
        if entry:
            prev_result = entry.get("result")
            prev_cid = entry.get("challenge_id")
            if prev_result == "correct" and str(prev_cid) == str(cid):
                return "⏭️ Đã solved: flag này đã đúng cho challenge này."
            if prev_result == "incorrect" and not force:
                return f"🚫 Bị blacklist: flag này đã submit SAI trước đó (challenge {prev_cid})."

        # Khóa theo CHALLENGE chứ không chỉ theo flag: process khác có thể vừa
        # solve bài bằng một candidate khác trong lúc ta chờ. Không gửi thêm
        # candidate sau khi local status đã lên solved_by_me.
        meta_path = self._find_meta_path(cid)
        if meta_path is not None and self.repo is not None:
            try:
                st = self.repo.read_status(meta_path)
                if SOLVE_RANK.get(st.get("solve"), 0) >= SOLVE_RANK["solved_by_me"]:
                    return "⏭️ Đã solved: challenge đã được đánh dấu solved_by_me trong workspace."
            except Exception:
                pass
        return None

    def _submit_network_and_commit(self, cid: Any, name: str, flag: str) -> Tuple[bool, str]:
        """Perform exactly one platform POST and commit its typed verdict."""
        self._throttle()
        success, message = self.platform.submit_flag(cid, flag)

        verdict = getattr(self.platform, "last_verdict", None) or ("correct" if success else "unknown")

        non_judgement = {
            "ratelimited",
            "auth_failed",
            "event_not_started",
            "event_paused",
            "event_closed",
            "cheat_detected",
            "challenge_not_found",
        }
        if verdict in non_judgement:
            Logger.warning(
                f"Submit chưa có phán quyết ({verdict}) — "
                f"không ghi lịch sử/blacklist: {message}"
            )
            return success, message

        if verdict == "already_solved":
            Logger.success(f"Kết quả: {message}")
            if self.workspace_dir:
                self._apply_verdict_to_status(cid, flag, verdict)
            return success, message

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
            Logger.success(f"Kết quả: {message}")
            if self.workspace_dir:
                self._update_local_workspace(cid, name, flag)
        elif verdict == "incorrect":
            Logger.error(f"Kết quả: {message}")
        else:
            Logger.warning(f"Kết quả: {message} (không rõ đúng/sai — chưa blacklist)")

        if verdict in ("correct", "incorrect"):
            self._apply_verdict_to_status(cid, flag, verdict)

        return success, message

    def submit(self, challenge_identifier: Union[int, str], flag: str, force: bool = False) -> Tuple[bool, str]:
        """
        Submits a flag for a given challenge, với:
          - Gate flag format (bắt buộc khớp mới submit)
          - Blacklist chống submit trùng / flag đã sai
          - Throttle theo platform
        """
        flag = flag.strip()
        if not flag:
            return False, "Flag không được để trống."

        cid, name = self.resolve_challenge_id(challenge_identifier)
        if cid is None:
            return False, f"Không tìm thấy challenge khớp: '{challenge_identifier}'"

        # ---- Gate 1: flag format ----
        fmt, fmt_source = self.resolve_flag_format()
        if not fmt:
            render_diagnostic(diag_no_format())
            return False, NO_FORMAT_MESSAGE
        if not validate_flag(flag, fmt):
            render_diagnostic(diag_format_mismatch(fmt, fmt_source))
            msg = f"Flag không khớp định dạng của giải ({fmt}; nguồn: {fmt_source or 'unknown'})."
            return False, msg

        # ---- Gate 2: blacklist / chống submit trùng ----
        entry = self._find_history_entry(flag)
        if entry:
            prev_result = entry.get("result")
            prev_cid = entry.get("challenge_id")
            if prev_result == "correct":
                if str(prev_cid) == str(cid):
                    Logger.info("Bỏ qua: flag này đã submit ĐÚNG cho chính challenge này trước đó (already solved).")
                    return False, "⏭️ Đã solved: flag này đã đúng cho challenge này."
            elif prev_result == "incorrect" and not force:
                render_diagnostic(diag_blacklisted(prev_cid))
                return False, f"🚫 Bị blacklist: flag này đã submit SAI trước đó (challenge {prev_cid})."

        # Tên challenge / giá trị flag là DỮ LIỆU → token neutral fg.base.
        Logger.info(f"Đang submit flag cho [info]{escape(str(name))}[/info] (ID: {cid})...", markup=True)
        Logger.info(f"Flag: [info]{escape(str(flag))}[/info]", markup=True)

        # Authenticate if needed
        try:
            self.platform.authenticate()
        except Exception as e:
            render_diagnostic(diag_auth_warning(e))

        gate_path = self._submit_gate_path(cid)
        if gate_path is not None:
            with locked_path(gate_path):
                blocked = self._recheck_before_network_submit(cid, flag, force)
                if blocked:
                    Logger.info(blocked)
                    return False, blocked
                return self._submit_network_and_commit(cid, name, flag)

        # Không có workspace => không có state dùng chung để lock; vẫn giữ
        # đúng contract một POST duy nhất và KHÔNG auto-retry POST.
        return self._submit_network_and_commit(cid, name, flag)

    # ------------------------------------------------------------------
    # Status đa chiều hooks
    # ------------------------------------------------------------------

    def _find_meta_path(self, challenge_id: Any):
        """Tìm metadata.json của challenge theo id (None nếu không có workspace).

        Review-6 HIGH: bản tombstone ``superseded_by`` (thư mục chết sau
        rename) không được khớp theo id — phán quyết submit/hoard phải ghi
        vào bản sống."""
        if not self.repo:
            return None
        for meta_path in self.repo.iter_challenges():
            meta = self.repo.read_metadata(meta_path)
            if (meta and not is_superseded(meta)
                    and str(meta.get("id")) == str(challenge_id)):
                return meta_path
        return None

    def _apply_verdict_to_status(self, challenge_id: Any, flag: str, verdict: str):
        """Ghi kết quả submit vào block ``status`` của challenge.

        - correct  : flag → submitted_correct (+value), solve nâng lên solved_by_me
                     (mirror solved_by_me + marker README do update_status lo)
        - incorrect: flag → submitted_wrong (+value để biết flag nào chết)
        """
        meta_path = self._find_meta_path(challenge_id)
        repo = self.repo
        if meta_path is None or repo is None:
            return
        try:
            def _mut(st):
                if verdict == "already_solved":
                    if SOLVE_RANK.get(st["solve"], 0) < SOLVE_RANK["solved_by_me"]:
                        st["solve"] = "solved_by_me"
                    return st
                st["flag"]["value"] = flag
                st["flag"]["state"] = "submitted_correct" if verdict == "correct" else "submitted_wrong"
                if verdict == "correct" and SOLVE_RANK.get(st["solve"], 0) < SOLVE_RANK["solved_by_me"]:
                    st["solve"] = "solved_by_me"
                return st

            repo.update_status(meta_path, _mut)
        except Exception as e:
            Logger.warning(f"Không thể cập nhật status đa chiều: {e}")

    def hoard_flag(self, challenge_identifier: Union[int, str], flag_value: str) -> Tuple[bool, str]:
        """Lưu cờ tìm được vào kho local (flag → hoarded) mà KHÔNG submit.

        Sẵn sàng cho lệnh CLI ``ctf flag <chal> <FLAG>`` ở feature sau.
        """
        flag_value = (flag_value or "").strip()
        if not flag_value:
            return False, "Flag không được để trống."
        cid, name = self.resolve_challenge_id(challenge_identifier)
        meta_path = self._find_meta_path(cid)
        repo = self.repo
        if meta_path is None or repo is None:
            return False, f"Không tìm thấy challenge khớp: '{challenge_identifier}'"
        try:
            def _mut(st):
                st["flag"]["value"] = flag_value
                st["flag"]["state"] = "hoarded"
                if SOLVE_RANK.get(st["solve"], 0) < SOLVE_RANK["working"]:
                    st["solve"] = "working"
                return st

            repo.update_status(meta_path, _mut)
            Logger.success(f"🏴 Đã hoard flag cho [info]{escape(str(name))}[/info] (chưa submit).", markup=True)
            return True, "Đã lưu flag vào kho."
        except Exception as e:
            Logger.warning(f"Không thể hoard flag: {e}")
            return False, str(e)

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
        if "Đã solved" in message:
            return "skipped_solved"
        if "blacklist" in message:
            return "skipped_blacklisted"
        if NO_FORMAT_MESSAGE in message or "không khớp định dạng" in message:
            return "skipped_by_format"
        if success:
            return "submitted_ok"
        return "failed"

    def _challenge_is_solved_now(self, meta_path) -> bool:
        """[hunt-c18 BUG-4] Re-load trạng thái solved MỚI NHẤT của challenge
        từ đĩa (block status qua migrate-on-read của repo) — gate giữa các
        candidate flag: submit() vừa đánh dấu solved thì các candidate còn
        lại CÙNG challenge phải bị bỏ qua."""
        repo = self.repo
        if repo is None:
            return False
        try:
            st = repo.read_status(meta_path)
        except Exception:
            return False
        return SOLVE_RANK.get(st.get("solve"), 0) >= SOLVE_RANK["solved_by_me"]

    def auto_scan_and_submit(self, force: bool = False) -> List[Dict[str, Any]]:
        """
        Scans workspace directory for filled flags in README.md or flag.txt and submits them.
        Regex candidate bị siết: chỉ tiền tố rõ ràng (FLAG|CTF|PTITCTF hoặc prefix đọc
        được từ flag format của giải). Mỗi candidate phải qua gate format + blacklist.
        """
        if not self.workspace_dir or not os.path.exists(self.workspace_dir):
            Logger.error("Không tìm thấy thư mục workspace để auto-scan.")
            return []
        repo = self.repo
        if repo is None:
            Logger.error("WorkspaceRepo chưa được khởi tạo cho auto-scan.")
            return []

        Logger.info(f"Đang quét workspace tìm flag chưa nộp: [info]{escape(self.workspace_dir)}[/info]", markup=True)

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

        for meta_path in repo.iter_challenges():
            root = meta_path.parent

            try:
                meta = repo.read_metadata(meta_path)
            except Exception:
                continue
            if not meta or is_superseded(meta):
                # Review-6 HIGH: thư mục tombstone không tham gia auto-scan
                # (flag trong dir chết đã được restore sang dir sống).
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
                # Hunt-c18 BUG-4 (MED): candidate ĐẦU TIÊN của challenge
                # vừa đúng — re-load trạng thái solved rồi bỏ qua các
                # candidate còn lại CÙNG challenge (nộp tiếp flag khác sai
                # = penalty trên nhiều platform). Gate theo trạng thái đĩa
                # chứ không theo chuỗi flag: chỉ dừng khi THẬT sự solved.
                if category == "submitted_ok" \
                        and self._challenge_is_solved_now(meta_path):
                    Logger.info(
                        f"[info]{escape(str(chall_name))}[/info] đã solved "
                        f"— bỏ qua các candidate flag còn lại cùng "
                        f"challenge.", markup=True)
                    break

        Logger.print_table(
            title=f"Tổng kết auto-scan submit ({len(results)} ứng viên)",
            columns=["Kết quả", "Số lượng"],
            rows=[[k, str(v)] for k, v in stats.items()]
        )
        Logger.success(
            f"Auto-scan hoàn tất: {stats['submitted_ok']} submitted_ok, "
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
        if not self.workspace_dir or self.repo is None:
            return
        repo = self.repo

        for meta_path in repo.iter_challenges():
            meta = repo.read_metadata(meta_path)
            if (not meta or is_superseded(meta)
                    or str(meta.get("id")) != str(challenge_id)):
                continue

            try:
                # Read-mutate-write trong cùng khóa flock với update_status
                # (W5.2: write_metadata unlocked gây lost update đa tiến trình).
                def _mut(current: dict) -> dict:
                    current = dict(current or {})
                    current["solved_by_me"] = True
                    current["submitted_flag"] = flag
                    return current

                repo.update_metadata(meta_path, _mut)

                root = meta_path.parent
                r_candidates = [root / "writeup" / "README.md", root / "README.md"]
                # Marker solved + placeholder flag trong README
                existing = [r for r in r_candidates if r.exists()]
                repo.write_solved_state(existing, solved=True)
                for r_candidate in existing:
                    # Hunt-c18 BUG-3 (MED): đọc/ghi README qua storage/fileio
                    # (khóa flock + atomic os.replace) thay vì open() thô —
                    # crash giữa chừng từng truncate README, writer khác
                    # từng lost-update. Lỗi KHÔNG còn bị nuốt lặng lẽ.
                    try:
                        with locked_path(r_candidate):
                            r_text = r_candidate.read_text(encoding="utf-8")
                            if "FLAG{...}" in r_text:
                                atomic_write_text(
                                    r_candidate,
                                    r_text.replace("FLAG{...}", flag))
                    except OSError as e:
                        Logger.warning(
                            f"Không thay được placeholder FLAG{{...}} trong "
                            f"{r_candidate}: {e}")

                Logger.success(f"Đã cập nhật tài liệu local cho [info]{escape(str(challenge_name))}[/info] -> Solved ✅", markup=True)
                break
            except Exception as e:
                Logger.warning(
                    f"Không thể cập nhật tài liệu local cho "
                    f"{escape(str(challenge_name))}: {e}")

    def submit_single_flag(
        self,
        challenge_id: Optional[Union[int, str]] = None,
        challenge_name: Optional[str] = None,
        flag_value: Optional[str] = None,
        force: bool = False,
    ) -> Tuple[bool, str]:
        target = challenge_id if challenge_id is not None else challenge_name
        if not target:
            Logger.error("Hãy chỉ định challenge ID hoặc tên challenge.")
            return False, "Thiếu định danh challenge"
        if not flag_value:
            Logger.error("Hãy chỉ định flag cần submit.")
            return False, "Thiếu giá trị flag"
        return self.submit(target, flag_value, force=force)

    def auto_submit_all(self, force: bool = False) -> List[Dict[str, Any]]:
        return self.auto_scan_and_submit(force=force)

    def interactive_submit(self, force: bool = False):
        challs = []
        for k, v in self.challenges_cache.items():
            if isinstance(k, str) and k.isdigit():
                challs.append(v)

        if not challs:
            Logger.warning("Chưa tải được challenge nào. Hãy nhập định danh challenge thủ công.")
            cid = input("Nhập Challenge ID: ").strip()
        else:
            print("\nChọn challenge để submit flag:")
            for idx, c in enumerate(challs, 1):
                print(f"  [{idx:>2}] {c.get('name')} (ID: {c.get('id')}, {c.get('category', 'Misc')})")
            choice = input(f"Chọn (1-{len(challs)}): ").strip()
            try:
                cid = challs[int(choice) - 1].get("id")
            except Exception:
                cid = choice

        flag = input("Nhập Flag: ").strip()
        if flag:
            self.submit(cid, flag, force=force)
