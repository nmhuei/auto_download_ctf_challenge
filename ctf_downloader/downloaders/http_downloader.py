import hashlib
import ipaddress
import json
import os
import random
import shutil
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import requests
from typing import Any, Callable, Dict, Optional, Tuple
from urllib.parse import urlparse, urljoin
from weakref import WeakValueDictionary
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename, extract_filename_from_headers, extract_filename_from_url
from .registry import register_downloader

CHUNK_SIZE = 131072  # 128KB default


def _get_optimal_chunk_size(total_size: int = 0) -> int:
    """Chọn kích thước chunk tối ưu theo dung lượng file để giảm I/O syscalls."""
    if total_size >= 20 * 1024 * 1024:
        return 1048576  # 1MB
    if total_size >= 2 * 1024 * 1024:
        return 524288   # 512KB
    return 131072       # 128KB


def _iter_response_content(resp: Any, chunk_size: int):
    """Stream response chunks across requests and curl_cffi backends.

    curl_cffi exposes a requests-like ``iter_content(chunk_size=...)`` but
    explicitly ignores the requested size and warns on every call. Let libcurl
    yield its native streaming chunks there; keep the tuned chunk size for the
    normal requests backend.
    """
    module = type(resp).__module__
    if module.startswith("curl_cffi."):
        return resp.iter_content()
    return resp.iter_content(chunk_size=chunk_size)
# Số lần thử resume (mở lại kết nối + Range) khi mất kết nối giữa chừng
MAX_RESUME_ATTEMPTS = 3
# C19-M5: exponential backoff cho vòng retry — 0.5×2^(n-1)s, cap 8s,
# jitter nhẹ (≤0.25s) chống thundering herd giữa các worker.
_RETRY_BASE_SECONDS = 0.5
_RETRY_CAP_SECONDS = 8.0
_RETRY_JITTER_SECONDS = 0.25
# Các HTTP redirect được theo dõi khi probe thủ công
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_PROBE_REDIRECTS = 10

# C9-04: registry khóa per-target (theo đường dẫn đích tuyệt đối). Hai thread
# của pool tải 2 attachment trùng tên đích trước đây cùng thấy/cùng xoá
# `.part` của nhau (fake-resume -> server trả 200) rồi rename đè nhau ->
# mất dữ liệu im lặng. Khóa giữ TRỌN VÒNG ĐỜI một lần tải (kể cả retry/
# resume tới khi rename xong) nên mọi thao tác .part/rename trên cùng đích
# được tuần tự hoá.
#
# C11-deferred: WeakValueDictionary thay dict thường — entry tự xoá khi
# thread CUỐI bỏ tham chiếu (tải xong, `tlock` local trong download_file bị
# thu hồi). An toàn đa luồng: mỗi thread nắm strong ref cục bộ từ lúc get
# dưới guard đến khi release trong `finally` nên lock sống đủ lâu cho mọi
# người đang giữ/chờ; khi không còn ai giữ, lock mới cho cùng đích tương
# đương lock cũ (không ai còn trong section). Guard vẫn bắt buộc để nguyên
# tử hoá get-or-create (hai thread cùng thấy None sẽ tạo hai lock khác
# nhau nếu không khoá).
_TARGET_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: "WeakValueDictionary" = WeakValueDictionary()


class DownloadFailed(Exception):
    """Download thất bại với lý do rõ ràng (nội dung HTML, link hết hạn, ...)."""


class LargeFileSkipped(DownloadFailed):
    """File vượt quá ngưỡng dung lượng cho phép -> bị skip (không tải body)."""

    def __init__(self, size: int):
        self.size = size
        super().__init__(f"skipped_large_file: vượt quá giới hạn dung lượng ({size} bytes)")


@register_downloader("direct_file")
class HttpDownloader:
    @staticmethod
    def _acquire_target_lock(target_path: str):
        """Lấy khóa độc quyền cho đúng MỘT đường dẫn đích (C9-04).

        Trả về ``(lock, waited)`` — hàm chỉ trả khi ĐÃ giữ được khóa, nên
        caller có thể nhả an toàn trong ``finally``; nếu bị interrupt giữa
        lúc chờ, exception ném ra trước khi gán nên caller không sở hữu khóa.
        """
        key = os.path.abspath(target_path)
        with _TARGET_LOCKS_GUARD:
            lock = _TARGET_LOCKS.get(key)
            if lock is None:
                lock = threading.Lock()
                _TARGET_LOCKS[key] = lock
        if lock.acquire(blocking=False):
            return lock, False
        Logger.info(
            f"Thread khác đang tải trùng đích "
            f"'{os.path.basename(target_path)}' — chờ tuần tự để tránh "
            f"ghi đè .part lẫn nhau."
        )
        lock.acquire()
        return lock, True

    @staticmethod
    def _short_error(exc: Exception, max_len: int = 200) -> str:
        return str(exc).replace("\n", " ")[:max_len]

    @staticmethod
    def _validate_remote_url(url: str) -> None:
        """Reject URL forms that should never reach the HTTP downloader.

        Private/loopback hosts are intentionally NOT rejected here because
        CTF labs commonly expose attachments/services on RFC1918 or localhost.
        The invariant enforced at this layer is narrower and compatibility
        safe: HTTP(S) only, a real hostname, and no embedded userinfo that
        could leak through logs or redirect handling.
        """
        try:
            parsed = urlparse(str(url))
        except Exception as exc:
            raise DownloadFailed(f"URL tải không hợp lệ: {exc}") from exc
        if parsed.scheme.lower() not in ("http", "https"):
            raise DownloadFailed(
                f"Scheme tải không được hỗ trợ: {parsed.scheme or '<missing>'} "
                "(chỉ cho phép http/https)"
            )
        if not parsed.hostname:
            raise DownloadFailed("URL tải thiếu hostname")
        if parsed.username is not None or parsed.password is not None:
            raise DownloadFailed(
                "URL tải chứa username/password nhúng — bị từ chối để tránh lộ credential"
            )

    @staticmethod
    def _origin(url: str) -> Tuple[str, str, int]:
        parsed = urlparse(url)
        scheme = parsed.scheme.lower()
        host = (parsed.hostname or "").lower()
        port = parsed.port or (443 if scheme == "https" else 80)
        return scheme, host, port

    @staticmethod
    def _host_private_status(hostname: str) -> Optional[bool]:
        """Return True/False when host privacy can be determined, else None.

        Initial private hosts remain valid for CTF labs.  This helper exists
        to detect a *redirect transition* from a known-public origin to a
        private/link-local/loopback destination.
        """
        host = str(hostname or "").strip().strip("[]").lower()
        if not host:
            return None
        if host == "localhost" or host.endswith(".localhost"):
            return True

        def _is_private_ip(value: str) -> bool:
            ip = ipaddress.ip_address(value)
            return bool(
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_reserved
                or ip.is_multicast
                or ip.is_unspecified
            )

        try:
            return _is_private_ip(host)
        except ValueError:
            pass

        try:
            infos = socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except OSError:
            return None
        statuses = []
        for info in infos:
            try:
                statuses.append(_is_private_ip(str(info[4][0])))
            except (ValueError, IndexError, TypeError):
                continue
        if not statuses:
            return None
        # Any private answer is treated as private to avoid a public DNS name
        # being used as a trampoline into loopback/RFC1918 metadata services.
        return any(statuses)

    @staticmethod
    def _cross_origin_headers(session: Any, headers: Optional[Dict[str, str]]) -> Dict[str, Any]:
        """Preserve only transport-safe headers on a cross-origin redirect.

        requests normally strips Authorization while following redirects, but
        we follow redirects manually so policy can inspect every hop.  Session
        headers may contain API keys/custom auth, therefore every non-safe
        session header is explicitly nulled on the new origin.  Range and
        conditional validators are preserved because they describe the file,
        not platform credentials.
        """
        safe = {
            "accept", "accept-encoding", "accept-language", "user-agent",
            "range", "if-range", "if-none-match", "if-modified-since",
            "cache-control", "pragma",
        }
        result: Dict[str, Any] = {}
        session_headers = getattr(session, "headers", None) or {}
        try:
            keys = list(session_headers.keys())
        except Exception:
            keys = []
        for key in keys:
            if str(key).lower() not in safe:
                result[str(key)] = None
        for key, value in dict(headers or {}).items():
            result[key] = value if str(key).lower() in safe else None
        return result

    @staticmethod
    def _request_follow_redirects(
        session: Any,
        method: str,
        url: str,
        *,
        timeout: int = 30,
        stream: bool = False,
        headers: Optional[Dict[str, str]] = None,
        allow_private_redirects: bool = False,
    ) -> Tuple[Any, str]:
        """Issue HEAD/GET while validating each redirect before following it.

        Security contract:
        - initial private/loopback URL is allowed (common CTF topology);
        - known-public -> private redirect is blocked unless explicitly opted in;
        - initial URLs outside the session credential origin, and later
          cross-origin hops, do not inherit Authorization/API-key headers;
        - non HTTP(S), userinfo URLs, redirect loops and missing Location fail.
        """
        method_up = str(method).upper()
        if method_up not in ("HEAD", "GET"):
            raise ValueError("redirect helper only supports HEAD/GET")
        HttpDownloader._validate_remote_url(url)
        current = url
        previous: Optional[str] = None
        credential_origin = getattr(session, "_credential_origin", None)
        try:
            credential_origin_key = (
                HttpDownloader._origin(str(credential_origin))
                if credential_origin else None
            )
        except Exception:
            credential_origin_key = None

        for _ in range(_MAX_PROBE_REDIRECTS + 1):
            current_origin = HttpDownloader._origin(current)
            cross_origin = bool(
                previous and HttpDownloader._origin(previous) != current_origin
            )
            outside_credential_origin = bool(
                credential_origin_key is not None
                and current_origin != credential_origin_key
            )
            hop_headers = (
                HttpDownloader._cross_origin_headers(session, headers)
                if cross_origin or outside_credential_origin
                else dict(headers or {})
            )
            kwargs: Dict[str, Any] = {
                "timeout": timeout,
                "allow_redirects": False,
            }
            if hop_headers:
                kwargs["headers"] = hop_headers
            if method_up == "GET":
                kwargs["stream"] = stream
                resp = session.get(current, **kwargs)
            else:
                resp = session.head(current, **kwargs)

            if getattr(resp, "status_code", None) not in _REDIRECT_CODES:
                return resp, current

            location = (getattr(resp, "headers", {}) or {}).get("Location")
            if not location:
                try:
                    resp.close()
                except Exception:
                    pass
                raise DownloadFailed("HTTP redirect thiếu Location")
            next_url = urljoin(current, location)
            HttpDownloader._validate_remote_url(next_url)

            cur_host = (urlparse(current).hostname or "")
            next_host = (urlparse(next_url).hostname or "")
            cur_private = HttpDownloader._host_private_status(cur_host)
            next_private = HttpDownloader._host_private_status(next_host)
            if cur_private is False and next_private is True and not allow_private_redirects:
                try:
                    resp.close()
                except Exception:
                    pass
                raise DownloadFailed(
                    "Redirect public → private/loopback bị chặn; "
                    "dùng --allow-private-redirects nếu đây là topology CTF chủ đích"
                )
            try:
                resp.close()
            except Exception:
                pass
            previous, current = current, next_url

        raise DownloadFailed(f"Quá nhiều HTTP redirect (>{_MAX_PROBE_REDIRECTS})")

    @staticmethod
    def _final_meta_path(target_path: str) -> str:
        return target_path + ".ctfmeta.json"

    @staticmethod
    def _sha256_file(path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def load_final_metadata(target_path: Optional[str]) -> Dict[str, Any]:
        if not target_path:
            return {}
        try:
            with open(HttpDownloader._final_meta_path(target_path), "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _write_final_metadata(target_path: str, data: Dict[str, Any]) -> Dict[str, Any]:
        payload = dict(data or {})
        payload["schema_version"] = 1
        payload["size"] = os.path.getsize(target_path)
        payload["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        meta_path = HttpDownloader._final_meta_path(target_path)
        tmp_path = meta_path + ".tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False, sort_keys=True)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, meta_path)
        except BaseException:
            try:
                os.remove(tmp_path)
            except OSError:
                pass
            raise
        return payload

    @staticmethod
    def _record_final_metadata(
        target_path: str,
        source_url: str,
        *,
        final_url: Optional[str] = None,
        headers: Optional[Dict[str, Any]] = None,
        verify_mode: str = "fast",
        previous: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        h = headers or {}
        data = dict(previous or {})
        data.update({
            "url": source_url,
            "final_url": final_url or source_url,
            "etag": (h.get("ETag") or h.get("etag") or data.get("etag") or None),
            "last_modified": (
                h.get("Last-Modified") or h.get("last-modified")
                or data.get("last_modified") or None
            ),
        })
        if str(verify_mode).lower() == "strict":
            data["sha256"] = HttpDownloader._sha256_file(target_path)
        else:
            data.setdefault("sha256", None)
        return HttpDownloader._write_final_metadata(target_path, data)

    @staticmethod
    def verify_existing_file(
        target_path: str,
        source_url: str,
        session: Any,
        *,
        mode: str = "fast",
        timeout: int = 30,
        allow_private_redirects: bool = False,
        remote_response: Any = None,
        final_url: Optional[str] = None,
    ) -> bool:
        """Validate an existing final file according to fast/normal/strict.

        fast   -> presence only (legacy behavior)
        normal -> compare remote ETag/Last-Modified/Content-Length when
                  available; at least one remote property must be comparable
        strict -> normal + local SHA-256 must match the persisted baseline
        """
        mode = str(mode or "fast").strip().lower()
        if mode not in ("fast", "normal", "strict"):
            raise ValueError(f"verify mode không hợp lệ: {mode}")
        if not os.path.isfile(target_path):
            return False
        if mode == "fast":
            return True

        meta = HttpDownloader.load_final_metadata(target_path)
        if meta.get("url") and str(meta.get("url")) != str(source_url):
            return False
        local_size = os.path.getsize(target_path)
        if meta.get("size") is not None:
            try:
                if int(meta["size"]) != local_size:
                    return False
            except (TypeError, ValueError):
                return False

        resp = remote_response
        resolved_url = final_url or source_url
        owned_response = resp is None
        if resp is None:
            cond_headers: Dict[str, str] = {}
            if meta.get("etag"):
                cond_headers["If-None-Match"] = str(meta["etag"])
            elif meta.get("last_modified"):
                cond_headers["If-Modified-Since"] = str(meta["last_modified"])
            try:
                resp, resolved_url = HttpDownloader._request_follow_redirects(
                    session,
                    "HEAD",
                    source_url,
                    timeout=timeout,
                    headers=cond_headers,
                    allow_private_redirects=allow_private_redirects,
                )
            except (requests.RequestException, DownloadFailed):
                return False

        try:
            status = int(getattr(resp, "status_code", 0) or 0)
            headers = getattr(resp, "headers", {}) or {}
            comparable = False
            if status == 304:
                comparable = bool(meta.get("etag") or meta.get("last_modified"))
            elif 200 <= status < 300:
                remote_etag = headers.get("ETag") or headers.get("etag")
                remote_lm = headers.get("Last-Modified") or headers.get("last-modified")
                raw_cl = headers.get("Content-Length") or headers.get("content-length")
                if meta.get("etag") and remote_etag:
                    comparable = True
                    if str(meta["etag"]) != str(remote_etag):
                        return False
                elif meta.get("last_modified") and remote_lm:
                    comparable = True
                    if str(meta["last_modified"]) != str(remote_lm):
                        return False
                if raw_cl is not None and str(raw_cl).strip().isdigit():
                    comparable = True
                    if int(raw_cl) != local_size:
                        return False
            else:
                return False

            if not comparable:
                return False

            if mode == "strict":
                expected_hash = str(meta.get("sha256") or "").strip().lower()
                if not expected_hash:
                    return False
                if HttpDownloader._sha256_file(target_path).lower() != expected_hash:
                    return False

            # Refresh validators after a successful validation. Preserve a
            # strict hash baseline; normal mode never invents a hash.
            HttpDownloader._record_final_metadata(
                target_path,
                source_url,
                final_url=resolved_url,
                headers=headers,
                verify_mode="fast",
                previous=meta,
            )
            return True
        finally:
            if owned_response:
                try:
                    resp.close()
                except Exception:
                    pass

    @staticmethod
    def _resume_meta_path(part_path: str) -> str:
        return part_path + ".meta.json"

    @staticmethod
    def _load_resume_metadata(part_path: Optional[str]) -> Dict[str, Any]:
        if not part_path:
            return {}
        meta_path = HttpDownloader._resume_meta_path(part_path)
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                meta = json.load(f)
            return meta if isinstance(meta, dict) else {}
        except (OSError, ValueError, TypeError):
            return {}

    @staticmethod
    def _load_if_range(part_path: Optional[str]) -> Optional[str]:
        """Load the validator associated with a partial download.

        ETag is preferred; Last-Modified is a valid If-Range validator too.
        Corrupt/stale sidecars are ignored rather than making resume fatal.
        """
        meta = HttpDownloader._load_resume_metadata(part_path)
        return meta.get("etag") or meta.get("last_modified") or None

    @staticmethod
    def _save_resume_validator(part_path: Optional[str], resp: requests.Response) -> None:
        """Persist ETag/Last-Modified beside ``.part`` for safe future resume."""
        if not part_path:
            return
        etag = (resp.headers.get("ETag") or resp.headers.get("etag") or "").strip()
        last_modified = (
            resp.headers.get("Last-Modified")
            or resp.headers.get("last-modified")
            or ""
        ).strip()
        if not etag and not last_modified:
            return
        meta_path = HttpDownloader._resume_meta_path(part_path)
        tmp_path = meta_path + ".tmp"
        payload = {"etag": etag or None, "last_modified": last_modified or None}
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, sort_keys=True)
                f.flush()
            os.replace(tmp_path, meta_path)
        except OSError:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    @staticmethod
    def _cleanup_resume_validator(part_path: Optional[str]) -> None:
        if not part_path:
            return
        for path in (
            HttpDownloader._resume_meta_path(part_path),
            HttpDownloader._resume_meta_path(part_path) + ".tmp",
        ):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except OSError:
                pass

    @staticmethod
    def _content_range_start(value: str) -> Optional[int]:
        """Return the first byte from ``Content-Range: bytes N-M/T``."""
        try:
            unit, rest = str(value).strip().split(None, 1)
            if unit.lower() != "bytes":
                return None
            bounds = rest.split("/", 1)[0]
            start = bounds.split("-", 1)[0]
            return int(start)
        except (ValueError, TypeError):
            return None

    @staticmethod
    def _retry_backoff(attempt: int) -> float:
        """C19-M5: exponential backoff cho vòng retry resume — trước đây
        ``continue`` NGAY sau khi tăng attempt nên các lần thử dồn cục bắn
        liên hoàn vào server đang quá tải. Delay 0.5×2^(n-1)s cap 8s +
        jitter nhẹ. Ngủ rồi trả về số giây đã ngủ (caller chỉ gọi khi còn
        được phép thử lại)."""
        delay = min(_RETRY_BASE_SECONDS * (2 ** max(0, attempt - 1)),
                    _RETRY_CAP_SECONDS)
        delay += random.uniform(0, _RETRY_JITTER_SECONDS)
        time.sleep(delay)
        return delay

    @staticmethod
    def probe_content_length(
        url: str,
        session: requests.Session,
        timeout: int = 30,
        allow_private_redirects: bool = False,
    ) -> Optional[int]:
        """HEAD pre-flight for Content-Length with validated redirects."""
        try:
            resp, _final_url = HttpDownloader._request_follow_redirects(
                session,
                "HEAD",
                url,
                timeout=timeout,
                allow_private_redirects=allow_private_redirects,
            )
        except (requests.RequestException, DownloadFailed) as e:
            Logger.warning(
                f"Không xác định trước dung lượng của {url}: "
                f"{type(e).__name__}: {HttpDownloader._short_error(e)}"
            )
            return None
        try:
            status = int(getattr(resp, "status_code", 0) or 0)
            if status == 304 or status >= 400:
                return None
            headers = getattr(resp, "headers", {}) or {}
            content_length = headers.get("Content-Length") or headers.get("content-length")
            if content_length is not None and str(content_length).strip().isdigit():
                return int(content_length)
            return None
        finally:
            try:
                resp.close()
            except Exception:
                pass

    @staticmethod
    def _download_parallel_segments(
        url: str,
        target_path: str,
        part_path: str,
        total_size: int,
        session: requests.Session,
        timeout: int = 30,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_size: int = 0,
        allow_private_redirects: bool = False,
    ) -> bool:
        """
        Tăng tốc tải file lớn (>8MB) bằng cách chia nhỏ thành các dải (Range segments)
        và tải đồng thời qua ThreadPoolExecutor (tương tự axel / aria2).
        """
        num_segments = min(8, max(2, total_size // (4 * 1024 * 1024)))
        seg_size = total_size // num_segments
        seg_files = [f"{part_path}.seg{i}" for i in range(num_segments)]
        ranges = []
        for i in range(num_segments):
            start = i * seg_size
            end = (start + seg_size - 1) if i < num_segments - 1 else total_size - 1
            ranges.append((start, end, seg_files[i]))

        cb_lock = threading.Lock()
        def safe_cb(chunk_len):
            if progress_callback:
                with cb_lock:
                    progress_callback(chunk_len, total_size)

        def download_segment(start_byte: int, end_byte: int, out_file: str) -> bool:
            headers = {"Range": f"bytes={start_byte}-{end_byte}"}
            try:
                resp, _final_url = HttpDownloader._request_follow_redirects(
                    session,
                    "GET",
                    url,
                    timeout=timeout,
                    stream=True,
                    headers=headers,
                    allow_private_redirects=allow_private_redirects,
                )
                with closing(resp):
                    if resp.status_code != 206:
                        return False
                    # 206 không đủ để tin rằng body thuộc đúng segment. Proxy
                    # hoặc origin lỗi có thể trả range khác; ghép các segment
                    # như vậy tạo file đúng SIZE nhưng sai NỘI DUNG.
                    content_range = resp.headers.get("Content-Range") or ""
                    try:
                        unit, rest = content_range.strip().split(None, 1)
                        bounds, total = rest.split("/", 1)
                        got_start, got_end = bounds.split("-", 1)
                        range_ok = (
                            unit.lower() == "bytes"
                            and int(got_start) == start_byte
                            and int(got_end) == end_byte
                            and (total == "*" or int(total) == total_size)
                        )
                    except (ValueError, TypeError):
                        range_ok = False
                    if not range_ok:
                        return False
                    chunk_sz = _get_optimal_chunk_size(end_byte - start_byte + 1)
                    downloaded = 0
                    with open(out_file, "wb") as f:
                        for chunk in _iter_response_content(resp, chunk_sz):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded += len(chunk)
                            safe_cb(len(chunk))
                    expected = end_byte - start_byte + 1
                    if os.path.exists(out_file) and os.path.getsize(out_file) == expected:
                        return True
                    return False
            except Exception:
                return False

        with ThreadPoolExecutor(max_workers=num_segments) as pool:
            futures = [pool.submit(download_segment, start, end, fpath) for start, end, fpath in ranges]
            results = [f.result() for f in futures]

        if all(results):
            try:
                with open(part_path, "wb") as outfile:
                    for seg_file in seg_files:
                        with open(seg_file, "rb") as sf:
                            shutil.copyfileobj(sf, outfile, length=1048576)
                        try:
                            os.remove(seg_file)
                        except OSError:
                            pass
                if os.path.exists(part_path) and os.path.getsize(part_path) == total_size:
                    return True
            except Exception:
                pass

        for seg_file in seg_files:
            if os.path.exists(seg_file):
                try:
                    os.remove(seg_file)
                except OSError:
                    pass
        return False

    @staticmethod
    def download_file(
        url: str,
        dest_dir: str,
        session: requests.Session,
        preferred_filename: Optional[str] = None,
        timeout: int = 30,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_size: int = 0,
        verify_mode: str = "fast",
        allow_private_redirects: bool = False,
    ) -> Optional[str]:
        """
        Downloads a file from URL to dest_dir.
        Returns the absolute path of the saved file or None on error.

        - Resume cơ bản: dữ liệu ghi vào `<name>.part`, khi retry gửi
          `Range: bytes=N-` và xử lý 206 (append). Server trả 200 (không hỗ trợ
          range) thì tải lại từ đầu.
        - Interstitial guard: response nhị phân kỳ vọng mà Content-Type là
          text/html -> KHÔNG lưu file, báo lỗi rõ ràng.
        - max_size > 0: gate unknown-size — đọc Content-Length ở response cuối
          NGAY TRƯỚC KHI ghi; nếu vượt ngưỡng thì raise LargeFileSkipped mà
          KHÔNG tải tiếp body. Vượt ngưỡng giữa chừng (chunked) cũng ngắt sớm.
        Raises DownloadFailed / LargeFileSkipped với lý do cụ thể.
        """
        target_path: Optional[str] = None
        part_path: Optional[str] = None
        attempt = 0
        tlock = None           # C9-04: khóa per-target (nếu đích đã xác định)
        tlock_waited = False   # True nếu có thread khác đang giữ khi ta đến
        parallel_disabled = False  # một lần segment fail -> fallback tuần tự chuẩn
        existing_needs_refresh = False
        verify_mode = str(verify_mode or "fast").strip().lower()
        if verify_mode not in ("fast", "normal", "strict"):
            raise ValueError(f"verify mode không hợp lệ: {verify_mode}")

        try:
            HttpDownloader._validate_remote_url(url)
            os.makedirs(dest_dir, exist_ok=True)

            # Caller đưa sẵn tên file -> xác định target/.part NGAY TRƯỚC vòng
            # lặp: cho phép nhận diện .part sót từ các lần chạy TRƯỚC để resume
            # (và reset khi 416) ngay từ request đầu tiên.
            if preferred_filename:
                target_path = os.path.join(dest_dir, sanitize_filename(preferred_filename))
                part_path = target_path + ".part"
                tlock, tlock_waited = HttpDownloader._acquire_target_lock(target_path)
                if (tlock_waited and not force and os.path.exists(target_path)):
                    # Worker trước vừa hoàn tất. Với fast, presence là đủ;
                    # normal/strict vẫn revalidate sau khi lock được grant.
                    if HttpDownloader.verify_existing_file(
                        target_path,
                        url,
                        session,
                        mode=verify_mode,
                        timeout=timeout,
                        allow_private_redirects=allow_private_redirects,
                    ):
                        Logger.info(
                            f"'{os.path.basename(target_path)}' vừa được luồng khác "
                            f"hoàn tất và verify={verify_mode} đạt -> bỏ qua tải lại {url}."
                        )
                        return target_path
                    existing_needs_refresh = True
                    Logger.warning(
                        f"'{os.path.basename(target_path)}' tồn tại nhưng verify={verify_mode} "
                        "không đạt -> tải lại."
                    )
                # C19-M4: skip-if-exists theo PRESENCE (trừ khi force) — thực
                # hiện TRƯỚC khi phát bất kỳ GET nào. Điều kiện skip cũ đòi
                # Content-Length khai báo + khớp kích thước nên server
                # chunked/unknown-length vẫn tải lại và ĐÈ file hoàn chỉnh
                # đã có. .part còn tồn tại nghĩa là lần tải trước CHƯA xong
                # -> không rơi nhánh này, resume chạy bình thường ở dưới.
                if (not force and os.path.exists(target_path)
                        and not os.path.exists(part_path)
                        and not existing_needs_refresh):
                    if HttpDownloader.verify_existing_file(
                        target_path,
                        url,
                        session,
                        mode=verify_mode,
                        timeout=timeout,
                        allow_private_redirects=allow_private_redirects,
                    ):
                        Logger.info(
                            f"'{os.path.basename(target_path)}' đã tồn tại — "
                            f"verify={verify_mode} đạt, bỏ qua tải {url}."
                        )
                        return target_path
                    existing_needs_refresh = True
                    Logger.warning(
                        f"'{os.path.basename(target_path)}' tồn tại nhưng verify={verify_mode} "
                        "không đạt -> tải lại."
                    )

            while True:
                offset = 0
                req_headers = {}
                if part_path and os.path.exists(part_path):
                    offset = os.path.getsize(part_path)
                    req_headers["Range"] = f"bytes={offset}-"
                    if_range = HttpDownloader._load_if_range(part_path)
                    if if_range:
                        req_headers["If-Range"] = if_range
                    Logger.info(f"Resume download {url} từ byte {offset}...")

                try:
                    resp, final_url = HttpDownloader._request_follow_redirects(
                        session,
                        "GET",
                        url,
                        timeout=timeout,
                        stream=True,
                        headers=req_headers,
                        allow_private_redirects=allow_private_redirects,
                    )
                except requests.RequestException as e:
                    attempt += 1
                    Logger.warning(f"Lỗi kết nối khi tải {url}: {type(e).__name__}: {HttpDownloader._short_error(e)}")
                    if attempt > MAX_RESUME_ATTEMPTS:
                        Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                        return None
                    HttpDownloader._retry_backoff(attempt)
                    continue

                try:
                    status = resp.status_code
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    host = (urlparse(url).hostname or "").lower()

                    # Discord CDN: link chia sẻ chỉ sống ~24h
                    if ("cdn.discordapp.com" in host or "media.discordapp.net" in host) and status in (403, 404):
                        raise DownloadFailed("Link Discord hết hạn (~24h), hãy lấy link mới")

                    # 416 khi đang resume: có thể file .part đã tải ĐỦ (server
                    # trả 416 dù offset == content-length, file hoàn chỉnh) —
                    # nếu Content-Range khai báo total `*/<total>` khớp đúng
                    # kích thước .part thì rename thành file cuối luôn, không
                    # tải lại. Không có/không đọc được Content-Range: coi .part
                    # là corrupt (offset vượt size thật -> server trả 416 MÃI
                    # MÃI nếu giữ nguyên) -> xoá và tải lại từ đầu.
                    if status == 416 and req_headers.get("Range"):
                        content_range = resp.headers.get("Content-Range") or ""
                        cr_total = content_range.rsplit("/", 1)[-1].strip() if "/" in content_range else ""
                        if (
                            cr_total.isdigit()
                            and target_path and part_path
                            and os.path.exists(part_path)
                            and int(cr_total) == os.path.getsize(part_path)
                        ):
                            Logger.info(
                                f"Server trả 416 nhưng .part ({offset} bytes) đã đủ "
                                f"tổng {cr_total} bytes theo Content-Range -> hoàn tất {url} không cần tải lại."
                            )
                            resume_meta = HttpDownloader._load_resume_metadata(part_path)
                            os.replace(part_path, target_path)
                            HttpDownloader._record_final_metadata(
                                target_path,
                                url,
                                final_url=final_url,
                                headers=resume_meta,
                                verify_mode=verify_mode,
                            )
                            HttpDownloader._cleanup_resume_validator(part_path)
                            return target_path
                        attempt += 1
                        Logger.warning(
                            f"Server trả 416 khi resume {url} từ byte {offset}: "
                            f".part ({offset} bytes) có thể corrupt/lớn hơn file trên server -> xoá .part, tải lại từ đầu."
                        )
                        if part_path and os.path.exists(part_path):
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass
                        HttpDownloader._cleanup_resume_validator(part_path)
                        if attempt > MAX_RESUME_ATTEMPTS:
                            Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                            return None
                        HttpDownloader._retry_backoff(attempt)
                        continue

                    if status not in (200, 206):
                        Logger.warning(f"Tải thất bại {url}: HTTP {status}")
                        return None

                    # Interstitial guard: mong đợi file nhị phân nhưng nhận trang HTML
                    if "text/html" in content_type:
                        raise DownloadFailed(
                            "Nhận được HTML thay vì file (có thể là trang interstitial/quota hết hạn)"
                        )

                    total_size = 0
                    cl_known = False
                    raw_cl = resp.headers.get("Content-Length")
                    if raw_cl and str(raw_cl).strip().isdigit():
                        total_size = int(raw_cl)
                        cl_known = True   # kể cả total_size == 0 (hợp lệ)

                    # Gate kích thước NGAY TRƯỚC KHI ghi bất kỳ byte nào xuống đĩa
                    if max_size and status == 200 and total_size and total_size > max_size:
                        resp.close()
                        raise LargeFileSkipped(total_size)

                    if target_path is None:
                        if preferred_filename:
                            filename = sanitize_filename(preferred_filename)
                        else:
                            filename = extract_filename_from_headers(resp.headers, fallback_url=url)
                        target_path = os.path.join(dest_dir, filename)
                        part_path = target_path + ".part"
                        # C9-04: đích chỉ vừa biết từ headers — lấy khóa muộn
                        # nhưng vẫn tuần tự hóa toàn bộ phần ghi/rename dưới.
                        tlock, tlock_waited = HttpDownloader._acquire_target_lock(target_path)
                        if (tlock_waited and not force and os.path.exists(target_path)):
                            if HttpDownloader.verify_existing_file(
                                target_path,
                                url,
                                session,
                                mode=verify_mode,
                                timeout=timeout,
                                allow_private_redirects=allow_private_redirects,
                                remote_response=resp,
                                final_url=final_url,
                            ):
                                resp.close()
                                Logger.info(
                                    f"'{os.path.basename(target_path)}' vừa được luồng khác "
                                    f"hoàn tất và verify={verify_mode} đạt -> bỏ qua tải lại {url}."
                                )
                                return target_path
                            existing_needs_refresh = True

                    # Từ đây target/part đã được xác định cho mọi đường
                    # (preferred name hoặc Content-Disposition).
                    assert target_path is not None
                    assert part_path is not None

                    # Skip nếu file hoàn chỉnh đã tồn tại (C19-M4: theo
                    # PRESENCE trừ khi force — không đòi Content-Length khớp;
                    # .part còn nghĩa là chưa xong -> resume riêng ở dưới).
                    if (
                        not force
                        and os.path.exists(target_path)
                        and not os.path.exists(part_path)
                        and not existing_needs_refresh
                    ):
                        if HttpDownloader.verify_existing_file(
                            target_path,
                            url,
                            session,
                            mode=verify_mode,
                            timeout=timeout,
                            allow_private_redirects=allow_private_redirects,
                            remote_response=resp,
                            final_url=final_url,
                        ):
                            resp.close()
                            Logger.info(
                                f"'{os.path.basename(target_path)}' đã tồn tại — "
                                f"verify={verify_mode} đạt, bỏ qua tải {url}."
                            )
                            return target_path
                        existing_needs_refresh = True
                        Logger.warning(
                            f"'{os.path.basename(target_path)}' tồn tại nhưng verify={verify_mode} "
                            "không đạt -> tải lại."
                        )

                    append_mode = status == 206
                    if append_mode and offset:
                        # RFC 9110: một 206 dùng để resume phải bắt đầu đúng
                        # byte ta yêu cầu. Nếu proxy/server trả range lệch,
                        # append sẽ tạo file corrupt; reset partial thay vì
                        # ghép mù quáng. Giữ tương thích với server cũ không
                        # gửi Content-Range bằng cách chỉ reject header có mặt
                        # nhưng sai.
                        content_range = resp.headers.get("Content-Range") or ""
                        cr_start = HttpDownloader._content_range_start(content_range)
                        if content_range and cr_start != offset:
                            resp.close()
                            Logger.warning(
                                f"Content-Range không khớp khi resume {url}: "
                                f"yêu cầu {offset}, server trả {content_range!r}; "
                                "bỏ partial và tải lại từ đầu."
                            )
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass
                            HttpDownloader._cleanup_resume_validator(part_path)
                            attempt += 1
                            if attempt > MAX_RESUME_ATTEMPTS:
                                return None
                            HttpDownloader._retry_backoff(attempt)
                            continue

                    if not append_mode and part_path and os.path.exists(part_path):
                        # Server không hỗ trợ Range, hoặc If-Range validator
                        # không còn khớp và server trả 200 full body -> restart.
                        try:
                            os.remove(part_path)
                        except OSError:
                            pass
                        HttpDownloader._cleanup_resume_validator(part_path)

                    # Ghi validator TRƯỚC khi stream. Nếu tiến trình rớt giữa
                    # body, .part + sidecar còn lại cho lần chạy sau gửi
                    # If-Range và không nối hai phiên bản remote khác nhau.
                    HttpDownloader._save_resume_validator(part_path, resp)

                    # Acceleration: nếu file lớn (>= 8MB), chưa tải dở (.part chưa có hoặc offset=0),
                    # và server hỗ trợ Range (Accept-Ranges bytes hoặc status 200):
                    content_encoding = (resp.headers.get("Content-Encoding") or "").strip().lower()
                    accept_ranges = (resp.headers.get("Accept-Ranges") or "").strip().lower()
                    if (
                        not parallel_disabled
                        # curl_cffi Session is not shared across our segment
                        # threads. For Cloudflare-active downloads prefer the
                        # already-proven single-stream path over unsafe shared
                        # browser-session Range fan-out.
                        and getattr(session, "cloudflare_active", False) is not True
                        and not append_mode
                        and offset == 0
                        and total_size >= 8 * 1024 * 1024
                        and not content_encoding
                        and (accept_ranges == "bytes" or "bytes" in resp.headers.get("accept-ranges", "").lower())
                    ):
                        parallel_headers = dict(resp.headers or {})
                        resp.close()
                        Logger.info(f"Tăng tốc tải file lớn ({total_size // (1024*1024)}MB) qua đa luồng Range song song...")
                        if HttpDownloader._download_parallel_segments(
                            url=url,
                            target_path=target_path,
                            part_path=part_path,
                            total_size=total_size,
                            session=session,
                            timeout=timeout,
                            progress_callback=progress_callback,
                            max_size=max_size,
                            allow_private_redirects=allow_private_redirects,
                        ):
                            os.replace(part_path, target_path)
                            HttpDownloader._record_final_metadata(
                                target_path,
                                url,
                                final_url=final_url,
                                headers=parallel_headers,
                                verify_mode=verify_mode,
                            )
                            HttpDownloader._cleanup_resume_validator(part_path)
                            return target_path
                        Logger.info(f"Chuyển fallback tải tuần tự cho {url}...")
                        # Không dùng một GET ad-hoc rồi rơi thẳng xuống writer:
                        # response fallback phải đi lại TOÀN BỘ validation
                        # status/content-type/size ở đầu vòng lặp. Nếu không,
                        # HTTP 500/HTML từ fallback có thể bị ghi thành file.
                        parallel_disabled = True
                        continue

                    downloaded_bytes = offset if append_mode else 0
                    chunk_sz = _get_optimal_chunk_size(total_size)

                    with open(part_path, "ab" if append_mode else "wb") as f, closing(resp):
                        for chunk in _iter_response_content(resp, chunk_sz):
                            if not chunk:
                                continue
                            f.write(chunk)
                            downloaded_bytes += len(chunk)
                            if progress_callback:
                                progress_callback(len(chunk), total_size)
                            # Ngắt sớm nếu vượt ngưỡng trong lúc stream (chunked,
                            # không biết Content-Length trước)
                            if max_size and downloaded_bytes > max_size:
                                raise LargeFileSkipped(downloaded_bytes)

                    # Body kết thúc sớm so với Content-Length?
                    # C19-L9: phép so chỉ hợp lệ khi byte đếm được == byte
                    # TRÊN DÂY. Response gzip/deflate/br: iter_content trả
                    # bytes ĐÃ GIẢI NÉN trong khi Content-Length là cỡ NÉN —
                    # dữ liệu khó-nén giải ra NHỎ HƠN CL từng khiến check
                    # báo "thiếu dữ liệu" ảo rồi retry đến chết dù file đủ.
                    content_encoding = (
                        resp.headers.get("Content-Encoding") or ""
                    ).strip().lower()
                    if (total_size and downloaded_bytes < total_size
                            and not content_encoding):
                        attempt += 1
                        Logger.warning(
                            f"Dữ liệu tải về thiếu ({downloaded_bytes}/{total_size} bytes) từ {url}"
                        )
                        if attempt > MAX_RESUME_ATTEMPTS:
                            Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                            return None
                        HttpDownloader._retry_backoff(attempt)
                        continue

                    # Review-6 MED: body RỖNG hoàn toàn mà server KHÔNG khai
                    # báo Content-Length gần như luôn là response lỗi (proxy/
                    # CDN trả 200 với body trống), không phải file 0-byte hợp
                    # lệ. Move .part rỗng lên đích sẽ khiến presence-skip của
                    # mọi lần chạy sau nuốt vĩnh viễn một file rác -> coi là
                    # chưa-hoàn-thất: xoá .part, retry như thường. Server thực
                    # sự muốn gửi file rỗng sẽ khai báo rõ Content-Length: 0
                    # (cl_known=True — nhánh dưới vẫn chấp nhận).
                    if not cl_known and downloaded_bytes == 0:
                        attempt += 1
                        Logger.warning(
                            f"Body rỗng không khai báo Content-Length từ {url} "
                            f"— coi như chưa hoàn thành, thử lại."
                        )
                        if part_path and os.path.exists(part_path):
                            try:
                                os.remove(part_path)
                            except OSError:
                                pass
                        HttpDownloader._cleanup_resume_validator(part_path)
                        if attempt > MAX_RESUME_ATTEMPTS:
                            Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                            return None
                        HttpDownloader._retry_backoff(attempt)
                        continue

                    os.replace(part_path, target_path)
                    HttpDownloader._record_final_metadata(
                        target_path,
                        url,
                        final_url=final_url,
                        headers=dict(resp.headers or {}),
                        verify_mode=verify_mode,
                    )
                    HttpDownloader._cleanup_resume_validator(part_path)
                    return target_path

                except (requests.RequestException) as e:
                    # Mất kết nối giữa chừng -> thử resume bằng Range ở vòng lặp sau
                    attempt += 1
                    Logger.warning(
                        f"Mất kết nối giữa chừng khi tải {url}: {type(e).__name__}: {HttpDownloader._short_error(e)}"
                    )
                    if attempt > MAX_RESUME_ATTEMPTS:
                        Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                        return None
                    HttpDownloader._retry_backoff(attempt)
                    continue
        except LargeFileSkipped:
            # Dọn file tạm (.part/.tmp) — không giữ rác nửa chừng trên đĩa
            for leftover in (part_path, target_path and target_path + ".tmp"):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            HttpDownloader._cleanup_resume_validator(part_path)
            raise
        except DownloadFailed:
            raise
        except Exception as e:
            Logger.warning(f"Tải thất bại {url}: {type(e).__name__}: {HttpDownloader._short_error(e)}")
            # Dọn tệp tạm nửa chừng (.part/.tmp): dữ liệu không đảm bảo flush
            # (vd disk-full OSError), giữ lại sẽ khiến lần sau resume từ offset lỗi.
            for leftover in (part_path, target_path and target_path + ".tmp"):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            HttpDownloader._cleanup_resume_validator(part_path)
            return None
        finally:
            # C9-04: nhả khóa per-target trên MỌI đường thoát (thành công,
            # skip, LargeFileSkipped/DownloadFailed, lỗi lạ) — thread đang
            # chờ cùng đích không được treo vĩnh viễn.
            if tlock is not None:
                tlock.release()

    @staticmethod
    def save_response_stream(
        resp: requests.Response,
        dest_dir: str,
        filename: str,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_size: int = 0,
        source_url: Optional[str] = None,
        verify_mode: str = "fast",
    ) -> Optional[str]:
        """
        Saves an existing streaming response object to dest_dir/filename.
        - Interstitial guard: Content-Type text/html -> KHÔNG lưu, raise DownloadFailed.
        - max_size > 0: gate unknown-size — kiểm tra Content-Length trước khi ghi,
          ngắt sớm giữa chừng nếu vượt ngưỡng, xoá .tmp và raise LargeFileSkipped.
        """
        tmp_path: Optional[str] = None
        target_path: Optional[str] = None
        tlock = None
        verify_mode = str(verify_mode or "fast").strip().lower()
        if verify_mode not in ("fast", "normal", "strict"):
            raise ValueError(f"verify mode không hợp lệ: {verify_mode}")
        try:
            os.makedirs(dest_dir, exist_ok=True)

            content_type = (resp.headers.get("Content-Type") or "").lower()
            if "text/html" in content_type:
                raise DownloadFailed(
                    "Nhận được HTML thay vì file (có thể là trang interstitial/quota hết hạn)"
                )

            filename = sanitize_filename(filename)
            target_path = os.path.join(dest_dir, filename)
            tmp_path = target_path + ".tmp"
            tlock, _waited = HttpDownloader._acquire_target_lock(target_path)
            source_url = str(source_url or getattr(resp, "url", "") or "")

            total_size = 0
            raw_cl = resp.headers.get("Content-Length")
            if raw_cl and str(raw_cl).strip().isdigit():
                total_size = int(raw_cl)

            # Gate kích thước NGAY TRƯỚC KHI ghi
            if max_size and total_size and total_size > max_size:
                resp.close()
                raise LargeFileSkipped(total_size)

            # C19-M4: skip-if-exists theo PRESENCE (trừ khi force) — điều kiện
            # cũ đòi total_size > 0 và khớp kích thước nên stream không khai
            # báo Content-Length vẫn ĐÈ file hoàn chỉnh đã có.
            if not force and os.path.exists(target_path):
                valid_existing = (
                    verify_mode == "fast"
                    or (
                        bool(source_url)
                        and HttpDownloader.verify_existing_file(
                            target_path,
                            source_url,
                            None,
                            mode=verify_mode,
                            remote_response=resp,
                            final_url=str(getattr(resp, "url", "") or source_url),
                        )
                    )
                )
                if valid_existing:
                    resp.close()
                    Logger.info(
                        f"'{filename}' đã tồn tại — verify={verify_mode} đạt, "
                        "bỏ qua ghi đè stream."
                    )
                    return target_path
                Logger.warning(
                    f"'{filename}' tồn tại nhưng verify={verify_mode} không đạt -> ghi lại stream."
                )

            downloaded_bytes = 0
            chunk_sz = _get_optimal_chunk_size(total_size)
            with open(tmp_path, "wb") as f, closing(resp):
                for chunk in _iter_response_content(resp, chunk_sz):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    if progress_callback:
                        progress_callback(len(chunk), total_size)
                    if max_size and downloaded_bytes > max_size:
                        raise LargeFileSkipped(downloaded_bytes)

            os.replace(tmp_path, target_path)
            HttpDownloader._record_final_metadata(
                target_path,
                source_url or str(getattr(resp, "url", "") or ""),
                final_url=str(getattr(resp, "url", "") or source_url or ""),
                headers=dict(resp.headers or {}),
                verify_mode=verify_mode,
            )
            return target_path
        except LargeFileSkipped:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            raise
        except DownloadFailed:
            raise
        except Exception as e:
            Logger.warning(f"Lỗi khi ghi stream ra file '{filename}': {type(e).__name__}: {HttpDownloader._short_error(e)}")
            # Dọn .tmp nửa chừng (vd disk-full OSError): dữ liệu không đảm bảo
            # flush, giữ lại chỉ là rác trên đĩa.
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            return None
        finally:
            if tlock is not None:
                tlock.release()
