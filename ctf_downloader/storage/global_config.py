"""Persistent global configuration (~/.config/ctf_toolkit/config.json).

Chuyển nguyên văn từ ctf_downloader/interactive_menu.py (Phase 3 rebuild);
interactive_menu re-export lại để giữ tương thích.
"""
import os
import json
from typing import Dict, Any

from ..utils.logger import Logger

CONFIG_DIR = os.path.expanduser('~/.config/ctf_toolkit')
GLOBAL_CONFIG_FILE = os.path.join(CONFIG_DIR, 'config.json')

def load_global_config() -> Dict[str, Any]:
    if os.path.exists(GLOBAL_CONFIG_FILE):
        try:
            with open(GLOBAL_CONFIG_FILE, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'workspaces': {}, 'default_workspace': None, 'auth': {}}

def save_global_config(cfg: Dict[str, Any]):
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        with open(GLOBAL_CONFIG_FILE, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
    except Exception as e:
        Logger.warning(f'Could not save config: {e}')
