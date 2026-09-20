"""Tactical Sub-Hub Controllers for Modern CTF Cockpit.

Decoupled modular sub-menus for Workspace management, Challenge operations,
Flag submission lab, and System arsenal.
Strictly adheres to the semantic theme palette with zero hardcoded styling.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.padding import Padding
from rich.cells import cell_len
from rich import box

from .theme import (
    ACCENT,
    ACCENT_DEEP,
    FG_BASE,
    FG_FAINT,
    FG_MUTED,
    INFO,
    SUCCESS,
    WARN,
    ERROR,
    load_theme,
)
from ..utils.logger import Logger


def _hub_console() -> Console:
    """Return an interactive console configured with active theme."""
    try:
        from ..interactive_menu import _menu_console
        return _menu_console()
    except Exception:
        return Console(stderr=True, theme=load_theme(None))


def _prompt(prompt_text: str = "❯ Select action: ") -> str:
    """Read a single line of input from console."""
    try:
        con = _hub_console()
        return con.input(prompt_text).strip()
    except (EOFError, KeyboardInterrupt):
        return "0"
    except Exception:
        try:
            return input(prompt_text).strip()
        except (EOFError, KeyboardInterrupt):
            return "0"


def _pause() -> None:
    """Pause execution until user presses Enter."""
    con = _hub_console()
    try:
        con.input(Text("Press Enter to continue...", style=FG_FAINT))
    except (EOFError, KeyboardInterrupt):
        pass


def render_hub_menu(
    title: str,
    actions: List[Tuple[str, str]],
    prompt_text: str = "❯ Select action: ",
    subtitle: Optional[str] = None,
) -> str:
    """Render a clean, modern tactical sub-hub menu and return user selection."""
    con = _hub_console()
    con.print()

    # Section header banner
    header = Text()
    header.append(" ◈ ", style=f"bold {ACCENT}")
    header.append(title.upper(), style=f"bold {FG_BASE}")
    if subtitle:
        header.append(f" · {subtitle}", style=FG_MUTED)

    con.print(
        Panel(
            header,
            box=box.HORIZONTALS,
            border_style=ACCENT_DEEP,
            padding=(0, 1),
        )
    )

    # Action list (dynamic cell_len alignment, zero manual spacing)
    split_actions = []
    for key, desc in actions:
        if " (" in desc and desc.endswith(")"):
            main_part, hint_part = desc.split(" (", 1)
            split_actions.append((key, main_part, f"({hint_part}"))
        else:
            split_actions.append((key, desc, ""))

    grid = Table.grid(padding=(0, 1))
    grid.add_column("key", justify="right", no_wrap=True)
    grid.add_column("main")
    grid.add_column("hint")

    for key, main_part, hint in split_actions:
        key_style = f"bold {FG_FAINT}" if key == "0" else f"bold {ACCENT}"
        main_style = FG_MUTED if key == "0" else FG_BASE
        grid.add_row(
            Text(f"[{key}]", style=key_style),
            Text(main_part, style=main_style),
            Text(hint, style=FG_MUTED) if hint else Text(""),
        )
    con.print(Padding(grid, (0, 2)))

    con.print()
    return _prompt(prompt_text)


def hub_workspace_targets(app: Any) -> None:
    """Hub 1: Workspace selection, cloning, credentials, and platform diagnostics."""
    actions = [
        ("1", "Select / Switch active competition workspace"),
        ("2", "Clone / Download new CTF challenge files"),
        ("3", "Configure & save Cookie / Token for this event"),
        ("4", "Platform Doctor & connectivity verification"),
        ("5", "Platform Schemas & Auto-Recon (Dò tìm & Quản lý Platform)"),
        ("0", "Back to Main Cockpit"),
    ]

    while True:
        choice = render_hub_menu(
            title="Workspace & Targets",
            subtitle="Chiến trường & Nền tảng",
            actions=actions,
        )
        if choice in ("0", ""):
            break
        elif choice == "1":
            if hasattr(app, "_menu_select_workspace"):
                app._menu_select_workspace()
        elif choice == "2":
            if hasattr(app, "_menu_download_new"):
                app._menu_download_new()
        elif choice == "3":
            if hasattr(app, "_menu_config_credentials"):
                app._menu_config_credentials()
        elif choice == "4":
            if hasattr(app, "_menu_platform_doctor"):
                app._menu_platform_doctor()
            else:
                _run_doctor_fallback(app)
        elif choice == "5":
            _menu_platform_manager(app)


def _menu_platform_manager(app: Any) -> None:
    """Sub-menu for Platform Schemas & Auto-Recon."""
    actions = [
        ("1", "List all registered platforms & schemas"),
        ("2", "Auto-Recon: Probe new CTF platform URL"),
        ("3", "View details of a platform schema"),
        ("4", "Remove custom platform schema"),
        ("0", "Back"),
    ]
    ws = getattr(app, "workspace_path", None)

    while True:
        choice = render_hub_menu(
            title="Platform Schemas & Auto-Recon",
            subtitle="Dò tìm & Quản lý Cấu trúc Nền tảng",
            actions=actions,
        )
        if choice in ("0", ""):
            break
        elif choice == "1":
            from ..cli_commands import handle_platform
            args = type("Args", (), {"platform_action": "list", "workspace": ws})()
            handle_platform(args)
            _pause()
        elif choice == "2":
            target_url = _prompt("Nhập URL trang CTF cần dò tìm (vd: https://ctf.example.com): ")
            if target_url and target_url != "0":
                from ..cli_commands import handle_platform
                save_choice = _prompt("Tự động lưu schema nếu tìm thấy? [y/N]: ").lower()
                should_save = save_choice in ("y", "yes")
                scope = "workspace" if ws else "global"
                if should_save and ws:
                    sc_in = _prompt("Lưu vào workspace hiện tại hay global? [w/g, mặc định: w]: ").lower()
                    if sc_in == "g":
                        scope = "global"
                args = type("Args", (), {
                    "platform_action": "probe",
                    "url": target_url,
                    "save": should_save,
                    "scope": scope,
                    "key": None,
                    "label": None,
                    "workspace": ws,
                })()
                handle_platform(args)
            _pause()
        elif choice == "3":
            key = _prompt("Nhập platform key cần xem (vd: metactf, ctfd): ")
            if key and key != "0":
                from ..cli_commands import handle_platform
                args = type("Args", (), {"platform_action": "show", "target": key, "workspace": ws})()
                handle_platform(args)
            _pause()
        elif choice == "4":
            key = _prompt("Nhập platform key cần xoá: ")
            if key and key != "0":
                from ..cli_commands import handle_platform
                scope = "workspace" if ws else "global"
                if ws:
                    sc_in = _prompt("Xoá khỏi workspace hay global? [w/g, mặc định: w]: ").lower()
                    if sc_in == "g":
                        scope = "global"
                args = type("Args", (), {
                    "platform_action": "remove",
                    "target": key,
                    "scope": scope,
                    "workspace": ws,
                })()
                handle_platform(args)
            _pause()


def _run_doctor_fallback(app: Any) -> None:
    """Run doctor checkup when app doesn't have a dedicated method."""
    from ..services.health_service import HealthService
    con = _hub_console()
    con.print(Text("\n  Checking platform health...", style=INFO))
    try:
        HealthService.check_workspace(app.workspace_path)
    except Exception as e:
        Logger.error(f"Doctor failed: {e}")
    _pause()


def challenge_action_card(app: Any, target: Dict[str, Any]) -> None:
    """Unified Action Card for a specific challenge: view, instance, flag, solver."""
    con = _hub_console()
    cid = target.get("id")
    cname = target.get("name") or "Unknown"
    cat = target.get("category") or "General"
    pts = target.get("points") or 0

    actions = [
        ("1", "View Description, Hints & Attachments"),
        ("2", "Dynamic Instance: Start / Stop / Extend container"),
        ("3", "Submit Flag for this challenge"),
        ("4", "Dispatch SuperBQA AI Solver (Tự động giải bài)"),
        ("0", "Back to Challenge List"),
    ]

    while True:
        con.print()
        card_table = Table(box=None, show_header=False, pad_edge=False, padding=(0, 1))
        card_table.add_column("Field", style=FG_MUTED, no_wrap=True)
        card_table.add_column("Value")
        card_table.add_row("Challenge", Text(str(cname), style=f"bold {FG_BASE}"))
        card_table.add_row("Metadata", Text(f"ID: {cid}  ·  Category: {cat}  ·  Points: {pts}", style=FG_MUTED))
        if target.get("connection_info"):
            card_table.add_row("Connection", Text(str(target["connection_info"]), style=INFO))
        if target.get("solved_by_me"):
            card_table.add_row("Status", Text("✔ SOLVED", style="solved"))
        else:
            card_table.add_row("Status", Text("· Unsolved", style=FG_FAINT))

        con.print(
            Panel(
                card_table,
                title=Text(" ACTION CARD ", style=f"bold {ACCENT}"),
                box=box.ROUNDED,
                border_style=ACCENT_DEEP,
                padding=(0, 1),
            )
        )

        choice = render_hub_menu(
            title=f"Tactical Action: {cname}",
            actions=actions,
            prompt_text="❯ Select card action (0-4): ",
        )

        if choice in ("0", ""):
            break
        elif choice == "1":
            if hasattr(app, "_view_challenge_detail_for"):
                app._view_challenge_detail_for(target)
            elif hasattr(app, "_show_challenge_detail"):
                app._show_challenge_detail(target)
        elif choice == "2":
            if hasattr(app, "_run_container_action_for_id"):
                app._run_container_action_for_id(cid, cname)
        elif choice == "3":
            if hasattr(app, "_submit_flag_for_target"):
                app._submit_flag_for_target(target)
        elif choice == "4":
            if hasattr(app, "_launch_solver_for_target"):
                app._launch_solver_for_target(target)
            else:
                _launch_single_solver(app, cid)


def _launch_single_solver(app: Any, cid: Any) -> None:
    """Launch SuperBQA solver for single target challenge."""
    from ..services.solver_service import SolverService
    con = _hub_console()
    try:
        svc = SolverService(app.workspace_path)
        res = svc.spawn_background(str(cid), workers=1, per_category=False)
        if res.get("success"):
            Logger.success(f"SuperBQA worker launched for challenge ID {cid}.")
        else:
            Logger.warning(f"Solver notice: {res.get('message')}")
    except Exception as e:
        Logger.error(f"Failed to launch solver: {e}")
    _pause()


def hub_challenge_operations(app: Any) -> None:
    """Hub 2: Challenge explorer, action card, and container fleet manager."""
    actions = [
        ("1", "View challenge tree & progress (Tree View)"),
        ("2", "Open Challenge Command Card (Select challenge)"),
        ("3", "Manage dynamic container / instance (start / stop / renew)"),
        ("0", "Back to Main Cockpit"),
    ]

    while True:
        choice = render_hub_menu(
            title="Challenge Operations",
            subtitle="Tác chiến Challenge & Instance",
            actions=actions,
        )
        if choice in ("0", ""):
            break
        elif choice == "1":
            if hasattr(app, "_menu_view_tree"):
                app._menu_view_tree()
        elif choice == "2":
            if hasattr(app, "_menu_select_and_open_card"):
                app._menu_select_and_open_card()
            elif hasattr(app, "_menu_view_challenge_detail"):
                app._menu_view_challenge_detail()
        elif choice == "3":
            if hasattr(app, "_menu_manage_instances"):
                app._menu_manage_instances()


def hub_flag_submission(app: Any) -> None:
    """Hub 3: Flag submission lab, hoarded flag harvesting, and submit history."""
    actions = [
        ("1", "Submit flag for a specific challenge"),
        ("2", "Auto scan & submit hoarded flags in workspace"),
        ("3", "View submit history & hoarded flag vault"),
        ("0", "Back to Main Cockpit"),
    ]

    while True:
        choice = render_hub_menu(
            title="Flag Submission Lab",
            subtitle="Trung tâm Nộp Flag & Kho Chiến lợi phẩm",
            actions=actions,
        )
        if choice in ("0", ""):
            break
        elif choice == "1":
            if hasattr(app, "_menu_submit_flag"):
                app._menu_submit_flag()
        elif choice == "2":
            if hasattr(app, "_menu_auto_submit"):
                app._menu_auto_submit()
        elif choice == "3":
            if hasattr(app, "_menu_flag_history"):
                app._menu_flag_history()
            else:
                _show_flag_vault(app)


def _show_flag_vault(app: Any) -> None:
    """Display hoarded flags and submit history in workspace."""
    from ..services.submit_service import SubmitService
    con = _hub_console()
    try:
        sub = SubmitService(app.workspace_path)
        sub.render_history(all_entries=True)
    except Exception as e:
        Logger.error(f"Cannot load flag history: {e}")
    _pause()


def hub_system_arsenal(app: Any) -> None:
    """Hub 5: Git synchronization, visual themes, and global settings."""
    actions = [
        ("1", "Git Sync & Remote Backup (Kho vũ khí GitHub)"),
        ("2", "Switch Visual Theme (Cyberpunk / Matrix / Amber / Nord)"),
        ("3", "Global Configuration (Default workspace root / auto-sync)"),
        ("0", "Back to Main Cockpit"),
    ]

    while True:
        choice = render_hub_menu(
            title="System & Arsenal",
            subtitle="Hệ thống & Kho Vũ Khí",
            actions=actions,
        )
        if choice in ("0", ""):
            break
        elif choice == "1":
            if hasattr(app, "_menu_git"):
                app._menu_git()
        elif choice == "2":
            if hasattr(app, "_menu_switch_theme"):
                app._menu_switch_theme()
        elif choice == "3":
            if hasattr(app, "_menu_global_config"):
                app._menu_global_config()
            else:
                _show_config_summary(app)


def _show_config_summary(app: Any) -> None:
    """Display current global configurations."""
    from ..storage.global_config import load_global_config
    con = _hub_console()
    cfg = load_global_config()
    con.print(Text("\n  Global Configuration:", style=f"bold {FG_BASE}"))
    for k, v in cfg.items():
        con.print(Text(f"    • {k}: ", style=ACCENT) + Text(str(v), style=FG_MUTED))
    _pause()
