"""InstanceService — quản lý container động (body cũ của instance_manager.InstanceManager).

InstanceManager trong ``ctf_downloader.instance_manager`` giờ chỉ là facade mỏng
delegate về đây. Method mới so với bản cũ:
  - ``sync_containers()``: logic ``--sync`` từ script instance.py
  - ``interactive_pick()``: menu interactive chọn challenge + action (dùng chung
    cho cli.handle_instance / instance.py / interactive_menu)
"""
import datetime
import os
import re
from typing import Any, Dict, List, Optional, Tuple

from ..services.platform_resolver import PlatformResolver
from ..storage.constants import TARGET_CONNECTION_FMT
from ..storage.workspace_repo import WorkspaceRepo
from ..utils.logger import Logger
from ..ui.diagnostics import Diagnostic, render as render_diagnostic
from rich.markup import escape

# Hint dùng chung cho mọi vấn đề kết nối / xác thực nền tảng (spec §4.6).
_DOCTOR_HINTS = (
    "chạy 'ctf doctor -u <url>' để kiểm tra cookie/token và kết nối nền tảng",
)


def diag_detect_failure(exc: Exception) -> Diagnostic:
    """Không dựng được platform adapter cho workspace."""
    return Diagnostic(
        "error",
        "Không khởi tạo được adapter nền tảng cho workspace này",
        cause=f"{type(exc).__name__}: {exc}" if str(exc) else type(exc).__name__,
        hints=(
            "kiểm tra challenges.json có ctf_info.url / platform_url hợp lệ",
            *_DOCTOR_HINTS,
            "nếu workspace trống, chạy 'ctf pull -u <url>' để tải lại",
        ),
    )


def diag_start_instance_fail(challenge_id: Any, name: str, msg: str) -> Diagnostic:
    """Không tạo được container instance cho challenge."""
    return Diagnostic(
        "error",
        f"Không tạo được container instance cho {name} (ID: {challenge_id})",
        cause=msg or None,
        hints=(
            *_DOCTOR_HINTS,
            "kiểm tra quota/giới hạn container trên nền tảng "
            "(có thể đã hết slot hoặc hết thời gian)",
            "chạy 'ctf instance status <id>' xem trạng thái hiện tại rồi thử start lại",
        ),
    )


class InstanceService:
    def __init__(self, workspace_path: str, cookie: Optional[str] = None, token: Optional[str] = None):
        self.workspace_path = os.path.abspath(workspace_path)
        self.cookie = cookie
        self.token = token
        self.repo = WorkspaceRepo(self.workspace_path)
        self.challenges_data = self._load_challenges_data()
        self.platform = self._init_platform()

    def _load_challenges_data(self) -> Dict[str, Any]:
        data = self.repo.read_challenges()
        if not data:
            Logger.warning('Không đọc được challenges.json')
        return data

    def _init_platform(self):
        try:
            session, platform, _info = PlatformResolver.for_workspace(
                self.repo,
                cookie=self.cookie,
                token=self.token,
            )
        except Exception as exc:
            # Lỗi phát hiện nền tảng: render Diagnostic rồi lan raise như cũ
            # (caller giữ nguyên hành vi pipeline / exit code).
            render_diagnostic(diag_detect_failure(exc))
            raise
        return platform

    def find_challenge(self, challenge_id: Optional[Any] = None, challenge_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        # 1. Tra trong challenges.json
        for c in (self.challenges_data or {}).get('challenges', []):
            if challenge_id is not None and str(c.get('id')) == str(challenge_id):
                return c
            if challenge_name and challenge_name.lower() in str(c.get('name', '')).lower():
                return c

        # 2. Fallback: metadata.json trong các thư mục challenge
        for meta_path in self.repo.iter_challenges():
            m = self.repo.read_metadata(meta_path)
            if not m:
                continue
            if challenge_id is not None and str(m.get('id')) == str(challenge_id):
                m['_local_path'] = str(meta_path.parent)
                return m
            if challenge_name and challenge_name.lower() in str(m.get('name', '')).lower():
                m['_local_path'] = str(meta_path.parent)
                return m
        return None

    # ------------------------------------------------------------------
    # Container actions
    # ------------------------------------------------------------------

    def start_instance(self, challenge_id: Any) -> Tuple[bool, Dict[str, Any]]:
        chall = self.find_challenge(challenge_id=challenge_id)
        name = chall.get('name', f'Challenge {challenge_id}') if chall else f'ID {challenge_id}'

        Logger.info(f'Đang khởi động container instance cho [bold][info]{escape(str(name))}[/info][/bold] (ID: {challenge_id})...', markup=True)
        success, info = self.platform.start_instance(challenge_id)

        if success:
            entry = info.get('entry')
            time_left = info.get('time_left') or info.get('close_time') or info.get('remain')

            # If entry not returned immediately, poll status once
            if not entry:
                import time
                time.sleep(1.5)
                st = self.platform.get_instance_status(challenge_id)
                if st.get('entry'):
                    entry = st.get('entry')
                    time_left = st.get('time_left') or time_left

            Logger.success(f'Container instance của [bold][info]{escape(str(name))}[/info][/bold] đã hoạt động!', markup=True)
            if entry:
                Logger.info(f'Điểm kết nối (entry): [info]{escape(str(entry))}[/info]', markup=True)
                if ':' in str(entry) and not str(entry).startswith('http'):
                    h, p = str(entry).split(':')
                    Logger.info(f'Lệnh netcat: [literal]{escape(f"nc {h} {p}")}[/literal]', markup=True)
            if time_left:
                Logger.info(f'Thời gian còn lại: [fg.base]{escape(str(time_left))}[/fg.base]', markup=True)

            self._update_local_instance_info(challenge_id, entry, time_left, status='running')
            return True, info
        else:
            msg = info.get('message', 'Lỗi không xác định')
            render_diagnostic(diag_start_instance_fail(challenge_id, name, msg))
            return False, info

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        chall = self.find_challenge(challenge_id=challenge_id)
        name = chall.get('name', f'Challenge {challenge_id}') if chall else f'ID {challenge_id}'

        Logger.info(f'Đang dừng container instance cho [bold][info]{escape(str(name))}[/info][/bold] (ID: {challenge_id})...', markup=True)
        success, msg = self.platform.stop_instance(challenge_id)
        if success:
            Logger.success(f'Đã dừng container cho {name}: {msg}')
            self._update_local_instance_info(challenge_id, entry=None, time_left=0, status='stopped')
        else:
            Logger.error(f'Dừng container thất bại: {msg}')
        return success, msg

    def extend_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        chall = self.find_challenge(challenge_id=challenge_id)
        name = chall.get('name', f'Challenge {challenge_id}') if chall else f'ID {challenge_id}'

        Logger.info(f'Đang gia hạn thời gian container cho [bold][info]{escape(str(name))}[/info][/bold] (ID: {challenge_id})...', markup=True)
        success, msg = self.platform.extend_instance(challenge_id)
        if success:
            Logger.success(f'Đã gia hạn container cho {name}: {msg}')
            st = self.platform.get_instance_status(challenge_id)
            if st.get('status') == 'running':
                self._update_local_instance_info(challenge_id, st.get('entry'), st.get('time_left'), status='running')
        else:
            Logger.error(f'Gia hạn container thất bại: {msg}')
        return success, msg

    def get_status(self, challenge_id: Any) -> Dict[str, Any]:
        st = self.platform.get_instance_status(challenge_id)
        if st.get('status') == 'running' and st.get('entry'):
            self._update_local_instance_info(challenge_id, st.get('entry'), st.get('time_left'), status='running')
        elif st.get('status') == 'stopped':
            self._update_local_instance_info(challenge_id, entry=None, time_left=0, status='stopped')
        return st

    def list_containers(self) -> List[Dict[str, Any]]:
        results = []
        for meta_path in self.repo.iter_challenges():
            m = self.repo.read_metadata(meta_path)
            if not m:
                continue
            if self.repo.is_container(m):
                m['_local_path'] = str(meta_path.parent)
                results.append(m)
        return results

    # ------------------------------------------------------------------
    # Sync toàn bộ container trong workspace (logic --sync của instance.py)
    # ------------------------------------------------------------------

    def sync_containers(self) -> int:
        """Scan và đồng bộ trạng thái mọi container challenge trong workspace.

        Trả về số container đang chạy (active) sau khi sync.
        """
        containers = self.list_containers()
        Logger.info(f'Đang quét và đồng bộ {len(containers)} challenge container...')
        active_count = 0
        for c in containers:
            cid = c.get('id')
            cname = c.get('name')
            st = self.get_status(cid)
            if st.get('status') == 'running' or st.get('entry'):
                active_count += 1
                Logger.success(f"[RUNNING] ID {cid} ({escape(str(cname))}): [info]{escape(str(st.get('entry')))}[/info]", markup=True)
        Logger.info(f'Sync hoàn tất! Có {active_count} container đang chạy.')
        return active_count

    # ------------------------------------------------------------------
    # Interactive menu dùng chung (cli.handle_instance / instance.py /
    # interactive_menu) — gom input() về một chỗ duy nhất
    # ------------------------------------------------------------------

    def interactive_pick(self):
        """Menu interactive: chọn challenge -> chọn action -> thực thi."""
        containers = self.list_containers()
        if not containers:
            Logger.warning('Không phát hiện challenge container nào. Nhập tay challenge ID.')
            chall_id = input('Nhập Challenge ID: ').strip()
        else:
            print("\nChọn challenge cần quản lý:")
            for idx, c in enumerate(containers, 1):
                print(f'  [{idx}] {c.get("name")} (ID: {c.get("id")}, {c.get("category")})')
            choice = input(f'Chọn (1-{len(containers)}): ').strip()
            try:
                selected = containers[int(choice) - 1]
                chall_id = selected.get('id')
            except Exception:
                Logger.error('Lựa chọn không hợp lệ.')
                return

        print("\nHành động:")
        print('  [1] Khởi động / Gia hạn container')
        print('  [2] Xem trạng thái container')
        print('  [3] Gia hạn thời lượng container')
        print('  [4] Dừng / Hủy container')

        act = input('Chọn (1-4): ').strip()
        if act == '1':
            self.start_instance(chall_id)
        elif act == '2':
            st = self.get_status(chall_id)
            Logger.info(f'Trạng thái: {st}')
        elif act == '3':
            self.extend_instance(chall_id)
        elif act == '4':
            self.stop_instance(chall_id)

    # ------------------------------------------------------------------
    # Sync thông tin instance vào workspace (metadata/README/solve.py/challenges.json)
    # ------------------------------------------------------------------

    def _update_local_instance_info(self, challenge_id: Any, entry: Optional[str], time_left: Any, status: str = 'running'):
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Update challenge metadata.json, writeup/README.md, and solver/solve.py
        for meta_path in self.repo.iter_challenges():
            m = self.repo.read_metadata(meta_path)
            if not m or str(m.get('id')) != str(challenge_id):
                continue

            try:
                # Read-mutate-write trong cùng khóa flock với update_status
                # (W5.2: write_metadata unlocked gây lost update đa tiến trình).
                def _mut(m: dict) -> dict:
                    m = dict(m or {})
                    inst = m.get('instance_info')
                    if not isinstance(inst, dict):
                        inst = {}

                    inst['is_container'] = True
                    inst['status'] = status
                    inst['last_updated'] = now_str

                    if entry:
                        m['connection_info'] = entry
                        inst['active_instance'] = entry
                        inst['last_entry'] = entry
                        inst['remaining_time'] = time_left
                    elif status == 'stopped':
                        inst['active_instance'] = None
                        inst['remaining_time'] = 0
                    m['instance_info'] = inst
                    return m

                self.repo.update_metadata(meta_path, _mut)
                Logger.info(f'[solved]✔[/solved] Đã đồng bộ thông tin instance vào: [info]{escape(os.path.relpath(meta_path, self.workspace_path))}[/info]', markup=True)

                # Mirror trục container của status đa chiều (spec §7).
                # Trạng thái khác running/stopped (vd 'unknown') -> KHÔNG đụng
                # trục container, giữ nguyên giá trị hiện có.
                try:
                    def _mut_container(st):
                        if status == 'running':
                            st['container'] = 'running'
                        elif status == 'stopped':
                            st['container'] = 'stopped'
                        return st

                    self.repo.update_status(meta_path, _mut_container)
                except Exception as e:
                    Logger.warning(f'Không thể mirror trạng thái container: {e}')

                root = meta_path.parent

                # Update writeup/README.md or README.md
                for doc_rel in [root / 'writeup' / 'README.md', root / 'README.md']:
                    if doc_rel.exists() and entry:
                        try:
                            doc_text = doc_rel.read_text(encoding='utf-8')

                            # Update Target Connection (anchor đầu dòng + count=1)
                            if 'Target Connection:' in doc_text:
                                doc_text = re.sub(
                                    r'^-\s*Target Connection:\s*`?[^`\n]+`?',
                                    TARGET_CONNECTION_FMT.format(info=entry),
                                    doc_text, count=1, flags=re.M,
                                )
                            doc_rel.write_text(doc_text, encoding='utf-8')
                        except Exception:
                            pass

                # Update solver/solve.py if URL or host/port pattern found
                solve_path = root / 'solver' / 'solve.py'
                if solve_path.exists() and entry:
                    try:
                        solve_text = solve_path.read_text(encoding='utf-8')

                        if entry.startswith('http'):
                            solve_text = re.sub(
                                r'^TARGET_URL\s*=\s*["\'][^"\']+["\']',
                                f'TARGET_URL = "{entry}"',
                                solve_text, count=1, flags=re.M,
                            )
                            solve_text = re.sub(
                                r'default=["\']https?://[^"\']+["\']',
                                f'default="{entry}"',
                                solve_text, count=1,
                            )
                        elif ':' in entry and not entry.startswith('http'):
                            h, p = entry.split(':')
                            solve_text = re.sub(
                                r'^HOST\s*=\s*["\'][^"\']+["\']',
                                f'HOST = "{h}"',
                                solve_text, count=1, flags=re.M,
                            )
                            solve_text = re.sub(
                                r'^PORT\s*=\s*\d+',
                                f'PORT = {p}',
                                solve_text, count=1, flags=re.M,
                            )

                        solve_path.write_text(solve_text, encoding='utf-8')
                    except Exception:
                        pass
                break
            except Exception as e:
                Logger.warning(f'Không thể cập nhật metadata: {e}')

        # 2. Update top-level challenges.json if present
        def _mut(data: dict) -> dict:
            challs = data.get('challenges', []) if isinstance(data, dict) else []
            for c in challs:
                if isinstance(c, dict) and str(c.get('id')) == str(challenge_id):
                    if entry:
                        c['connection_info'] = entry
                    inst = c.get('instance_info')
                    if not isinstance(inst, dict):
                        inst = {}
                    c['instance_info'] = inst
                    inst['status'] = status
                    if entry:
                        inst['active_instance'] = entry
                        inst['remaining_time'] = time_left
                    break
            return data

        if os.path.exists(self.repo.challenges_path):
            self.repo.mutate_challenges(_mut)
