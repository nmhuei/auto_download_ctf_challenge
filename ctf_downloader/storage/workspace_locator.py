"""Resolve a human-friendly event reference to one local CTF workspace."""
from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Optional

from .global_config import resolve_workspace_root


class WorkspaceReferenceError(ValueError):
    """Base error for a workspace reference that cannot be resolved safely."""


class WorkspaceReferenceNotFound(WorkspaceReferenceError):
    """No workspace under the configured root matches the reference."""


class WorkspaceReferenceAmbiguous(WorkspaceReferenceError):
    """More than one workspace matches; caller must not pick arbitrarily."""


def _event_key(value: object) -> str:
    """Canonicalize event names while preserving an exact-match-only policy."""
    normalized = unicodedata.normalize("NFKD", str(value or "").casefold())
    return "".join(character for character in normalized if character.isalnum())


def _event_title(workspace: Path) -> Optional[str]:
    try:
        data = json.loads((workspace / "challenges.json").read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    info = data.get("ctf_info") if isinstance(data, dict) else None
    title = info.get("title") if isinstance(info, dict) else None
    return str(title) if title else None


def _one_or_raise(matches: list[Path], reference: str, root: Path) -> str:
    if len(matches) == 1:
        return str(matches[0].resolve())
    if len(matches) > 1:
        options = ", ".join(str(path) for path in matches)
        raise WorkspaceReferenceAmbiguous(
            f"Tên workspace/event '{reference}' khớp nhiều workspace: {options}. "
            "Dùng -w <đường-dẫn> để chọn rõ ràng."
        )
    raise WorkspaceReferenceNotFound(
        f"Không tìm thấy workspace/event '{reference}' trong {root}. "
        "Dùng `ctf workspaces` để xem danh sách hoặc -w <đường-dẫn>."
    )


def resolve_workspace_reference(reference: str, *, workspace_root: Optional[Path | str] = None) -> str:
    """Find an event below workspace root by directory name, then CTF title.

    This intentionally accepts exact canonical matches only.  It never uses a
    fuzzy match and never selects one match from an ambiguous set.
    """
    raw_reference = str(reference or "").strip()
    if not raw_reference:
        raise WorkspaceReferenceNotFound("Tên workspace/event không được để trống.")

    direct_path = Path(raw_reference).expanduser()
    if direct_path.is_dir():
        return str(direct_path.resolve())

    root = Path(workspace_root or resolve_workspace_root()).expanduser().resolve()
    if not root.is_dir():
        raise WorkspaceReferenceNotFound(
            f"Workspace root không tồn tại: {root}. Dùng `ctf config workspace-root` để kiểm tra."
        )

    key = _event_key(raw_reference)
    workspaces = [path for path in sorted(root.iterdir()) if path.is_dir()]

    directory_matches = [path for path in workspaces if _event_key(path.name) == key]
    if directory_matches:
        return _one_or_raise(directory_matches, raw_reference, root)

    title_matches = [path for path in workspaces if _event_key(_event_title(path)) == key]
    return _one_or_raise(title_matches, raw_reference, root)
