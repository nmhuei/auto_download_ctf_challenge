"""Shared constants for ctf_downloader.

Các giá trị dưới đây được copy NGUYÊN VĂN từ các literal đang tồn tại
(see task-2 brief): dashboard.py (SOLVED_MARKERS_DONE), workspace_builder.py
(TARGET_CONNECTION_FMT, FLAG_PLACEHOLDER), summary_generator.py
(SUMMARY_FILES_LINE), ranking.py (LIVE_RANK_PREFIX).
"""

# --- Solved-state markers (nguồn: dashboard.py:33-41) ---
SOLVED_DONE = "- [x] Solved"
SOLVED_TODO = "- [ ] Solved"
SOLVED_EMOJI_DONE = "✅ Solved"
SOLVED_MARKERS_DONE = ("- [x] Solved", "- [X] Solved", SOLVED_EMOJI_DONE, "Status: ✅")

# --- Workspace / summary templates ---
# Nguồn: workspace_builder.py:316 — "- Target Connection: `{info}`"
TARGET_CONNECTION_FMT = "- Target Connection: `{info}`"
# Nguồn: summary_generator.py:47
SUMMARY_FILES_LINE_PREFIX = "- **Total Files Downloaded**:"
SUMMARY_FILES_LINE = "- **Total Files Downloaded**: {total_files}\n"
# Nguồn: ranking.py:186-189
LIVE_RANK_PREFIX = "- **Live Rank**:"

# --- Solve script / workspace defaults ---
SOLVE_VAR_NAMES = ("HOST", "PORT", "TARGET_URL")
# Nguồn: workspace_builder.py:197 / :336
FLAG_PLACEHOLDER = "FLAG{...}"

DEFAULT_CATEGORY = "Misc"

# --- Challenge Status Model đa chiều (schema v2) ---
# Nguồn: docs/superpowers/specs/2026-08-24-challenge-status-model-design.md
STATUS_SCHEMA_VERSION = 2

SOLVE_STATES = ("unsolved", "working", "solved_by_me", "solved_by_team", "solved_other")
FLAG_STATES = ("none", "found_unverified", "hoarded", "submitted_correct", "submitted_wrong")
WRITEUP_STATES = ("none", "skeleton", "draft", "complete")
CONTAINER_STATES = ("none", "running", "stopped")

# Thứ bậc trục solve cho nguyên tắc "chỉ nâng không hạ":
# sync/pull KHÔNG BAO GIỜ hạ trạng thái local cao hơn.
SOLVE_RANK = {
    "unsolved": 0,
    "working": 1,
    "solved_other": 2,
    "solved_by_team": 3,
    "solved_by_me": 4,
}

# Mapping signal -> icon (design system spec §6). MỌI render phải lấy icon
# từ đây — không text trần, không icon inline rải rác.
STATUS_ICONS = {
    "solve": {
        "unsolved": "·",
        "working": "🛠️",
        "solved_by_me": "🧑✅",
        "solved_by_team": "👥✅",
        "solved_other": "🌐✅",
    },
    "flag": {
        "none": "∅",
        "found_unverified": "❓",
        "hoarded": "🏴",
        "submitted_correct": "🚩✔",
        "submitted_wrong": "⛔",
    },
    "writeup": {
        "none": "-",
        "skeleton": "📄",
        "draft": "📝",
        "complete": "📚",
    },
    "container": {
        "running": "🐳▶",
        "stopped": "🐳⏸",
    },
}

CATEGORY_ICONS = {
    "web": "🌐",
    "pwn": "🖥️",
    "crypto": "🔐",
    "reverse": "🔬",
    "forensics": "🔍",
    "misc": "🎲",
    "blockchain": "⛓️",
}

__all__ = [
    "SOLVED_DONE",
    "SOLVED_TODO",
    "SOLVED_EMOJI_DONE",
    "SOLVED_MARKERS_DONE",
    "TARGET_CONNECTION_FMT",
    "SUMMARY_FILES_LINE_PREFIX",
    "SUMMARY_FILES_LINE",
    "LIVE_RANK_PREFIX",
    "SOLVE_VAR_NAMES",
    "FLAG_PLACEHOLDER",
    "DEFAULT_CATEGORY",
    "STATUS_SCHEMA_VERSION",
    "SOLVE_STATES",
    "FLAG_STATES",
    "WRITEUP_STATES",
    "CONTAINER_STATES",
    "SOLVE_RANK",
    "STATUS_ICONS",
    "CATEGORY_ICONS",
]
