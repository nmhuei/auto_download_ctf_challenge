"""SniperService — P2-6 "Sniper first-blood": preload flag + nộp tự động ngay
khi giải mở (giây đầu tiên của event window).

QUYẾT ĐỊNH FORMAT FILE: **JSON** (``<ws>/sniper.json``), KHÔNG phải YAML.
Lý do: subset YAML cần ở đây chỉ là "key: value + list item" phẳng — thêm
pyyaml là dependency mới trong khi ``json`` stdlib đã cover đủ 100% nhu cầu
(user edit tay bằng editor bất kỳ vẫn ok). Nếu sau này muốn hỗ trợ YAML thì
thêm alias load, không đổi API.

File ``sniper.json`` do user tự điền (từ leak/quiz/warmup):

.. code-block:: json

    [
      {"challenge": "crypto-warmup", "flag": "FLAG{...}", "delay_seconds": 0},
      {"challenge": 12, "flag": "FLAG{...}", "delay_seconds": 5}
    ]

- ``challenge``     : id hoặc name (delegate resolve cho SubmitService).
- ``flag``          : flag preload.
- ``delay_seconds`` : số giây SAU KHI window mở mới bắn (default 0).

VAN AN TOÀN (spec P2-6 §3):
1. Chỉ submit khi ĐÃ vào window (>= ``ctf_info.event_window.start``) — không
   bao giờ bắn sớm hơn start (tránh ban). Không biết start → yêu cầu caller
   truyền ``--start-at`` ISO.
2. Mỗi target tối đa 1 lần thử; sai → bị SubmitService blacklist tự động
   (dùng đúng gate/throttle sẵn có của SubmitService). Chỉ khi bật
   ``retry_wrong`` target sai mới được thử lại, và luôn qua ``force=True``
   kèm trần ``MAX_ATTEMPTS_PER_TARGET``.
3. In cảnh báo automation ngay khi chạy: một số giải cấm automation — tuân
   thủ rules.

Verdict xử lý mỗi phát bắn:
- ``correct``     : ◆ FB first-blood (solved green) — bỏ khỏi hàng chờ.
- ``incorrect``   : SubmitService đã ghi blacklist (submit_history) — bỏ khỏi
                    hàng chờ (trừ khi ``retry_wrong``).
- ``ratelimited`` : KHÔNG tiêu lượt thử — backoff luỹ thừa (tôn trọng throttle
                    platform; SubmitService._throttle vẫn chạy bình thường
                    bên trong), rồi thử lại.
- khác/unknown    : tính như đã tiêu lượt thử (tránh vòng lặp vô hạn).

Dừng khi hết target hoặc Ctrl-C (bắt KeyboardInterrupt sạch, in trạng thái
các target còn lại).

WIRE CLI SAU (cli.py đang bận — chưa wire, gọi trực tiếp như sau)::

    from ctf_downloader.storage.workspace_repo import WorkspaceRepo
    from ctf_downloader.services.submit_service import SubmitService
    from ctf_downloader.services.sniper_service import SniperService

    repo = WorkspaceRepo(ws_dir)
    submitter = SubmitService(url=url, cookie=cookie, workspace_dir=ws_dir)
    SniperService(repo, submitter).run(
        poll_interval=10,
        start_at="2026-08-30T00:00:00Z",  # --start-at nếu challenges.json thiếu window
        retry_wrong=False,                # --retry-wrong
    )
"""
import json
import math
import time
from typing import Any, Dict, List, Optional, Tuple

from rich.markup import escape
from rich.table import Table

from ..platforms.base import normalize_epoch_to_utc
from ..ui.theme import ACCENT, ERROR, FG_BASE, FG_FAINT, FG_MUTED, SOLVED, WARN
from ..utils.logger import console

SNIPER_FILENAME = "sniper.json"

# Backoff khi bị rate-limit (luỹ thừa, có trần).
BACKOFF_BASE_SECONDS = 30.0
BACKOFF_MAX_SECONDS = 600.0

# Trần số lần thử/target khi bật retry_wrong (chống spam flag chết).
MAX_ATTEMPTS_PER_TARGET = 3

# Trần số lần rate-limit liên tiếp trước khi bỏ cuộc (chống vòng lặp vô hạn
# khi last_verdict kẹt ở "ratelimited" vì lý do khác).
MAX_CONSECUTIVE_RATELIMITS = 20

# Cảnh báo automation (PHOSPHOR: glyph ``!`` warn gắn tại chỗ in, không emoji).
AUTOMATION_WARNING = (
    "Một số giải CTF cấm automation/auto-submit — hãy đọc kỹ rules của giải "
    "và tuân thủ. Sniper chỉ bắn sau khi window mở."
)


# ----------------------------------------------------------------------
# Output helpers — PHOSPHOR FIELD KIT (spec §3): màu luôn đi kèm glyph,
# không emoji chrome; challenge/flag/message là dữ liệu → escape markup.
# ----------------------------------------------------------------------

def _warn(msg: str) -> None:
    console.print(f"[{WARN}]![/{WARN}] {escape(msg)}")


def _fail(msg: str) -> None:
    console.print(f"[{ERROR}]✗[/{ERROR}] {escape(msg)}")


def _dim(msg: str) -> None:
    """Trạng thái chờ — dim, dot faint làm bullet."""
    console.print(f"[{FG_FAINT}]·[/{FG_FAINT}] [dim]{escape(msg)}[/dim]")


def _cause(msg: str) -> None:
    """Chuỗi nhân quả ``╰─▶`` (spec §5 whitelist đường kẻ)."""
    console.print(
        f"  [{FG_FAINT}]╰─▶[/{FG_FAINT}] "
        f"[{FG_MUTED}]{escape(msg)}[/{FG_MUTED}]"
    )


class SniperService:
    """Preload flag và nộp tự động đúng lúc window mở (first-blood race)."""

    def __init__(self, repo, submit_service):
        """
        :param repo:           WorkspaceRepo của workspace mục tiêu.
        :param submit_service: SubmitService đã cấu hình url/cookie — mọi phát
                               bắn đi qua ``submit_service.submit()`` để hưởng
                               đúng gate flag-format + blacklist + throttle.
        """
        self.repo = repo
        self.submitter = submit_service

    # ------------------------------------------------------------------
    # Load targets
    # ------------------------------------------------------------------

    def load_targets(self, path: Optional[Any] = None) -> List[Dict[str, Any]]:
        """Đọc ``<ws>/sniper.json`` → list target đã validate + sort theo delay.

        Nhận cả top-level list lẫn ``{"targets": [...]}``. Entry thiếu
        ``challenge``/``flag``, sai kiểu JSON, hay file hỏng → bỏ qua với
        cảnh báo (không bao giờ raise).
        """
        path = path if path is not None else self.repo.root / SNIPER_FILENAME
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()   # C15-9: context manager — không rò fd
        except FileNotFoundError:
            _warn(
                f"Không tìm thấy {SNIPER_FILENAME} trong workspace — tạo file "
                f"(list các {{challenge, flag, delay_seconds}}) để dùng sniper."
            )
            return []
        except OSError as e:
            _fail(f"Không đọc được {path}: {e}")
            return []

        try:
            data = json.loads(text)
        except ValueError as e:
            _fail(f"{SNIPER_FILENAME} hỏng (JSON không hợp lệ): {e}")
            return []

        if isinstance(data, dict):
            data = data.get("targets")
        if not isinstance(data, list):
            _fail(f"{SNIPER_FILENAME}: nội dung phải là list hoặc {{'targets': [...]}}.")
            return []

        targets: List[Dict[str, Any]] = []
        for idx, raw in enumerate(data):
            if not isinstance(raw, dict):
                _warn(f"{SNIPER_FILENAME}: bỏ qua entry #{idx} (không phải object).")
                continue
            raw_challenge = raw.get("challenge")
            if isinstance(raw_challenge, str):
                raw_challenge = raw_challenge.strip()
            challenge = raw_challenge
            flag = str(raw.get("flag") or "").strip()
            if not challenge or not flag:
                _warn(
                    f"{SNIPER_FILENAME}: bỏ qua entry #{idx} (thiếu 'challenge' hoặc 'flag')."
                )
                continue
            try:
                delay = max(0.0, float(raw.get("delay_seconds") or 0))
            except (TypeError, ValueError):
                _warn(f"{SNIPER_FILENAME}: entry '#{idx}' delay_seconds sai kiểu → 0.")
                delay = 0.0
            if not math.isfinite(delay):
                # C15-8: json.loads stdlib nhận sẵn Infinity/NaN — Infinity
                # xuyên qua max(0.0, inf) khiến target KHÔNG BAO GIỜ due,
                # run() polling vô hạn. Ép về 0 an toàn (NaN đã tự về 0 do
                # max, nhưng chặn lại một chỗ cho tường minh).
                _warn(f"{SNIPER_FILENAME}: entry '#{idx}' delay_seconds không "
                      f"hữu hạn ({raw.get('delay_seconds')}) → 0.")
                delay = 0.0
            targets.append({
                "challenge": challenge,
                "flag": flag,
                "delay_seconds": delay,
            })

        # C15-7: dedup theo (challenge, flag) GIỮ THỨ TỰ khai báo — target
        # trùng bị bắn 2 phát là lãng phí request/rủi ro rate-limit, phát 1
        # sai thì blacklist ăn ngay phát 2.
        seen: set = set()
        unique_targets: List[Dict[str, Any]] = []
        for t in targets:
            key = (str(t["challenge"]), t["flag"])
            if key in seen:
                continue
            seen.add(key)
            unique_targets.append(t)
        targets = unique_targets

        # Sort ổn định theo delay_seconds (thứ tự khai báo giữ nguyên khi bằng nhau)
        targets.sort(key=lambda t: t["delay_seconds"])
        return targets

    # ------------------------------------------------------------------
    # Start-time resolution
    # ------------------------------------------------------------------

    def resolve_start(self, start_at: Optional[str] = None) -> Optional[float]:
        """Epoch-giây thời điểm mở giải, hoặc ``None`` nếu không xác định được.

        Ưu tiên ``--start-at`` (ISO 8601 hoặc epoch) nếu có; nếu không thì đọc
        ``challenges.json.ctf_info.event_window.start`` (qua
        ``normalize_epoch_to_utc`` — hiểu cả epoch giây/ms lẫn ISO).
        """
        if start_at:
            dt = normalize_epoch_to_utc(str(start_at))
            if dt is None:
                raise ValueError(
                    f"--start-at không hợp lệ: '{start_at}' (cần ISO 8601 hoặc epoch giây)."
                )
            return dt.timestamp()
        try:
            ctf_info = self.repo.read_challenges().get("ctf_info") or {}
        except Exception:
            return None
        dt = normalize_epoch_to_utc((ctf_info.get("event_window") or {}).get("start"))
        return dt.timestamp() if dt else None

    # ------------------------------------------------------------------
    # Fire một target
    # ------------------------------------------------------------------

    def _history_entry(self, flag: str) -> Optional[Dict[str, Any]]:
        fl = (flag or "").strip()
        for e in getattr(self.submitter, "submit_history", []) or []:
            if isinstance(e, dict) and str(e.get("flag", "")).strip() == fl:
                return e
        return None

    def _classify_result(
        self,
        flag: str,
        message: str,
        prev_verdict: Optional[str],
    ) -> Tuple[str, str]:
        """Phân loại kết quả một phát bắn → ('correct'|'incorrect'|'ratelimited'|'unknown', msg).

        Nguồn chân lý theo thứ tự:
        1. Entry submit_history của flag (SubmitService vừa ghi) — correct/incorrect.
        2. ``platform.last_verdict`` == 'ratelimited' (không ghi lịch sử).
        3. Còn lại → unknown.
        """
        result = ((self._history_entry(flag) or {}).get("result")) or ""
        if result == "correct":
            return "correct", message
        if result == "incorrect":
            return "incorrect", message
        verdict = getattr(getattr(self.submitter, "platform", None), "last_verdict", None)
        if verdict == "ratelimited" and verdict != prev_verdict:
            return "ratelimited", message
        if verdict == "ratelimited":
            # last_verdict kẹt ở ratelimited từ phát trước mà lịch sử không đổi:
            # vẫn coi là ratelimited (nhưng run() có trần liên tiếp để thoát).
            return "ratelimited", message
        return "unknown", message

    def _fire(self, target: Dict[str, Any], force: bool) -> Tuple[str, str]:
        """Bắn 1 phát cho target qua SubmitService. Trả (kind, message)."""
        prev_verdict = getattr(getattr(self.submitter, "platform", None), "last_verdict", None)
        try:
            _success, message = self.submitter.submit(
                target["challenge"], target["flag"], force=force
            )
        except Exception as exc:  # network lỗi v.v. — không làm rơi cả sniper
            return "unknown", f"lỗi submit: {exc}"
        return self._classify_result(target["flag"], message, prev_verdict)

    # ------------------------------------------------------------------
    # Run loop
    # ------------------------------------------------------------------

    @staticmethod
    def _human_wait(seconds: float) -> str:
        seconds = max(0, int(round(seconds)))
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}h{m:02d}m{s:02d}s" if h else (f"{m}m{s:02d}s" if m else f"{s}s")

    def _print_remaining(self, pending: List[Dict[str, Any]]) -> None:
        """Bảng tổng kết target chưa bắn được — heading faint UPPERCASE,
        không viền/kẻ dọc (spec §5: khoảng trắng trước đường kẻ)."""
        if not pending:
            return
        console.print()
        console.print(f"[bold {FG_FAINT}]SNIPER · TARGETS CÒN LẠI[/bold {FG_FAINT}]")
        table = Table(show_header=True, header_style=f"bold {FG_FAINT}", box=None)
        table.add_column("CHALLENGE", style=FG_BASE)
        table.add_column("FLAG", style=FG_MUTED)
        table.add_column("ATTEMPTS", justify="right", style=FG_BASE)
        for t in pending:
            table.add_row(
                escape(str(t.get("challenge"))),
                escape(t.get("flag", "")),
                str(t.get("attempts", 0)),
            )
        console.print(table)

    def run(
        self,
        poll_interval: float = 10.0,
        start_at: Optional[str] = None,
        retry_wrong: bool = False,
    ) -> Dict[str, Any]:
        """Chờ đến giờ G rồi bắn lần lượt theo hàng chờ (xem docstring module).

        :param poll_interval: chu kỳ ngủ giữa các tick khi chờ/backoff (giây).
        :param start_at:      ISO 8601/epoch override — bắt buộc khi
                              challenges.json không có event_window.start.
        :param retry_wrong:   cho phép thử lại target sai (force=True, tối đa
                              ``MAX_ATTEMPTS_PER_TARGET`` lần/target).
        :return: summary dict {solved, failed, pending, started_at, aborted}.
        """
        # Clamp phòng thủ: argparse không validate --poll; giá trị <= 0
        # truyền thẳng vào time.sleep(min(poll, delta)) khi chờ giờ G sẽ nổ
        # ValueError traceback. Service là lớp mỏng cuối cùng nên tự bảo vệ.
        poll_interval = max(1.0, float(poll_interval))
        _warn(AUTOMATION_WARNING)

        targets = self.load_targets()
        summary: Dict[str, Any] = {
            "solved": [],
            "failed": [],
            "pending": [],
            "started_at": None,
            "aborted": False,
        }
        if not targets:
            _dim("Sniper: không có target nào — thoát.")
            return summary

        try:
            start_ts = self.resolve_start(start_at)
        except ValueError as e:
            _fail(str(e))
            summary["pending"] = targets
            return summary
        if start_ts is None:
            _fail(
                "Không xác định được thời điểm mở giải (thiếu "
                "challenges.json.ctf_info.event_window.start)."
            )
            _cause("truyền --start-at <ISO> để sniper biết giờ G.")
            summary["pending"] = targets
            return summary

        summary["started_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_ts))
        pending: List[Dict[str, Any]] = [dict(t, attempts=0) for t in targets]
        solved: List[Dict[str, Any]] = []
        failed: List[Dict[str, Any]] = []

        # ---- Van an toàn #1: KHÔNG bao giờ bắn sớm hơn start ----
        # C12-S1: try mở TỪ vòng chờ giờ G — Ctrl-C lúc canh giờ phải abort
        # sạch (docstring hứa), không nổ traceback ra ngoài run().
        try:
            while True:
                now = time.time()
                if now >= start_ts:
                    break
                _dim(
                    f"còn {self._human_wait(start_ts - now)} tới giờ G "
                    f"({summary['started_at']}) — đang canh…"
                )
                time.sleep(min(float(poll_interval), start_ts - now))
            console.print(
                f"[{ACCENT}]◆[/{ACCENT}] "
                f"[bold {FG_BASE}]Window mở[/bold {FG_BASE}] "
                f"[{FG_MUTED}]— sniper bắt đầu bắn theo hàng chờ.[/{FG_MUTED}]"
            )

            backoff_until = 0.0
            backoff_step = BACKOFF_BASE_SECONDS
            consecutive_ratelimits = 0

            while pending:
                now = time.time()

                due = [t for t in pending if now >= start_ts + t["delay_seconds"]]
                if not due:
                    next_due = min(start_ts + t["delay_seconds"] for t in pending)
                    wake = min(next_due, now + poll_interval)
                    time.sleep(max(0.0, min(wake - now, poll_interval)))
                    continue

                # Rate-limit backoff: tôn trọng throttle của platform, KHÔNG
                # tiêu lượt thử của target nào trong lúc backoff.
                if now < backoff_until:
                    time.sleep(min(float(poll_interval), backoff_until - now))
                    continue

                target = due[0]
                force = retry_wrong and target["attempts"] > 0
                target["attempts"] += 1
                # Log phát bắn: `◆ <challenge> → flag…` (accent khi bắn).
                console.print(
                    f"[{ACCENT}]◆ {escape(str(target['challenge']))}[/{ACCENT}]"
                    f" [{FG_FAINT}]→[/{FG_FAINT}]"
                    f" [{ACCENT}]{escape(target['flag'])}…[/{ACCENT}]"
                )
                kind, message = self._fire(target, force=force)

                if kind == "correct":
                    consecutive_ratelimits = 0
                    pending.remove(target)
                    solved.append(target)
                    console.print(
                        f"[bold {SOLVED}]◆ FB[/bold {SOLVED}] "
                        f"[bold {FG_BASE}]{escape(str(target['challenge']))}[/"
                        f"bold {FG_BASE}]"
                        f" [{FG_MUTED}]— {escape(message)}[/{FG_MUTED}]"
                    )

                elif kind == "incorrect":
                    consecutive_ratelimits = 0
                    # SubmitService ĐÃ tự blacklist flag này trong submit_history
                    # (gate sẵn có). Không retry trừ khi --retry-wrong.
                    if retry_wrong and target["attempts"] < MAX_ATTEMPTS_PER_TARGET:
                        _warn(
                            f"Sai (lần {target['attempts']}/"
                            f"{MAX_ATTEMPTS_PER_TARGET}) — sẽ thử lại "
                            f"{target['challenge']}: {message}"
                        )
                    else:
                        pending.remove(target)
                        failed.append(target)
                        _fail(f"SAI — {target['challenge']}: {message}")
                        _cause("flag đã bị blacklist tự động trong "
                               "submit_history — bỏ khỏi hàng chờ.")

                elif kind == "ratelimited":
                    consecutive_ratelimits += 1
                    if consecutive_ratelimits >= MAX_CONSECUTIVE_RATELIMITS:
                        _fail(
                            f"Bị rate-limit {consecutive_ratelimits} lần "
                            f"liên tiếp — dừng sniper."
                        )
                        break
                    _warn(
                        f"Rate-limited — backoff {backoff_step:.0f}s (target "
                        f"'{target['challenge']}' giữ nguyên lượt thử)."
                    )
                    backoff_until = time.time() + backoff_step
                    backoff_step = min(backoff_step * 2, BACKOFF_MAX_SECONDS)

                else:  # unknown — tính như đã tiêu lượt thử để tránh vòng lặp
                    consecutive_ratelimits = 0
                    if retry_wrong and target["attempts"] < MAX_ATTEMPTS_PER_TARGET:
                        _warn(
                            f"Kết quả không rõ — sẽ thử lại "
                            f"{target['challenge']}: {message}"
                        )
                    else:
                        pending.remove(target)
                        failed.append(target)
                        _warn(
                            f"Kết quả không rõ (không retry) — "
                            f"{target['challenge']}: {message}"
                        )

        except KeyboardInterrupt:
            summary["aborted"] = True
            _warn("Ctrl-C — dừng sniper theo yêu cầu.")

        summary["solved"] = solved
        summary["failed"] = failed
        summary["pending"] = pending
        if pending:
            self._print_remaining(pending)
        console.print()
        console.print(
            f"[bold {FG_BASE}]Sniper kết thúc:[/] "
            f"[{SOLVED}]✔ {len(solved)} first-blood[/{SOLVED}] · "
            f"[{ERROR}]✗ {len(failed)} thất bại[/{ERROR}] · "
            f"[{FG_MUTED}]· {len(pending)} còn lại[/{FG_MUTED}]"
        )
        return summary


# Alias tiện import từ facade cũ nếu sau này wire CLI qua submitter
__all__ = ["SniperService", "SNIPER_FILENAME"]
