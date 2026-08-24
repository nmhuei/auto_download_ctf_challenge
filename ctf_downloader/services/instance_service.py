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
            Logger.warning('Could not load challenges.json')
        return data

    def _init_platform(self):
        session, platform, _info = PlatformResolver.for_workspace(
            self.repo,
            cookie=self.cookie,
            token=self.token,
        )
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

        Logger.info(f'Launching container instance for [bold cyan]{name}[/bold cyan] (ID: {challenge_id})...')
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

            Logger.success(f'Container instance active for [bold cyan]{name}[/bold cyan]!')
            if entry:
                Logger.info(f'Entry Point: [bold green]{entry}[/bold green]')
                if ':' in str(entry) and not str(entry).startswith('http'):
                    h, p = str(entry).split(':')
                    Logger.info(f'Netcat command: [bold yellow]nc {h} {p}[/bold yellow]')
            if time_left:
                Logger.info(f'Remaining Lifetime: [bold magenta]{time_left}[/bold magenta]')

            self._update_local_instance_info(challenge_id, entry, time_left, status='running')
            return True, info
        else:
            msg = info.get('message', 'Unknown error')
            Logger.error(f'Failed to start container: {msg}')
            return False, info

    def stop_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        chall = self.find_challenge(challenge_id=challenge_id)
        name = chall.get('name', f'Challenge {challenge_id}') if chall else f'ID {challenge_id}'

        Logger.info(f'Stopping container instance for [bold cyan]{name}[/bold cyan] (ID: {challenge_id})...')
        success, msg = self.platform.stop_instance(challenge_id)
        if success:
            Logger.success(f'Container stopped for {name}: {msg}')
            self._update_local_instance_info(challenge_id, entry=None, time_left=0, status='stopped')
        else:
            Logger.error(f'Failed to stop container: {msg}')
        return success, msg

    def extend_instance(self, challenge_id: Any) -> Tuple[bool, str]:
        chall = self.find_challenge(challenge_id=challenge_id)
        name = chall.get('name', f'Challenge {challenge_id}') if chall else f'ID {challenge_id}'

        Logger.info(f'Extending container time for [bold cyan]{name}[/bold cyan] (ID: {challenge_id})...')
        success, msg = self.platform.extend_instance(challenge_id)
        if success:
            Logger.success(f'Container extended for {name}: {msg}')
            st = self.platform.get_instance_status(challenge_id)
            if st.get('status') == 'running':
                self._update_local_instance_info(challenge_id, st.get('entry'), st.get('time_left'), status='running')
        else:
            Logger.error(f'Failed to extend container: {msg}')
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
        Logger.info(f'Scanning and syncing {len(containers)} container challenges...')
        active_count = 0
        for c in containers:
            cid = c.get('id')
            cname = c.get('name')
            st = self.get_status(cid)
            if st.get('status') == 'running' or st.get('entry'):
                active_count += 1
                Logger.success(f"[RUNNING] ID {cid} ({cname}): [bold green]{st.get('entry')}[/bold green]")
        Logger.info(f'Sync complete! Found {active_count} active running container(s).')
        return active_count

    # ------------------------------------------------------------------
    # Interactive menu dùng chung (cli.handle_instance / instance.py /
    # interactive_menu) — gom input() về một chỗ duy nhất
    # ------------------------------------------------------------------

    def interactive_pick(self):
        """Menu interactive: chọn challenge -> chọn action -> thực thi."""
        containers = self.list_containers()
        if not containers:
            Logger.warning('No container challenges detected. Enter challenge ID manually.')
            chall_id = input('Enter Challenge ID: ').strip()
        else:
            print("\nSelect Challenge to manage:")
            for idx, c in enumerate(containers, 1):
                print(f'  [{idx}] {c.get("name")} (ID: {c.get("id")}, {c.get("category")})')
            choice = input(f'Choice (1-{len(containers)}): ').strip()
            try:
                selected = containers[int(choice) - 1]
                chall_id = selected.get('id')
            except Exception:
                Logger.error('Invalid choice.')
                return

        print("\nAction:")
        print('  [1] Start / Renew Container')
        print('  [2] Check Container Status')
        print('  [3] Extend Container Lifetime')
        print('  [4] Stop / Destroy Container')

        act = input('Choice (1-4): ').strip()
        if act == '1':
            self.start_instance(chall_id)
        elif act == '2':
            st = self.get_status(chall_id)
            Logger.info(f'Status: {st}')
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
                Logger.info(f'[bold green]✓[/bold green] Synced instance details into: [cyan]{os.path.relpath(meta_path, self.workspace_path)}[/cyan]')

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
                    Logger.warning(f'Could not mirror container status: {e}')

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
                Logger.warning(f'Could not update metadata: {e}')

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
