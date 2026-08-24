"""WebDashboard — local read-only dashboard (P2-5, ``ctf serve``).

Chuẩn stdlib ``http.server`` — KHÔNG thêm dependency, KHÔNG framework/CDN.

Nguyên tắc:
- CHỈ ĐỌC: không endpoint ghi nào; POST/PUT/... → 405.
- Bind mặc định 127.0.0.1 (không expose LAN); caller phải chủ động truyền
  host khác nếu muốn — và đây là lựa chọn của user, không phải mặc định.
- Mọi dữ liệu platform (tên challenge, notes, labels...) đi qua
  ``html.escape`` trước khi nhúng vào HTML — chống XSS từ dữ liệu remote.

Routes:
- ``GET /``               → HTML đơn trang: header giải + progress bar + bảng
                            challenge với badge 4 trục (STATUS_ICONS), filter
                            querystring ?cat=&label=&q=, auto-refresh 30s.
- ``GET /api/status.json``→ JSON stats + list (cho future use).
- khác                    → 404. Method khác GET → 405.
"""
import html
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, List, Optional

from ..storage.constants import CATEGORY_ICONS, STATUS_ICONS
from .status_service import StatusService


class WebDashboard:
    """Dashboard HTTP read-only render trực tiếp từ StatusService."""

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 8689
    REFRESH_SECONDS = 30

    def __init__(self, repo):
        self.repo = repo

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
<th class="num">Points</th><th class="num">Solves</th><th>Note</th></tr></thead>
<tbody>
{''.join(rows) or '<tr><td colspan="7" class="meta">Không có challenge nào khớp filter.</td></tr>'}
</tbody>
</table>
<footer>read-only · localhost only · generated by WebDashboard</footer>
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
        print(f"🌐 Dashboard: http://{host}:{port}/ (Ctrl+C để dừng — read-only)")
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
    """Handler read-only: GET / , GET /api/status.json ; method khác → 405."""

    dashboard: WebDashboard = None  # type: ignore[assignment]
    server_version = "CTFWebDashboard/1.0"

    def log_message(self, fmt, *args):  # noqa: N802 — im lặng, không spam stderr
        pass

    # Method guard dùng chung cho mọi verb không phải GET.
    def _reject_method(self):
        self.send_response(405)
        self.send_header("Allow", "GET, HEAD")
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        body = "405 Method Not Allowed — dashboard chỉ đọc (read-only).\n".encode("utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_POST = _reject_method
    do_PUT = _reject_method
    do_DELETE = _reject_method
    do_PATCH = _reject_method

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
