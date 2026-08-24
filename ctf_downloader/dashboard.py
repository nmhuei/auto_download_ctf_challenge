import os
from typing import Any, Dict, List, Optional

from .storage.workspace_repo import WorkspaceRepo
from .services.status_service import StatusService


class CTFDashboard:
    """Facade mỏng — delegate vào ``services.status_service.StatusService``."""

    def __init__(self, workspace_path: str):
        self.workspace_path = os.path.abspath(workspace_path)
        self.repo = WorkspaceRepo(self.workspace_path)
        self.challenges_data = self._load_challenges_data()
        self.local_challenges = self._scan_local_challenges()

    def _load_challenges_data(self) -> Dict[str, Any]:
        return self.repo.read_challenges()

    def _scan_local_challenges(self) -> List[Dict[str, Any]]:
        return StatusService.scan_local_challenges(self.repo)

    def get_summary_stats(self) -> Dict[str, Any]:
        return StatusService.summary_stats(self.repo, challenges=self.local_challenges)

    def render_tree(self, filter_cat: Optional[List[str]] = None, only_unsolved: bool = False, only_solved: bool = False, only_container: bool = False):
        StatusService.render_tree(
            self.repo,
            stats=self.get_summary_stats(),
            filter_cat=filter_cat,
            only_unsolved=only_unsolved,
            only_solved=only_solved,
            only_container=only_container,
        )
