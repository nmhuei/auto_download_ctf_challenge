"""Hằng dùng chung cho link Mega (megatools) — tầng trung tính.

Cả ``extractors.link_extractor`` (classify downloadable khi có tool) lẫn
``downloaders.mega`` (shell-out tải file) đều cần danh sách binary megatools.
Đặt ở utils để hai tầng cùng import xuống mà không layer nào phải import
ngược layer kia (giữ ràng buộc kiến trúc R4: extractors không import
downloaders).
"""

# Không tự implement crypto Mega — shell-out sang megatools.
MEGA_TOOL_CANDIDATES = ("megadl", "mega-get")
