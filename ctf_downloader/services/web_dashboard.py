"""WebDashboard — local dashboard (P2-5 ``ctf serve``; v2 thêm POST submit).

Chuẩn stdlib ``http.server`` — KHÔNG thêm dependency, KHÔNG framework/CDN.

Nguyên tắc:
- v1 READ-ONLY giữ nguyên: mọi route GET không đổi.
- v2: DUY NHẤT ``POST /api/submit`` được phép ghi — submit flag QUA
  ``SubmitService.submit()`` nên hưởng trọn gate format + blacklist +
  throttle sẵn có của CLI. Các method/path POST khác vẫn → 405.
- Rate-limit tự vệ: tối đa 1 request submit / 5 giây per dashboard session
  (đếm bằng ``time.monotonic``); quá hạn mức → 429 + ``Retry-After``.
- Bind mặc định 127.0.0.1 (không expose LAN); caller phải chủ động truyền
  host khác nếu muốn — và đây là lựa chọn của user, không phải mặc định.
- Mọi dữ liệu platform (tên challenge, notes, labels, flag hoarded...)
  đi qua ``html.escape`` trước khi nhúng vào HTML — chống XSS từ dữ liệu
  remote. Toast kết quả submit render bằng ``textContent``, không innerHTML.
- CSRF-lite: ``POST /api/submit`` bắt buộc header ``X-Requested-With:
  XMLHttpRequest`` — form cross-origin đơn giản không tự đặt được header
  tuỳ chỉnh (chỉ fetch/XHR same-origin của dashboard mới gửi đúng).

Routes:
- ``GET /``               → HTML đơn trang: header giải + progress bar + bảng
                            challenge với badge 4 trục (STATUS_ICONS), filter
                            querystring ?cat=&label=&q=, auto-refresh 30s;
                            hàng OPEN/working có ô input flag (prefill khi
                            hoarded) + nút Submit (fetch inline, toast text).
- ``GET /api/status.json``→ JSON stats + list (cho future use).
- ``POST /api/submit``    → JSON {challenge, flag} → SubmitService.submit()
                            → JSON {ok, message}. Lỗi HTTP: 400 body thiếu,
                            403 thiếu CSRF-lite header, 429 rate-limit,
                            500 lỗi service, 503 chưa khởi tạo được submitter.
- khác                    → 404. Method khác GET/HEAD/POST(/api/submit) → 405.
"""
import html
import json
import math
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional, Tuple

from ..storage.constants import CATEGORY_ICONS, SOLVE_RANK, STATUS_ICONS
from .status_service import StatusService


class WebDashboard:
    """Dashboard HTTP render trực tiếp từ StatusService + POST submit qua gate."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8689
    REFRESH_SECONDS = 30

    # v2 — POST /api/submit
    SUBMIT_PATH = "/api/submit"
    SUBMIT_COOLDOWN = 5.0  # giây giữa 2 lần submit / dashboard session
    CSRF_HEADER = "X-Requested-With"
    CSRF_VALUE = "XMLHttpRequest"

    def __init__(self, repo, submit_factory=None):
        self.repo = repo
        # Factory tạo SubmitService — lazy (mạng chỉ chạm khi user thật sự
        # submit), inject được từ ngoài để test. None → dùng factory mặc định.
        self._submit_factory = submit_factory
        self._submitter = None
        self._submitter_lock = threading.Lock()
        self._rate_lock = threading.Lock()
        self._last_submit_monotonic: Optional[float] = None

    # ------------------------------------------------------------------ #
    # Data collection (đọc thuần qua StatusService)
    # ------------------------------------------------------------------ #
    def collect(self) -> Dict[str, Any]:
        """Scan workspace một lần, trả data dùng chung cho HTML và JSON."""
        challenges = StatusService.scan_local_challenges(self.repo)
        stats = StatusService.summary_stats(self.repo, challenges)
        window = StatusService._render_window(self.repo)
        return {"stats": stats, "challenges": challenges, "window": window}

    @staticmethod
    def apply_filters(data: Dict[str, Any], cat: str = "",
                      label: str = "", q: str = "") -> List[Dict[str, Any]]:
        """Lọc danh sách challenge theo querystring (?cat=&label=&q=).

        - ``cat``: khớp category, case-insensitive.
        - ``label``: comma-separated, AND — challenge phải mang TẤT CẢ label.
        - ``q``: substring trên name + notes.
        """
        challs = data.get("challenges") or []
        out = challs
        if cat:
            c_low = cat.strip().lower()
            out = [c for c in out
                   if str(c.get("category", "")).lower() == c_low]
        if label:
            wanted = [l.strip() for l in label.split(",") if l.strip()]
            if wanted:
                def _has_all(c):
                    have = {str(x) for x in ((c.get("_status") or {}).get("labels") or [])}
                    return all(w in have for w in wanted)
                out = [c for c in out if _has_all(c)]
        if q:
            q_low = q.strip().lower()
            if q_low:
                def _matches(c):
                    st = c.get("_status") or {}
                    hay = " ".join([
                        str(c.get("name", "")),
                        str(st.get("notes") or ""),
                    ]).lower()
                    return q_low in hay
                out = [c for c in out if _matches(c)]
        return out

    # ------------------------------------------------------------------ #
    # POST /api/submit — submit QUA SubmitService (gate + rate-limit)
    # ------------------------------------------------------------------ #
    @staticmethod
    def is_submittable(chal: Dict[str, Any]) -> bool:
        """True khi challenge đang OPEN/working (chưa ai solve)."""
        status = chal.get("_status") or {}
        solve = status.get("solve") or "unsolved"
        return SOLVE_RANK.get(solve, 0) < SOLVE_RANK["solved_other"]

    def _default_submit_factory(self):
        """Factory mặc định: SubmitService đầy đủ auth từ global config.

        Gọi lazy (lần POST đầu) — không chạm mạng khi dashboard chỉ được xem.
        """
        from .auth_service import AuthService
        from .submit_service import SubmitService

        ws = str(self.repo.root) if getattr(self.repo, "root", None) else os.getcwd()
        cookie, token = AuthService.resolve(ws)
        return SubmitService(cookie=cookie, token=token, workspace_dir=ws)

    def _get_submitter(self):
        with self._submitter_lock:
            if self._submitter is None:
                factory = self._submit_factory or self._default_submit_factory
                self._submitter = factory()
            return self._submitter

    def handle_submit_request(
        self, raw_body: bytes, headers: Any,
    ) -> Tuple[int, Dict[str, Any], Optional[int]]:
        """Xử lý 1 POST /api/submit (tách khỏi HTTP handler để test trực tiếp).

        Trả về ``(http_status, json_payload, retry_after_seconds|None)``.
        Thứ tự: CSRF-lite → parse body → rate-limit → SubmitService.submit().
        Request lỗi format KHÔNG đốt hạn mức rate-limit.
        """
        # 1) CSRF-lite: form cross-origin đơn giản không đặt được header này.
        got = ""
        try:
            got = str(headers.get(self.CSRF_HEADER) or "").strip()
        except Exception:
            pass
        if got != self.CSRF_VALUE:
            return 403, {"ok": False, "message": (
                f"Thiếu hoặc sai header {self.CSRF_HEADER}: {self.CSRF_VALUE}.")}, None

        # 2) Body JSON {challenge, flag}.
        try:
            payload = json.loads((raw_body or b"").decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("body phải là JSON object")
        except Exception:
            return 400, {"ok": False, "message": (
                "Body không phải JSON object hợp lệ ({challenge, flag}).")}, None
        challenge = payload.get("challenge")
        flag = payload.get("flag")
        if challenge is None or str(challenge).strip() == "" \
                or not str(flag or "").strip():
            return 400, {"ok": False,
                         "message": "Thiếu 'challenge' hoặc 'flag'."}, None

        # 3) Rate-limit tự vệ: 1 request / SUBMIT_COOLDOWN giây per session.
        #    Từ chối KHÔNG đẩy mốc thời gian (không tự gia hạn khoá).
        with self._rate_lock:
            now = time.monotonic()
            if self._last_submit_monotonic is not None:
                elapsed = now - self._last_submit_monotonic
                if elapsed < self.SUBMIT_COOLDOWN:
                    wait = math.ceil(self.SUBMIT_COOLDOWN - elapsed)
                    return 429, {
                        "ok": False,
                        "message": (f"Rate-limit: tối đa 1 submit/"
                                    f"{int(self.SUBMIT_COOLDOWN)}s — "
                                    f"thử lại sau ~{wait}s."),
                        "retry_after": wait,
                    }, wait
            self._last_submit_monotonic = now

        # 4) Submit qua gate sẵn có của SubmitService.
        try:
            submitter = self._get_submitter()
        except Exception as exc:
            return 503, {"ok": False, "message":
                         f"Không khởi tạo được SubmitService: {exc}"}, None
        try:
            ok, message = submitter.submit(challenge, str(flag))
            return 200, {"ok": bool(ok), "message": str(message)}, None
        except Exception as exc:
            return 500, {"ok": False, "message": f"Lỗi submit: {exc}"}, None

    # ------------------------------------------------------------------ #
    # JSON API
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_status(status: Optional[dict]) -> dict:
        st = status or {}
        flag = st.get("flag") or {}
        return {
            "solve": st.get("solve", "unsolved"),
            "flag_state": flag.get("state", "none"),
            "writeup": st.get("writeup", "none"),
            "container": st.get("container"),
            "labels": [str(x) for x in (st.get("labels") or [])],
            "notes": str(st.get("notes") or ""),
        }

    def status_json(self) -> bytes:
        """Payload JSON cho ``/api/status.json`` — chỉ kiểu serializable."""
        data = self.collect()
        stats = data["stats"]

        categories = {
            cat: {k: v for k, v in info.items() if k != "challenges"}
            for cat, info in (stats.get("categories") or {}).items()
        }
        challs = []
        for c in data["challenges"]:
            challs.append({
                "id": c.get("id"),
                "name": str(c.get("name", "")),
                "category": str(c.get("category", "")),
                "points": c.get("points"),
                "solves_count": c.get("solves_count", c.get("solves")),
                "folder": str(c.get("_rel_folder", "")),
                "local_files_count": c.get("_local_files_count", 0),
                "status": self._clean_status(c.get("_status")),
            })

        payload = {
            "title": stats.get("title"),
            "platform": stats.get("platform"),
            "url": stats.get("url"),
            "user": stats.get("user"),
            "team": stats.get("team"),
            "event_window_text": data.get("window") or "",
            "total_challenges": stats.get("total_challenges", 0),
            "solved_challenges": stats.get("solved_challenges", 0),
            "total_points": stats.get("total_points", 0),
            "earned_points": stats.get("earned_points", 0),
            "completion_rate": round(stats.get("completion_rate") or 0.0, 2),
            "categories": categories,
            "challenges": challs,
        }
        body = json.dumps(payload, ensure_ascii=False, default=str)
        return (body + "\n").encode("utf-8")

    # ------------------------------------------------------------------ #
    # HTML rendering
    # ------------------------------------------------------------------ #
    @staticmethod
    def _esc(value: Any) -> str:
        return html.escape(str(value if value is not None else ""), quote=True)

    def _badge_html(self, c: dict) -> str:
        """Badge 4 trục — icon LẤY TỪ STATUS_ICONS, không icon inline rải rác."""
        status = c.get("_status") or {}
        solve = status.get("solve", "unsolved")
        flag_state = (status.get("flag") or {}).get("state", "none")
        writeup = status.get("writeup", "none")
        container = StatusService._effective_container(self.repo, c, status)

        parts = [
            f'<span class="badge" title="solve: {self._esc(solve)}">'
            f'{self._esc(STATUS_ICONS["solve"].get(solve, STATUS_ICONS["solve"]["unsolved"]))}</span>',
            f'<span class="badge" title="flag: {self._esc(flag_state)}">'
            f'{self._esc(STATUS_ICONS["flag"].get(flag_state, STATUS_ICONS["flag"]["none"]))}</span>',
            f'<span class="badge" title="writeup: {self._esc(writeup)}">'
            f'{self._esc(STATUS_ICONS["writeup"].get(writeup, STATUS_ICONS["writeup"]["none"]))}</span>',
        ]
        if container:
            parts.append(
                f'<span class="badge" title="container: {self._esc(container)}">'
                f'{self._esc(STATUS_ICONS["container"].get(container, ""))}</span>')
        return "".join(parts)

    def render_page(self, data: Dict[str, Any], cat: str = "",
                    label: str = "", q: str = "") -> bytes:
        stats = data["stats"]
        esc = self._esc
        rate = float(stats.get("completion_rate") or 0.0)
        window = str(data.get("window") or "")

        rows: List[str] = []
        for c in self.apply_filters(data, cat, label, q):
            status = c.get("_status") or {}
            cat_name = str(c.get("category", "Misc"))
            cat_icon = CATEGORY_ICONS.get(cat_name.lower(), "📁")
            labels = [str(x) for x in (status.get("labels") or [])]
            notes = str(status.get("notes") or "").strip()
            solved = StatusService._is_solved(c)
            if self.is_submittable(c):
                # Ô submit: input flag (prefill khi hoarded) + nút Submit.
                flag_info = status.get("flag") or {}
                prefill = str(flag_info.get("value") or "") \
                    if flag_info.get("state") == "hoarded" else ""
                cid_attr = esc(c.get("id", "?"))
                submit_cell = (
                    '<td class="submit">'
                    f'<input type="text" class="flag-input" '
                    f'placeholder="flag…" value="{esc(prefill)}" '
                    f'aria-label="flag {cid_attr}">'
                    f'<button type="button" class="submit-btn" '
                    f'data-submit-challenge="{cid_attr}">Submit</button>'
                    '</td>')
            else:
                submit_cell = '<td class="submit"></td>'
            rows.append(
                f"<tr class=\"{'solved' if solved else 'unsolved'}\">"
                f"<td>{self._badge_html(c)}</td>"
                f"<td>{esc(c.get('id', '?'))}</td>"
                f"<td class=\"name\">{esc(c.get('name', 'Unknown'))}"
                + (f" <span class='labels'>🏷 {esc(','.join(labels))}</span>" if labels else "")
                + "</td>"
                f"<td>{esc(cat_icon)} {esc(cat_name)}</td>"
                f"<td class=\"num\">{esc(c.get('points') or 0)}</td>"
                f"<td class=\"num\">{esc(c.get('solves_count', c.get('solves', '-')))}</td>"
                f"<td>{esc(notes)}</td>"
                f"{submit_cell}"
                "</tr>")

        user_team = " / ".join(x for x in (stats.get("user"), stats.get("team")) if x)
        filter_cat = esc(cat)
        filter_label = esc(label)
        filter_q = esc(q)

        page = f"""<!DOCTYPE html>
<html lang="vi">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="refresh" content="{self.REFRESH_SECONDS}">
<title>{esc(stats.get('title'))} — CTF Dashboard</title>
<style>
:root {{ color-scheme: light dark;
  --bg: #f6f6f8; --fg: #1c1c22; --card: #ffffff; --line: #d8d8e0;
  --muted: #66666f; --accent: #4c7dff; --bar-bg: #e3e3ea; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #14141a; --fg: #e8e8ee; --card: #1e1e26; --line: #33333e;
    --muted: #9a9aa6; --accent: #7aa2ff; --bar-bg: #2a2a34; }}
}}
* {{ box-sizing: border-box; }}
body {{ margin: 0; padding: 1.2rem; background: var(--bg); color: var(--fg);
  font: 15px/1.5 system-ui, sans-serif; }}
.card {{ background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; padding: 1rem 1.2rem; max-width: 1100px; margin: 0 auto 1rem; }}
h1 {{ font-size: 1.35rem; margin: 0 0 .25rem; }}
.meta {{ color: var(--muted); font-size: .9rem; }}
.bar {{ height: 12px; background: var(--bar-bg); border-radius: 999px;
  overflow: hidden; margin: .6rem 0 .2rem; }}
.fill {{ height: 100%; background: var(--accent); border-radius: 999px;
  transition: width .4s ease; }}
form.filters {{ display: flex; gap: .5rem; flex-wrap: wrap; align-items: center; }}
form.filters input {{ background: var(--bg); color: var(--fg);
  border: 1px solid var(--line); border-radius: 6px; padding: .35rem .6rem; }}
form.filters button {{ background: var(--accent); color: #fff; border: 0;
  border-radius: 6px; padding: .38rem .9rem; cursor: pointer; }}
table {{ border-collapse: collapse; width: 100%; max-width: 1100px;
  margin: 0 auto; font-size: .92rem; }}
th, td {{ text-align: left; padding: .45rem .6rem; border-bottom: 1px solid var(--line); }}
th {{ color: var(--muted); font-weight: 600; white-space: nowrap; }}
td.num {{ text-align: right; white-space: nowrap; }}
tr.solved td.name {{ color: var(--muted); text-decoration: line-through; }}
.badge {{ display: inline-block; min-width: 1.6em; text-align: center; }}
.labels {{ color: var(--muted); font-size: .82em; }}
td.submit {{ white-space: nowrap; }}
input.flag-input {{ background: var(--bg); color: var(--fg);
  border: 1px solid var(--line); border-radius: 6px;
  padding: .25rem .45rem; width: 11rem; max-width: 28vw; }}
button.submit-btn {{ background: var(--accent); color: #fff; border: 0;
  border-radius: 6px; padding: .3rem .6rem; cursor: pointer;
  margin-left: .35rem; }}
button.submit-btn:disabled {{ opacity: .5; cursor: default; }}
#toast {{ position: fixed; bottom: 1.2rem; left: 50%;
  transform: translateX(-50%); background: var(--card); color: var(--fg);
  border: 1px solid var(--line); border-radius: 8px;
  padding: .5rem .9rem; font-size: .9rem; max-width: 90vw;
  box-shadow: 0 4px 14px rgba(0,0,0,.25); opacity: 0;
  pointer-events: none; transition: opacity .25s ease; }}
#toast.show {{ opacity: 1; }}
footer {{ color: var(--muted); text-align: center; font-size: .8rem; margin-top: 1rem; }}
</style>
</head>
<body>
<div class="card">
  <h1>🏆 {esc(stats.get('title'))}
      <span class="meta">[{esc(str(stats.get('platform') or '').upper())}]</span></h1>
  <div class="meta">👤 {esc(user_team) if user_team else '<i>anonymous</i>'}
   &nbsp;|&nbsp; 📊 {int(stats.get('solved_challenges') or 0)}/{int(stats.get('total_challenges') or 0)} solved
   &nbsp;|&nbsp; 💰 {esc(stats.get('earned_points') or 0)}/{esc(stats.get('total_points') or 0)} pts</div>
  {'<div class="meta">⏱️ ' + esc(window) + '</div>' if window else ''}
  <div class="bar"><div class="fill" style="width:{rate:.1f}%"></div></div>
  <div class="meta">{rate:.1f}% — auto-refresh mỗi {self.REFRESH_SECONDS}s</div>
</div>
<div class="card">
  <form class="filters" method="get" action="/">
    <input type="text" name="cat" placeholder="category (vd: web)" value="{filter_cat}">
    <input type="text" name="label" placeholder="labels (a,b — AND)" value="{filter_label}">
    <input type="text" name="q" placeholder="tìm tên / note…" value="{filter_q}">
    <button type="submit">Lọc</button>
    <a href="/" style="margin-left:auto;color:var(--muted)">reset</a>
  </form>
</div>
<table>
<thead><tr><th>Status</th><th>#</th><th>Challenge</th><th>Category</th>
<th class="num">Points</th><th class="num">Solves</th><th>Note</th>
<th>Flag</th></tr></thead>
<tbody>
{''.join(rows) or '<tr><td colspan="8" class="meta">Không có challenge nào khớp filter.</td></tr>'}
</tbody>
</table>
<div id="toast" role="status" aria-live="polite"></div>
<script>
(function () {{
  'use strict';
  var toast = document.getElementById('toast');
  var toastTimer = null;
  function show(msg) {{
    toast.textContent = msg;            // textContent — không innerHTML (XSS)
    toast.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(function () {{ toast.classList.remove('show'); }}, 5000);
  }}
  Array.prototype.forEach.call(
    document.querySelectorAll('button[data-submit-challenge]'),
    function (btn) {{
      btn.addEventListener('click', function () {{
        var input = btn.parentElement.querySelector('input.flag-input');
        var flag = input ? input.value.trim() : '';
        if (!flag) {{ show('⚠ Nhập flag trước khi submit.'); return; }}
        btn.disabled = true;
        fetch('{self.SUBMIT_PATH}', {{
          method: 'POST',
          headers: {{
            'Content-Type': 'application/json',
            '{self.CSRF_HEADER}': '{self.CSRF_VALUE}'
          }},
          body: JSON.stringify({{
            challenge: btn.getAttribute('data-submit-challenge'),
            flag: flag
          }})
        }}).then(function (resp) {{
          return resp.json().catch(function () {{ return {{}}; }})
            .then(function (data) {{ return {{ code: resp.status, data: data }}; }});
        }}).then(function (res) {{
          show((res.data && res.data.message)
            ? res.data.message
            : ('HTTP ' + res.code));
          if (res.code !== 429) btn.disabled = false;
        }}).catch(function (err) {{
          show('Lỗi submit: ' + err);
          btn.disabled = false;
        }});
      }});
    }});
}})();
</script>
<footer>read-only + POST /api/submit qua gate · localhost only · generated by WebDashboard</footer>
</body>
</html>
"""
        return page.encode("utf-8")


    # ------------------------------------------------------------------ #
    # Entry point
    # ------------------------------------------------------------------ #
    def make_server(self, host: str, port: int) -> ThreadingHTTPServer:
        handler = type("BoundDashboardHandler", (_DashboardHandler,),
                       {"dashboard": self})
        return ThreadingHTTPServer((host, port), handler)

    def serve(self, host: Optional[str] = None, port: Optional[int] = None) -> None:
        """Chạy dashboard (blocking). Mặc định bind 127.0.0.1 — KHÔNG expose LAN.

        Port bận / không bind được → ``OSError`` với message rõ ràng.
        """
        host = host or self.DEFAULT_HOST
        port = self.DEFAULT_PORT if port is None else int(port)
        try:
            httpd = self.make_server(host, port)
        except OSError as exc:
            raise OSError(
                f"Không thể khởi động dashboard tại {host}:{port} — "
                f"port bận hoặc không hợp lệ ({exc})."
            ) from exc
        print(f"🌐 Dashboard: http://{host}:{port}/ "
              f"(Ctrl+C để dừng — read-only + submit qua gate)")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()


# ---------------------------------------------------------------------- #
# HTTP handler (stdlib)
# ---------------------------------------------------------------------- #
class _DashboardHandler(BaseHTTPRequestHandler):
    """Handler: GET / , GET /api/status.json , POST /api/submit.

    Method/path khác → 405 (POST ngoài /api/submit, PUT, DELETE, PATCH).
    """

    dashboard: WebDashboard = None  # type: ignore[assignment]
    server_version = "CTFWebDashboard/2.0"

    def log_message(self, fmt, *args):  # noqa: N802 — im lặng, không spam stderr
        pass

    # Method guard dùng chung cho mọi verb/không-giữa không được phép.
    def _reject_method(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD, POST")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = ("405 Method Not Allowed — chỉ GET và POST /api/submit.\n"
                .encode("utf-8"))
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_PUT = _reject_method
    do_DELETE = _reject_method
    do_PATCH = _reject_method

    def _send_json(self, code: int, payload: Dict[str, Any],
                   retry_after: Optional[int] = None) -> None:
        body = (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        if retry_after is not None:
            self.send_header("Retry-After", str(retry_after))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):  # noqa: N802 — duy nhất POST /api/submit được phép ghi
        from urllib.parse import urlparse

        try:
            if urlparse(self.path).path != self.dashboard.SUBMIT_PATH:
                self._reject_method()
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            raw = self.rfile.read(length) if length > 0 else b""
            code, payload, retry_after = \
                self.dashboard.handle_submit_request(raw, self.headers)
            self._send_json(code, payload, retry_after)
        except BrokenPipeError:
            pass
        except Exception as exc:  # không bao giờ làm server chết vì 1 request
            try:
                self._send_json(500, {"ok": False,
                                      "message": f"Internal error: {exc}"})
            except Exception:
                pass

    def do_GET(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path == "/api/status.json":
                body = self.dashboard.status_json()
                ctype = "application/json; charset=utf-8"
            elif path == "/":
                qs = parse_qs(parsed.query)
                data = self.dashboard.collect()
                body = self.dashboard.render_page(
                    data,
                    cat=(qs.get("cat") or [""])[0],
                    label=(qs.get("label") or [""])[0],
                    q=(qs.get("q") or [""])[0],
                )
                ctype = "text/html; charset=utf-8"
            else:
                self.send_response(404)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                notfound = b"404 Not Found\n"
                self.send_header("Content-Length", str(len(notfound)))
                self.end_headers()
                self.wfile.write(notfound)
                return

            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except BrokenPipeError:
            pass
        except Exception as exc:  # không bao giờ làm server chết vì 1 request
            try:
                msg = f"500 Internal Server Error: {exc}\n".encode("utf-8")
                self.send_response(500)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)
            except Exception:
                pass

    def do_HEAD(self):  # noqa: N802
        self.do_GET()
