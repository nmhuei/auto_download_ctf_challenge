"""RegisterService — auto-register 1 tài khoản/lần chạy trên platform CTF.

Van-an-toàn (bắt buộc, spec auto-register §4):
  - TẠO ĐÚNG 1 tài khoản cho mỗi lần chạy — KHÔNG có cờ count/batch, KHÔNG loop.
  - Luôn in cảnh báo rules trước khi tạo tài khoản.
  - Captcha phức tạp (Turnstile/reCAPTCHA/hCaptcha) -> dừng sạch, hướng dẫn
    thủ công (platform layer raise PlatformRegisterUnsupported) — không bypass.
  - Rate limit nội bộ: 2 lần register trên cùng URL phải cách nhau >= 60s
    (state persist trong global config để chặn cả giữa các lần chạy CLI).
"""
import os
import random
import string
import time
from typing import Any, Callable, Dict, Optional

from rich.markup import escape

from ..platforms.base import PlatformRegisterUnsupported
from ..storage.global_config import (load_global_config, save_global_config,
                                     update_global_config)
from ..storage.fileio import SKIP_WRITE
from ..ui.theme import ERROR, FG_BASE, FG_FAINT, FG_MUTED, INFO, SOLVED, WARN
from ..utils.logger import Logger, console
from ..utils.tempmail import TempMailClient, TempMailError

# Bảng ký tự mật khẩu mạnh (loại bỏ ký tự dễ nhầm lẫn l/1/I/O/0)
_PW_LOWER = "abcdefghijkmnopqrstuvwxyz"
_PW_UPPER = "ABCDEFGHJKLMNPQRSTUVWXYZ"
_PW_DIGIT = "23456789"
_PW_SPECIAL = "!@#$%^&*-_+?"
_USERNAME_ALPHABET = string.ascii_lowercase + string.digits

#: Kết quả :meth:`RegisterService._commit_attempt` (review c18-2, LOW):
#: phân biệt rõ BA trạng thái — caller cũ chỉ nhìn bool nên case "thư mục
#: config biến mất / storage hỏng" vẫn bị báo thành công như case ghi được.
COMMIT_OK = "ok"                    # attempt (+ auth) đã persist
COMMIT_PREEMPTED = "preempted"      # thua cuộc TOCTOU: không ghi gì
COMMIT_UNPERSISTED = "unpersisted"  # mutator chạy nhưng KHÔNG persist được


def generate_credentials(prefix: str = "player",
                         password_length: int = 16,
                         rng: Optional[random.Random] = None) -> Dict[str, str]:
    """Sinh credentials random: username = prefix + 6 alphanumeric lowercase,
    password ``password_length`` ký tự đảm bảo đủ 4 nhóm ký tự.

    Truyền ``random.Random(seed)`` để chạy deterministic (dùng trong test).
    """
    rng = rng or random.Random()
    suffix = "".join(rng.choice(_USERNAME_ALPHABET) for _ in range(6))
    username = f"{prefix}{suffix}"

    while password_length < 4:
        password_length = 4  # tối thiểu 4 để đảm bảo đủ 4 nhóm
    pools = [_PW_LOWER, _PW_UPPER, _PW_DIGIT, _PW_SPECIAL]
    chars = [rng.choice(pool) for pool in pools]
    all_chars = "".join(pools)
    chars += [rng.choice(all_chars)
              for _ in range(password_length - len(chars))]
    rng.shuffle(chars)
    return {"username": username, "password": "".join(chars)}


class RegisterService:
    """Điều phối `ctf register`: sinh credential -> gọi platform.register ->
    lưu auth map -> in kết quả. Mỗi lần run() tạo ĐÚNG 1 tài khoản."""

    RATE_LIMIT_SECONDS = 60

    def __init__(self,
                 now_fn: Callable[[], float] = time.time,
                 sleep_fn: Callable[[float], None] = time.sleep,
                 config_loader: Callable[[], Dict] = load_global_config,
                 config_saver: Optional[Callable[[Dict], Any]] = None,
                 tempmail_factory: Callable[[], TempMailClient] = TempMailClient,
                 detect_fn: Optional[Callable] = None,
                 config_updater: Optional[Callable[[Callable], Any]] = None):
        self._now = now_fn
        self._sleep = sleep_fn
        self._load_cfg = config_loader
        # Legacy seam (trước đây dùng cho mọi lần ghi); từ hunt-c18 mọi GHI
        # đi qua ``config_updater`` (đọc-mutate-ghi trong khóa flock). Vẫn
        # nhận tham số để giữ tương thích caller cũ; mặc định nạp hàm thật.
        self._save_cfg = config_saver or save_global_config
        self._update_cfg = config_updater if config_updater is not None \
            else (lambda mutator: update_global_config(mutator))
        self._tempmail_factory = tempmail_factory
        if detect_fn is None:
            # import muộn để tránh vòng phụ thuộc khi chỉ dùng unit-test thuần
            from ..platforms.detection import detect_platform_info
            detect_fn = detect_platform_info
        self._detect = detect_fn

    # ------------------------------------------------------------------ #
    # Van-an-toàn §4: rate limit >= 60s giữa 2 lần register cùng URL
    # ------------------------------------------------------------------ #
    def _check_rate_limit(self, cfg: Dict, url_key: str) -> float:
        """Trả về số giây còn phải chờ (0.0 = được phép)."""
        state = cfg.get("register_state") or {}
        last = state.get(url_key) or {}
        last_ts = float(last.get("last_attempt_ts") or 0)
        elapsed = self._now() - last_ts
        remaining = self.RATE_LIMIT_SECONDS - elapsed
        return max(0.0, remaining)

    def _record_attempt(self, cfg: Dict, url_key: str) -> Dict:
        state = cfg.setdefault("register_state", {})
        entry = state.setdefault(url_key, {})
        entry["last_attempt_ts"] = self._now()
        return cfg

    def _commit_attempt(self, url_key: str,
                        auth_key: Optional[str] = None,
                        auth_entry: Optional[Dict[str, Any]] = None) -> str:
        """[hunt-c18 BUG-2, MED] Ghi nhận attempt (+ auth entry tùy chọn)
        NGUYÊN TỬ chống TOCTOU: đọc-mutate-ghi TRONG CÙNG khóa flock của
        global config, và re-check rate limit trên state MỚI NHẤT TRÊN ĐĨA
        — không phải cfg đã load từ đầu lần chạy (network + xác minh email
        có thể kéo dài hàng phút, hai CLI song song cùng URL đều pass check
        ban đầu).

        Trả về (review c18-2, LOW — tách ba trạng thái thay vì bool):
          - ``COMMIT_OK``         — attempt đã ghi thành công;
          - ``COMMIT_PREEMPTED``  — một tiến trình khác vừa ghi attempt cùng
            URL trong lúc mình chạy (thua cuộc): KHÔNG ghi gì để không đè
            timestamp của tiến trình thắng cuộc hay lost-update phần còn
            lại của config;
          - ``COMMIT_UNPERSISTED`` — mutator chạy bình thường nhưng updater
            vẫn không trả state (thư mục config biến mất) HOẶC OSError
            (PermissionError... khi đọc/ghi file) — warning được log rõ,
            exception KHÔNG lan qua run() che exit-code mapping.
        """
        preempted = {"wait": 0.0}

        def _mut(fresh: Dict[str, Any]):
            wait = self._check_rate_limit(fresh, url_key)
            if wait > 0:
                preempted["wait"] = wait
                return SKIP_WRITE
            self._record_attempt(fresh, url_key)
            if auth_entry is not None and auth_key:
                fresh.setdefault("auth", {})[auth_key] = auth_entry
            return fresh

        try:
            result = self._update_cfg(_mut)
        except OSError as exc:
            Logger.warning(
                "Không ghi được register_state vào global config "
                f"({exc.__class__.__name__}: {exc}) — rate-limit giữa các "
                f"lần chạy có thể không còn hiệu lực cho URL này.")
            return COMMIT_UNPERSISTED
        if preempted["wait"] > 0:
            return COMMIT_PREEMPTED
        if result is None:
            # Review c18-2 (LOW): mutator KHÔNG trả SKIP mà updater vẫn trả
            # None -> thư mục chứa config biến mất (locked_update_json
            # không hồi sinh dir). Phân biệt rõ với thua cuộc ở trên thay
            # vì báo thành công.
            Logger.warning(
                "Không ghi được register_state: thư mục global config đã "
                "biến mất — rate-limit giữa các lần chạy có thể không còn "
                f"hiệu lực cho URL này.")
            return COMMIT_UNPERSISTED
        return COMMIT_OK

    def _set_auth_entry(self, key: str, entry: Dict[str, Any]) -> None:
        """Merge auth entry vào global config NGUYÊN TỬ qua khóa flock
        (trước đây load-stale-save — cửa sổ RMW gây lost update với process
        khác). Không re-check rate limit: attempt của chính mình đã được
        commit ngay sau register rồi.

        Review c18-2 (LOW): OSError từ storage KHÔNG lan qua run() — account
        ĐÃ tạo phía server nên credentials (đã in) là tài sản quan trọng
        nhất; lỗi persist auth chỉ cần log rõ để user backup thủ công."""
        def _mut(fresh: Dict[str, Any]) -> Dict[str, Any]:
            fresh.setdefault("auth", {})[key] = entry
            return fresh

        try:
            self._update_cfg(_mut)
        except OSError as exc:
            Logger.warning(
                f"Không lưu được auth[{key}] vào global config "
                f"({exc.__class__.__name__}: {exc}) — hãy backup credentials "
                f"đã in ở trên thủ công.")

    @staticmethod
    def _print_warnings(url: str) -> None:
        """Cảnh báo van-an-toàn bắt buộc trước khi tạo bất kỳ tài khoản nào.

        PHOSPHOR: mỗi dòng gắn glyph ``!`` warn amber (spec §3.2), không emoji,
        không đường kẻ ``=`` full-width (spec §5 cấm lặp lại nhiều hơn 1 lần).
        """
        def w(msg: str) -> None:
            console.print(f"[{WARN}]![/{WARN}] {msg}")

        console.print(
            f"[{WARN}]![/{WARN}] "
            f"[bold {FG_BASE}]VAN AN TOÀN AUTO-REGISTER[/bold {FG_BASE}]")
        w(f"Một số giải [bold]cấm nhiều tài khoản/người[/bold] — đảm bảo "
          f"tuân thủ rules của giải ([{INFO}]{escape(url)}[/{INFO}]).")
        w("Tool sẽ tạo [bold]ĐÚNG 1 tài khoản[/bold] cho lần chạy này "
          "(không hỗ trợ batch/loop theo thiết kế).")
        w("Nếu platform bật captcha (Turnstile/reCAPTCHA/hCaptcha), tool "
          "sẽ DỪNG và bạn cần đăng ký thủ công.")

    @staticmethod
    def _print_credentials(creds: Dict[str, str], created: bool = True) -> None:
        """Khối credentials PHOSPHOR: dòng ``✔ Đã tạo tài khoản <user>``
        solved-green khi thành công; nhãn cột faint UPPERCASE; URL/literal
        info-cyan. Với ``created=False`` (auto-register bị chặn → hướng dẫn
        thủ công) chỉ liệt kê credentials, không có ✔."""
        if created:
            console.print(
                f"[{SOLVED}]✔[/{SOLVED}] Đã tạo tài khoản "
                f"[bold {FG_BASE}]{escape(creds.get('username', ''))}"
                f"[/bold {FG_BASE}]")
        for label, color in (("URL", INFO), ("USERNAME", FG_BASE),
                             ("PASSWORD", FG_BASE), ("EMAIL", FG_BASE)):
            val = creds.get(label.lower())
            if not val:
                continue
            console.print(
                f"  [{FG_FAINT}]{label:<9}[/{FG_FAINT}]"
                f"[{color}]{escape(val)}[/{color}]")

    # ------------------------------------------------------------------ #
    # Auth map persistence (global config)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _auth_key(workspace: Optional[str], url: str) -> str:
        """Key trong auth map: đường dẫn workspace tuyệt đối nếu có, ngược lại URL."""
        if workspace:
            abs_ws = os.path.abspath(workspace)
            if os.path.isdir(abs_ws):
                return abs_ws
        return url

    @staticmethod
    def _cookies_to_header(cookies: Dict[str, str]) -> str:
        return "; ".join(f"{k}={v}" for k, v in (cookies or {}).items())

    # ------------------------------------------------------------------ #
    # Email: --email | --tempmail | fallback tempmail tự thử
    # ------------------------------------------------------------------ #
    def _resolve_email(self, email_arg: Optional[str],
                       use_tempmail: bool) -> Dict[str, Any]:
        """Trả {'email', 'tempmail': client|None, 'verified_flow': bool}.

        Không truyền gì -> tự thử tempmail; lỗi -> báo user dùng --email.
        """
        if email_arg:
            return {"email": email_arg, "tempmail": None, "verified_flow": False}

        try:
            client = self._tempmail_factory()
            address, _mail_pw, _token = client.create_mailbox()
            console.print(
                f"[{INFO}]ℹ Tempmail sẵn sàng: {escape(address)}[/{INFO}] "
                f"[{FG_MUTED}](mail.tm)[/{FG_MUTED}]")
            return {"email": address, "tempmail": client, "verified_flow": True}
        except TempMailError as exc:
            if use_tempmail:
                raise RuntimeError(
                    f"Không tạo được mailbox tạm (--tempmail): {exc}. "
                    "Hãy tự cung cấp --email.") from exc
            Logger.warning(f"Tempmail lỗi ({exc}) — hãy chạy lại với "
                           "--email <dia-chi> nếu platform bắt nhập email.")
            raise RuntimeError(
                "Thiếu email đăng ký: truyền --email me@x.com hoặc --tempmail."
            ) from exc

    def _make_verify_hook(self, client: Optional[TempMailClient],
                          timeout_s: float = 120.0):
        """Hook xác minh email (CTFd): poll tempmail <=120s, GET link /confirm/<token>."""
        if client is None:
            return None

        def hook(session) -> bool:
            Logger.info("Đang chờ thư xác nhận từ platform (tối đa "
                        f"{int(timeout_s)}s)...")
            msg = client.wait_for_message(timeout_s=timeout_s)
            if msg is None:
                Logger.error("Hết giờ chưa nhận được thư xác nhận — kiểm tra "
                             "thủ công hộp thư tạm hoặc đăng nhập web để resend.")
                return False
            content = client.fetch_message_text(msg.get("id", ""))
            link = TempMailClient.find_confirm_link(content)
            if not link:
                Logger.error("Thư xác nhận không chứa link /confirm/<token> — "
                             "xác minh thủ công.")
                return False
            try:
                resp = session.get(link, timeout=20)
                # Hunt-c18 BUG-6 (LOW): biểu thức cũ kết thúc ``or True`` —
                # mọi HTTP status đều được tính là verified (check chết).
                # requests tự follow redirect nên chuỗi confirm chuẩn luôn
                # kết thúc 200; 404/500 phải trả False để user biết phải
                # xác minh thủ công.
                status = getattr(resp, "status_code", 0)
                if status == 200:
                    Logger.success(f"Đã mở link xác nhận ({link}) "
                                   f"-> HTTP {status}.")
                    return True
                Logger.error(f"Link xác nhận trả HTTP {status or '?'} — "
                             "chưa xác minh được email, kiểm tra thủ công.")
                return False
            except Exception as exc:
                Logger.error(f"Mở link xác nhận thất bại: {exc}")
                return False

        return hook

    # ------------------------------------------------------------------ #
    # Orchestrator — ĐÚNG 1 lần register mỗi lần gọi
    # ------------------------------------------------------------------ #
    def run(self, url: str,
            email: Optional[str] = None,
            use_tempmail: bool = False,
            username_prefix: str = "player",
            password: Optional[str] = None,
            workspace: Optional[str] = None) -> Dict[str, Any]:
        """Chạy auto-register. Returns dict trạng thái; raise RuntimeError khi
        đầu vào sai / bị rate-limit; PlatformRegisterUnsupported khi platform
        chặn (captcha...) — caller (CLI) map sang exit code."""
        if not url:
            raise RuntimeError("Thiếu URL platform (-u https://ctf.example.com).")

        url = url.rstrip("/")
        self._print_warnings(url)

        cfg = self._load_cfg()
        wait = self._check_rate_limit(cfg, url)
        if wait > 0:
            raise RuntimeError(
                f"Rate limit: URL này vừa được register cách đây chưa đầy "
                f"{self.RATE_LIMIT_SECONDS}s — thử lại sau {int(wait)}s nữa.")

        creds = generate_credentials(username_prefix)
        creds["password"] = password or creds["password"]

        mail = self._resolve_email(email, use_tempmail)
        reg_email = mail["email"]

        from ..services.session_factory import create_session
        session = create_session()

        Logger.info(f"Phát hiện loại platform tại {url} ...")
        platform, info = self._detect(url, session)
        console.print(
            f"[{FG_FAINT}]·[/{FG_FAINT}] Platform: "
            f"[{INFO}]{escape(info.platform_type)}[/{INFO}] "
            f"[{FG_MUTED}](confidence={info.confidence})[/{FG_MUTED}]")

        hook = self._make_verify_hook(mail["tempmail"])
        try:
            result = platform.register(
                username=creds["username"], email=reg_email,
                password=creds["password"], verify_email_hook=hook)
        except PlatformRegisterUnsupported as exc:
            # Hunt-c18 BUG-7 (LOW): nhánh captcha CŨNG là MỘT lần attempt —
            # trước đây re-raise KHÔNG ghi nhận nên chạy lại liền nhau bypass
            # rate limit. Ghi NGAY (atomic anti-TOCTOU) trước khi re-raise;
            # thua cuộc race thì giữ nguyên attempt của tiến trình thắng.
            if self._commit_attempt(url) == COMMIT_PREEMPTED:
                Logger.warning(
                    "Một tiến trình khác vừa ghi nhận register cùng URL khi "
                    "lần chạy này đang diễn ra — giữ nguyên attempt của "
                    "tiến trình đó.")
            # COMMIT_UNPERSISTED: warning đã log tại _commit_attempt.
            # Diagnostic-style (spec §4.6): ✗ kết quả → ╰─▶ hướng dẫn thủ công.
            console.print(
                f"[bold {ERROR}]✗[/bold {ERROR}] "
                f"[bold]{escape(str(exc))}[/bold]")
            console.print(
                f"  [{FG_FAINT}]╰─▶[/{FG_FAINT}] "
                f"[{FG_MUTED}]đăng ký thủ công bằng trình duyệt với credentials "
                f"dưới đây rồi cấu hình cookie/token như bình thường.[/{FG_MUTED}]")
            self._print_credentials({**creds, "url": url, "email": reg_email},
                                    created=False)
            raise
        except Exception as exc:
            # Van-an-toàn R2: exception thường giữa flow (mạng/tempmail chết
            # giữa xác minh email...) — account CÓ THỂ đã tồn tại server-side
            # nên credentials KHÔNG được mất, và attempt vẫn phải được ghi
            # nhận để rate-limit không bị bypass.
            Logger.error(f"Register lỗi giữa chừng: {str(exc)[:200]}")
            if self._commit_attempt(url) == COMMIT_PREEMPTED:
                Logger.warning(
                    "Một tiến trình khác vừa ghi nhận register cùng URL khi "
                    "lần chạy này đang diễn ra — giữ nguyên attempt của "
                    "tiến trình đó.")
            # COMMIT_UNPERSISTED: warning đã log tại _commit_attempt.
            self._print_credentials({**creds, "url": url, "email": reg_email},
                                    created=False)
            raise

        # Van-an-toàn: ghi nhận MỌI lần attempt để siết rate limit — giờ là
        # atomic: re-check trên state mới nhất TRONG khóa (hunt-c18 BUG-2);
        # thua cuộc race nghĩa là một CLI song song vừa tạo tài khoản cùng
        # URL → dừng sạch thay vì tạo trùng.
        if self._commit_attempt(url) == COMMIT_PREEMPTED:
            raise RuntimeError(
                "Rate limit: một tiến trình khác vừa đăng ký cùng URL trong "
                "khi lần chạy này đang diễn ra — dừng để tránh tạo trùng "
                "tài khoản.")
        # COMMIT_UNPERSISTED (review c18-2, LOW): account ĐÃ tạo phía server
        # — KHÔNG chặn flow ở đây (trước đây OSError lan qua run() che
        # exit-code mapping và nuốt mất credentials); warning đã log rõ,
        # credentials vẫn được in + auth vẫn thử lưu bên dưới.

        if not result.get("ok"):
            Logger.error(f"Register thất bại: {result.get('message')}")
            return {"ok": False, "message": result.get("message"),
                    "credentials": {**creds, "email": reg_email}}

        self._print_credentials({**creds, "url": url, "email": reg_email})

        # Lưu auth map: ưu tiên key = workspace tuyệt đối (nếu là dir thật),
        # ngược lại key = URL. Cookie/token serialize để `ctf pull -w` dùng lại.
        auth_entry = {
            "username": creds["username"],
            "password": creds["password"],
            "email": reg_email,
            "registered_at": int(self._now()),
        }
        token = result.get("token")
        cookies = result.get("cookies") or {
            c.name: c.value for c in session.cookies}
        if cookies:
            auth_entry["cookie"] = self._cookies_to_header(cookies)
        if token:
            auth_entry["token"] = token

        # Hunt-c18 BUG-2: auth map merge NGUYÊN TỬ qua updater (đọc-mutate-
        # ghi trong khóa) thay vì load-stale-save; attempt đã commit ở bước
        # trên nên không ghi đè timestamp lần nữa.
        key = self._auth_key(workspace, url)
        self._set_auth_entry(key, auth_entry)

        saved_as = ("workspace " + key) if workspace and \
            os.path.isdir(os.path.abspath(workspace)) else f"URL {key}"
        # Hint cyan (token info): path/literal đi kèm màu lạnh duy nhất.
        console.print(
            f"[{INFO}]ℹ credentials đã lưu vào global config — "
            f"auth[{escape(saved_as)}][/{INFO}]")

        return {
            "ok": True,
            "platform": info.platform_type,
            "credentials": {**creds, "email": reg_email},
            "auth_key": key,
            "email_verified": result.get("email_verified"),
            "message": result.get("message"),
        }
