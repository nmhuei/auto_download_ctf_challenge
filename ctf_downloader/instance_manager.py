import os
import json
import re
from typing import Dict, Any, Optional, Tuple, List
from .utils.http_client import create_session
from .platforms.detector import PlatformDetector
from .platforms.gzctf import GZCTFPlatform
from .platforms.ctfd import CTFdPlatform
from .platforms.rctf import RCTFPlatform
from .platforms.custom_rest import CustomRESTPlatform
from .utils.logger import Logger

class InstanceManager:
    def __init__(self, workspace_path: str, cookie: Optional[str] = None, token: Optional[str] = None):
        self.workspace_path = os.path.abspath(workspace_path)
        self.cookie = cookie
        self.token = token
        self.challenges_data = self._load_challenges_data()
        self.platform = self._init_platform()

    def _load_challenges_data(self) -> Dict[str, Any]:
        json_path = os.path.join(self.workspace_path, 'challenges.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                Logger.warning(f'Could not load challenges.json: {e}')
        return {}

    def _init_platform(self):
        ctf_info = self.challenges_data.get('ctf_info', {})
        base_url = ctf_info.get('url')
        platform_type = ctf_info.get('platform', 'generic').lower()

        if not base_url:
            # Try to infer from metadata.json in subdirectories
            for root, _, files in os.walk(self.workspace_path):
                if 'metadata.json' in files:
                    try:
                        with open(os.path.join(root, 'metadata.json'), 'r', encoding='utf-8') as f:
                            m = json.load(f)
                            sub_url = m.get('submit_endpoint')
                            if sub_url:
                                base_url = '/'.join(sub_url.split('/')[:3])
                                break
                    except Exception:
                        pass

        if not base_url:
            raise ValueError(f'Could not determine CTF platform URL from workspace: {self.workspace_path}')

        session = create_session(cookie=self.cookie, token=self.token)

        if platform_type == 'gzctf' or 'gzctf' in base_url or 'infosecptit' in base_url:
            plat = GZCTFPlatform(base_url, session)
            game_id = ctf_info.get('game_id')
            if game_id:
                plat.game_id = game_id
            return plat
        elif platform_type == 'rctf':
            return RCTFPlatform(base_url, session)
        elif platform_type == 'ctfd':
            return CTFdPlatform(base_url, session)
        elif platform_type == 'custom_rest':
            return CustomRESTPlatform(base_url, session)
        else:
            return PlatformDetector.detect_and_init(base_url, session)

    def find_challenge(self, challenge_id: Optional[Any] = None, challenge_name: Optional[str] = None) -> Optional[Dict[str, Any]]:
        challs = self.challenges_data.get('challenges', [])
        for c in challs:
            if challenge_id is not None and str(c.get('id')) == str(challenge_id):
                return c
            if challenge_name and challenge_name.lower() in str(c.get('name', '')).lower():
                return c

        for root, _, files in os.walk(self.workspace_path):
            if 'metadata.json' in files:
                try:
                    with open(os.path.join(root, 'metadata.json'), 'r', encoding='utf-8') as f:
                        m = json.load(f)
                        if challenge_id is not None and str(m.get('id')) == str(challenge_id):
                            m['_local_path'] = root
                            return m
                        if challenge_name and challenge_name.lower() in str(m.get('name', '')).lower():
                            m['_local_path'] = root
                            return m
                except Exception:
                    pass
        return None

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
        for root, _, files in os.walk(self.workspace_path):
            if 'metadata.json' in files:
                try:
                    with open(os.path.join(root, 'metadata.json'), 'r', encoding='utf-8') as f:
                        m = json.load(f)
                        raw = m.get('raw', {})
                        tags = m.get('tags', [])
                        inst = m.get('instance_info', {})
                        
                        is_cont = inst.get('is_container') or m.get('type') == 'DynamicContainer' or raw.get('type') == 'dynamic_docker' or raw.get('type') == 'DynamicContainer' or 'container' in [str(t).lower() for t in tags]
                        
                        if is_cont:
                            m['_local_path'] = root
                            results.append(m)
                except Exception:
                    pass
        return results

    def _update_local_instance_info(self, challenge_id: Any, entry: Optional[str], time_left: Any, status: str = 'running'):
        import datetime
        now_str = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        # 1. Update challenge metadata.json, writeup/README.md, and solver/solve.py
        for root, _, files in os.walk(self.workspace_path):
            if 'metadata.json' in files:
                try:
                    meta_path = os.path.join(root, 'metadata.json')
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        m = json.load(f)
                    
                    if str(m.get('id')) == str(challenge_id):
                        if 'instance_info' not in m or not isinstance(m['instance_info'], dict):
                            m['instance_info'] = {}
                        
                        m['instance_info']['is_container'] = True
                        m['instance_info']['status'] = status
                        m['instance_info']['last_updated'] = now_str
                        
                        if entry:
                            m['connection_info'] = entry
                            m['instance_info']['active_instance'] = entry
                            m['instance_info']['last_entry'] = entry
                            m['instance_info']['remaining_time'] = time_left
                        elif status == 'stopped':
                            m['instance_info']['active_instance'] = None
                            m['instance_info']['remaining_time'] = 0

                        with open(meta_path, 'w', encoding='utf-8') as f:
                            json.dump(m, f, indent=2, ensure_ascii=False)
                        Logger.info(f'[bold green]✓[/bold green] Synced instance details into: [cyan]{os.path.relpath(meta_path, self.workspace_path)}[/cyan]')

                        # Update writeup/README.md or README.md
                        for doc_rel in [os.path.join(root, 'writeup', 'README.md'), os.path.join(root, 'README.md')]:
                            if os.path.exists(doc_rel) and entry:
                                try:
                                    with open(doc_rel, 'r', encoding='utf-8') as rf:
                                        doc_text = rf.read()
                                    
                                    # Update Target Connection
                                    if 'Target Connection:' in doc_text:
                                        doc_text = re.sub(r'-\s*Target Connection:\s*`?[^`\n]+`?', f'- Target Connection: `{entry}`', doc_text)
                                    with open(doc_rel, 'w', encoding='utf-8') as rf:
                                        rf.write(doc_text)
                                except Exception:
                                    pass

                        # Update solver/solve.py if URL or host/port pattern found
                        solve_path = os.path.join(root, 'solver', 'solve.py')
                        if os.path.exists(solve_path) and entry:
                            try:
                                with open(solve_path, 'r', encoding='utf-8') as sf:
                                    solve_text = sf.read()
                                
                                if entry.startswith('http'):
                                    solve_text = re.sub(r'TARGET_URL\s*=\s*["\'][^"\']+["\']', f'TARGET_URL = "{entry}"', solve_text)
                                    solve_text = re.sub(r'default=["\']https?://[^"\']+["\']', f'default="{entry}"', solve_text)
                                elif ':' in entry and not entry.startswith('http'):
                                    h, p = entry.split(':')
                                    solve_text = re.sub(r'HOST\s*=\s*["\'][^"\']+["\']', f'HOST = "{h}"', solve_text)
                                    solve_text = re.sub(r'PORT\s*=\s*\d+', f'PORT = {p}', solve_text)

                                with open(solve_path, 'w', encoding='utf-8') as sf:
                                    sf.write(solve_text)
                            except Exception:
                                pass
                        break
                except Exception as e:
                    Logger.warning(f'Could not update metadata: {e}')

        # 2. Update top-level challenges.json if present
        json_path = os.path.join(self.workspace_path, 'challenges.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                challs = data if isinstance(data, list) else data.get('challenges', [])
                for c in challs:
                    if str(c.get('id')) == str(challenge_id):
                        if entry:
                            c['connection_info'] = entry
                        if 'instance_info' not in c or not isinstance(c['instance_info'], dict):
                            c['instance_info'] = {}
                        c['instance_info']['status'] = status
                        if entry:
                            c['instance_info']['active_instance'] = entry
                            c['instance_info']['remaining_time'] = time_left
                        break
                
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(data, f, indent=2, ensure_ascii=False)
            except Exception:
                pass
