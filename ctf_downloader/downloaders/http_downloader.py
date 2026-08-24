import os
import shutil
import requests
from typing import Optional, Callable
from urllib.parse import urlparse, urljoin
from ..utils.logger import Logger
from ..utils.sanitize import sanitize_filename, extract_filename_from_headers, extract_filename_from_url
from .registry import register_downloader

CHUNK_SIZE = 65536
# Số lần thử resume (mở lại kết nối + Range) khi mất kết nối giữa chừng
MAX_RESUME_ATTEMPTS = 3
# Các HTTP redirect được theo dõi khi probe thủ công
_REDIRECT_CODES = (301, 302, 303, 307, 308)
_MAX_PROBE_REDIRECTS = 10


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
    def _short_error(exc: Exception, max_len: int = 200) -> str:
        return str(exc).replace("\n", " ")[:max_len]

    @staticmethod
    def probe_content_length(
        url: str,
        session: requests.Session,
        timeout: int = 30
    ) -> Optional[int]:
        """
        Pre-flight: HEAD request trên URL để đọc Content-Length trước khi tải.
        Theo redirect THỦ CÔNG từng bước để giữ nguyên signed URL (không bị requests
        drop query params). Trả về None nếu không xác định được (HEAD lỗi, redirect
        vòng, status lỗi, thiếu header...) — khi đó file sẽ được xử lý kiểu
        unknown-size (kiểm tra trong lúc stream).
        """
        current_url = url
        for _ in range(_MAX_PROBE_REDIRECTS):
            try:
                resp = session.head(current_url, timeout=timeout, allow_redirects=False)
            except requests.RequestException as e:
                Logger.warning(f"Không xác định trước dung lượng của {url}: {type(e).__name__}: {HttpDownloader._short_error(e)}")
                return None

            status = resp.status_code
            if status in _REDIRECT_CODES:
                location = resp.headers.get("Location")
                if not location:
                    return None
                current_url = urljoin(current_url, location)
                continue

            if status == 304 or status >= 400:
                # HEAD không khả dụng trên host này -> coi như unknown
                return None

            content_length = resp.headers.get("Content-Length") or resp.headers.get("content-length")
            if content_length and str(content_length).strip().isdigit():
                return int(content_length)
            return None
        return None

    @staticmethod
    def download_file(
        url: str,
        dest_dir: str,
        session: requests.Session,
        preferred_filename: Optional[str] = None,
        timeout: int = 30,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_size: int = 0
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

        try:
            os.makedirs(dest_dir, exist_ok=True)

            while True:
                offset = 0
                req_headers = {}
                if part_path and os.path.exists(part_path):
                    offset = os.path.getsize(part_path)
                    req_headers["Range"] = f"bytes={offset}-"
                    Logger.info(f"Resume download {url} từ byte {offset}...")

                try:
                    resp = session.get(url, stream=True, timeout=timeout, allow_redirects=True, headers=req_headers)
                except requests.RequestException as e:
                    attempt += 1
                    Logger.warning(f"Lỗi kết nối khi tải {url}: {type(e).__name__}: {HttpDownloader._short_error(e)}")
                    if attempt > MAX_RESUME_ATTEMPTS:
                        Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                        return None
                    continue

                try:
                    status = resp.status_code
                    content_type = (resp.headers.get("Content-Type") or "").lower()
                    host = (urlparse(url).hostname or "").lower()

                    # Discord CDN: link chia sẻ chỉ sống ~24h
                    if ("cdn.discordapp.com" in host or "media.discordapp.net" in host) and status in (403, 404):
                        raise DownloadFailed("Link Discord hết hạn (~24h), hãy lấy link mới")

                    if status not in (200, 206):
                        Logger.warning(f"Tải thất bại {url}: HTTP {status}")
                        return None

                    # Interstitial guard: mong đợi file nhị phân nhưng nhận trang HTML
                    if "text/html" in content_type:
                        raise DownloadFailed(
                            "Nhận được HTML thay vì file (có thể là trang interstitial/quota hết hạn)"
                        )

                    total_size = 0
                    raw_cl = resp.headers.get("Content-Length")
                    if raw_cl and str(raw_cl).strip().isdigit():
                        total_size = int(raw_cl)

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

                    # Skip nếu đã tồn tại file hoàn chỉnh cùng kích thước
                    if (
                        not force and status == 200 and total_size > 0
                        and os.path.exists(target_path) and not os.path.exists(part_path)
                        and os.path.getsize(target_path) == total_size
                    ):
                        resp.close()
                        return target_path

                    append_mode = status == 206
                    if not append_mode and part_path and os.path.exists(part_path):
                        # Server không hỗ trợ Range (trả 200 thay vì 206) -> tải lại từ đầu
                        try:
                            os.remove(part_path)
                        except OSError:
                            pass

                    downloaded_bytes = offset if append_mode else 0

                    with open(part_path, "ab" if append_mode else "wb") as f, resp:
                        for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
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
                    if total_size and downloaded_bytes < total_size:
                        attempt += 1
                        Logger.warning(
                            f"Dữ liệu tải về thiếu ({downloaded_bytes}/{total_size} bytes) từ {url}"
                        )
                        if attempt > MAX_RESUME_ATTEMPTS:
                            Logger.warning(f"Tải thất bại {url}: quá số lần thử lại ({MAX_RESUME_ATTEMPTS}).")
                            return None
                        continue

                    shutil.move(part_path, target_path)
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
                    continue
        except LargeFileSkipped:
            # Dọn file tạm (.part/.tmp) — không giữ rác nửa chừng trên đĩa
            for leftover in (part_path, target_path and target_path + ".tmp"):
                if leftover and os.path.exists(leftover):
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
            raise
        except DownloadFailed:
            raise
        except Exception as e:
            Logger.warning(f"Tải thất bại {url}: {type(e).__name__}: {HttpDownloader._short_error(e)}")
            return None

    @staticmethod
    def save_response_stream(
        resp: requests.Response,
        dest_dir: str,
        filename: str,
        force: bool = False,
        progress_callback: Optional[Callable[[int, int], None]] = None,
        max_size: int = 0
    ) -> Optional[str]:
        """
        Saves an existing streaming response object to dest_dir/filename.
        - Interstitial guard: Content-Type text/html -> KHÔNG lưu, raise DownloadFailed.
        - max_size > 0: gate unknown-size — kiểm tra Content-Length trước khi ghi,
          ngắt sớm giữa chừng nếu vượt ngưỡng, xoá .tmp và raise LargeFileSkipped.
        """
        tmp_path: Optional[str] = None
        target_path: Optional[str] = None
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

            total_size = 0
            raw_cl = resp.headers.get("Content-Length")
            if raw_cl and str(raw_cl).strip().isdigit():
                total_size = int(raw_cl)

            # Gate kích thước NGAY TRƯỚC KHI ghi
            if max_size and total_size and total_size > max_size:
                resp.close()
                raise LargeFileSkipped(total_size)

            if not force and os.path.exists(target_path) and total_size > 0:
                if os.path.getsize(target_path) == total_size:
                    resp.close()
                    return target_path

            downloaded_bytes = 0
            with open(tmp_path, "wb") as f, resp:
                for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                    if not chunk:
                        continue
                    f.write(chunk)
                    downloaded_bytes += len(chunk)
                    if progress_callback:
                        progress_callback(len(chunk), total_size)
                    if max_size and downloaded_bytes > max_size:
                        raise LargeFileSkipped(downloaded_bytes)

            shutil.move(tmp_path, target_path)
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
            return None
