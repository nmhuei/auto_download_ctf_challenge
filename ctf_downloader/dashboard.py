import os
import json
import re
from typing import Dict, Any, List, Optional
from .utils.logger import Logger

class CTFDashboard:
    def __init__(self, workspace_path: str):
        self.workspace_path = os.path.abspath(workspace_path)
        self.challenges_data = self._load_challenges_data()
        self.local_challenges = self._scan_local_challenges()

    def _load_challenges_data(self) -> Dict[str, Any]:
        json_path = os.path.join(self.workspace_path, 'challenges.json')
        if os.path.exists(json_path):
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}

    def _scan_local_challenges(self) -> List[Dict[str, Any]]:
        results = []
        for root, _, files in os.walk(self.workspace_path):
            if 'metadata.json' in files:
                try:
                    with open(os.path.join(root, 'metadata.json'), 'r', encoding='utf-8') as f:
                        m = json.load(f)
                    
                    # Check solved state from writeup/README.md, README.md, or metadata
                    is_solved = bool(m.get('solved_by_me', False))
                    for rp in [os.path.join(root, 'writeup', 'README.md'), os.path.join(root, 'README.md')]:
                        if os.path.exists(rp):
                            with open(rp, 'r', encoding='utf-8') as rf:
                                rtxt = rf.read()
                                if '- [x] Solved' in rtxt or '- [X] Solved' in rtxt or '✅ Solved' in rtxt or 'Status: ✅' in rtxt:
                                    is_solved = True
                                    break
                                elif not is_solved and '- [ ] Solved' in rtxt:
                                    is_solved = False

                    m['solved_by_me'] = is_solved
                    m['_folder'] = root
                    m['_rel_folder'] = os.path.relpath(root, self.workspace_path)
                    
                    # Count local files in challenge/ subdirectory or root
                    c_subdir = os.path.join(root, 'challenge')
                    if os.path.isdir(c_subdir):
                        local_files = [f for f in os.listdir(c_subdir) if f not in ['__pycache__', '.git']]
                    else:
                        local_files = [f for f in os.listdir(root) if f not in ['metadata.json', 'README.md', 'solve.py', 'challenge', 'solver', 'writeup', 'script', 'scripts', '__pycache__']]
                    m['_local_files_count'] = len(local_files)

                    results.append(m)
                except Exception:
                    pass
        return results

    def get_summary_stats(self) -> Dict[str, Any]:
        challs = self.local_challenges
        total_challs = len(challs)
        solved_challs = sum(1 for c in challs if c.get('solved_by_me'))
        total_points = sum((c.get('points') or 0) for c in challs)
        earned_points = sum((c.get('points') or 0) for c in challs if c.get('solved_by_me'))

        by_cat = {}
        for c in challs:
            cat = c.get('category', 'Misc')
            pts = c.get('points') or 0
            if cat not in by_cat:
                by_cat[cat] = {'total': 0, 'solved': 0, 'points': 0, 'earned': 0, 'challenges': []}
            by_cat[cat]['total'] += 1
            by_cat[cat]['points'] += pts
            if c.get('solved_by_me'):
                by_cat[cat]['solved'] += 1
                by_cat[cat]['earned'] += pts
            by_cat[cat]['challenges'].append(c)


        ctf_info = self.challenges_data.get('ctf_info', {}) if isinstance(self.challenges_data, dict) else {}
        return {
            'title': ctf_info.get('title') or os.path.basename(self.workspace_path),
            'url': ctf_info.get('url', ''),
            'platform': ctf_info.get('platform', 'generic'),
            'user': ctf_info.get('user', ''),
            'team': ctf_info.get('team', ''),
            'total_challenges': total_challs,
            'solved_challenges': solved_challs,
            'unsolved_challenges': total_challs - solved_challs,
            'total_points': total_points,
            'earned_points': earned_points,
            'completion_rate': (solved_challs / total_challs * 100) if total_challs > 0 else 0,
            'categories': by_cat
        }

    def render_tree(self, filter_cat: Optional[List[str]] = None, only_unsolved: bool = False, only_solved: bool = False, only_container: bool = False):
        stats = self.get_summary_stats()
        title = stats['title']
        platform = stats['platform'].upper()
        rate = stats['completion_rate']
        
        print('=' * 85)
        print(f" 🏆 CTF WORKSPACE: {title} [{platform}]")
        if stats['user'] or stats['team']:
            team_str = f" | Team: {stats['team']}" if stats['team'] else ''
            print(f" 👤 User: {stats['user']}{team_str}")
        print(f" 📊 Progress: {stats['solved_challenges']}/{stats['total_challenges']} Solved ({rate:.1f}%) | Points: {stats['earned_points']}/{stats['total_points']}")
        
        bar_len = 30
        filled_len = int(bar_len * rate // 100)
        bar = '█' * filled_len + '░' * (bar_len - filled_len)
        print(f"    [{bar}] {rate:.1f}%")
        print('=' * 85)

        categories = stats['categories']
        for cat, data in sorted(categories.items()):
            if filter_cat and cat.lower() not in [c.lower() for c in filter_cat]:
                continue
            
            c_list = data['challenges']
            if only_unsolved:
                c_list = [c for c in c_list if not c.get('solved_by_me')]
            elif only_solved:
                c_list = [c for c in c_list if c.get('solved_by_me')]
            
            if only_container:
                c_list = [c for c in c_list if c.get('instance_info', {}).get('is_container') or c.get('type') == 'DynamicContainer' or c.get('raw', {}).get('type') == 'DynamicContainer']

            if not c_list:
                continue

            c_rate = (data['solved'] / data['total'] * 100) if data['total'] > 0 else 0
            c_bar = '█' * int(10 * c_rate // 100) + '░' * (10 - int(10 * c_rate // 100))
            print(f"\n📁 {cat} ({len(c_list)} challs, {data['points']} pts) [{c_bar}] {data['solved']}/{data['total']}")

            for idx, c in enumerate(c_list):
                is_last = (idx == len(c_list) - 1)
                prefix = '└── ' if is_last else '├── '
                
                status_icon = '✅' if c.get('solved_by_me') else '⏳'
                status_str = 'Solved' if c.get('solved_by_me') else 'Unsolved'
                
                c_id = c.get('id', '?')
                c_name = c.get('name', 'Unknown')
                c_pts = c.get('points', 0)
                solves = c.get('solves_count', c.get('solves', '-'))
                files_count = c.get('_local_files_count', 0)
                
                # Check container tag
                is_cont = c.get('instance_info', {}).get('is_container') or c.get('type') == 'DynamicContainer' or c.get('raw', {}).get('type') == 'DynamicContainer'
                cont_str = ' [🐳 Container]' if is_cont else ''
                files_str = f' [{files_count} file(s)]' if files_count > 0 else ''
                
                print(f"  {prefix}[{status_icon} {status_str:<8}] {c_id:>3}. {c_name:<32} ({c_pts:>4} pts) - {str(solves):>3} solves{cont_str}{files_str}")

        print('\n' + '=' * 85)
