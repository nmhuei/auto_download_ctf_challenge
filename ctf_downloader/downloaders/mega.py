import os
import shutil
import subprocess
from typing import Optional, Tuple

from ..utils.logger import Logger

# Không tự implement crypto Mega — shell-out sang megatools.
MEGA_TOOL_CANDIDATES = ("megadl", "mega-get")
MEGA_MISSING_TOOL_MESSAGE = "Cần cài megatools (megadl) để tải link Mega"


class MegaDownloader:
    """
    Wrapper mỏng gọi binary megatools (megadl / mega-get) để tải link mega.nz.
    Tool được kiểm tra bằng shutil.which; nếu không có thì link Mega được classify
    thành không tải được với message hướng dẫn cài đặt.
    """

    @staticmethod
    def available_tool() -> Optional[str]:
        for tool in MEGA_TOOL_CANDIDATES:
            tool_path = shutil.which(tool)
            if tool_path:
                return tool_path
        return None

    @staticmethod
    def download(url: str, dest_dir: str, timeout: int = 600) -> Tuple[Optional[str], str]:
        """
        Tải `url` vào `dest_dir` bằng megatools.
        Returns (saved_path_or_None, message).
        """
        tool_path = MegaDownloader.available_tool()
        if not tool_path:
            Logger.warning(MEGA_MISSING_TOOL_MESSAGE)
            return None, MEGA_MISSING_TOOL_MESSAGE

        os.makedirs(dest_dir, exist_ok=True)

        try:
            before = set(os.listdir(dest_dir))
        except OSError:
            before = set()

        tool_name = os.path.basename(tool_path)
        if tool_name == "megadl":
            cmd = [tool_path, "--path", dest_dir, url]
        else:  # mega-get <url> [destination]
            cmd = [tool_path, url, dest_dir]

        try:
            proc = subprocess.run(
                cmd,
                cwd=dest_dir,
                timeout=timeout,
                capture_output=True,
                text=True
            )
        except FileNotFoundError:
            return None, MEGA_MISSING_TOOL_MESSAGE
        except subprocess.TimeoutExpired:
            msg = f"megatools ({tool_name}) timeout sau {timeout}s khi tải {url}."
            Logger.warning(msg)
            return None, msg

        if proc.returncode != 0:
            err_text = (proc.stderr or proc.stdout or "").strip().splitlines()
            detail = err_text[-1][:200] if err_text else "(không có stderr)"
            msg = f"megatools ({tool_name}) thất bại (exit {proc.returncode}): {detail}"
            Logger.warning(msg)
            return None, msg

        # Xác định file vừa tải về (file mới xuất hiện trong dest_dir)
        try:
            after = set(os.listdir(dest_dir))
            new_files = sorted(after - before)
        except OSError:
            new_files = []

        if len(new_files) == 1:
            saved_path = os.path.join(dest_dir, new_files[0])
            Logger.info(f"megatools: đã tải '{new_files[0]}'.")
            return saved_path, "Downloaded via megatools"
        if len(new_files) > 1:
            return dest_dir, f"Downloaded {len(new_files)} files via megatools"

        # Tool báo thành công nhưng không phát hiện file mới (có thể ghi đè file cũ)
        return dest_dir, "Downloaded via megatools"
