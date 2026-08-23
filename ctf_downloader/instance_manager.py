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
            Logger.success(f'Container instance active for [bold cyan]{name}[/bold cyan]!')
            if entry:
                Logger.info(f'Entry Point: [bold green]{entry}[/bold green]')
                if ':' in str(entry) and not str(entry).startswith('http'):
                    h, p = str(entry).split(':')
                    Logger.info(f'Netcat command: [bold yellow]nc {h} {p}[/bold yellow]')
            if time_left:
                Logger.info(f'Remaining Lifetime: [bold magenta]{time_left}[/bold magenta]')

            self._update_local_instance_info(challenge_id, entry, time_left)
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
        else:
            Logger.error(f'Failed to extend container: {msg}')
        return success, msg

    def get_status(self, challenge_id: Any) -> Dict[str, Any]:
        return self.platform.get_instance_status(challenge_id)

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
                        
                        is_cont = inst.get('is_container') or m.get('type') == 'DynamicContainer' or raw.get('type') == 'DynamicContainer' or 'container' in [str(t).lower() for t in tags]
                        
                        if is_cont:
                            m['_local_path'] = root
                            results.append(m)
                except Exception:
                    pass
        return results

    def _update_local_instance_info(self, challenge_id: Any, entry: Optional[str], time_left: Any):
        if not entry:
            return
        for root, _, files in os.walk(self.workspace_path):
            if 'metadata.json' in files:
                try:
                    meta_path = os.path.join(root, 'metadata.json')
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        m = json.load(f)
                    if str(m.get('id')) == str(challenge_id):
                        if 'instance_info' not in m:
                            m['instance_info'] = {}
                        m['instance_info']['last_entry'] = entry
                        m['instance_info']['last_active'] = str(time_left)
                        with open(meta_path, 'w', encoding='utf-8') as f:
                            json.dump(m, f, indent=2, ensure_ascii=False)
                        break
                except Exception:
                    pass
