"""
SP3 — Recon & capability map.

Định nghĩa `PlatformInfo`: bản đồ thông tin + năng lực (capabilities) của một
nền tảng CTF, do `detector.PlatformDetector.detect_platform_info()` điền dữ liệu.
Object này có thể serialize (`to_dict()`) vào metadata/challenges.json sau này.
"""

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional

from .registry import PLATFORMS

# Các giá trị hợp lệ của platform_type — sinh tự từ registry (giữ legacy "unknown")
PLATFORM_TYPES = ("unknown", *sorted(PLATFORMS))


@dataclass
class PlatformInfo:
    """
    Thông tin recon của một nền tảng CTF.

    Attributes:
        platform_type: gzctf | ctfd | rctf | custom_rest | generic_html | unknown
        base_url:      URL gốc đã chuẩn hoá (đã lược bỏ suffix /challenges...)
        confidence:    độ tin cậy của kết quả nhận diện: high | medium | low
        signals:       danh sách dấu hiệu khớp (phục vụ log/debug)
        capabilities:  năng lực của nền tảng:
            - container:          hỗ trợ spawn instance/container động (whale, GZCTF...)
            - scoreboard:         có bảng xếp hạng
            - rules_via_api:      luật lệ lấy được qua API (vd /api/config.Rules của GZCTF)
            - api_encryption:     API response mã hoá bằng public key (GZCTF ApiPublicKey);
                                  None = chưa biết/không áp dụng
            - port_mapping_proxy: GZCTF PortMapping == "PlatformProxy" (port qua proxy platform)
        game_id:       ID game giải đấu (GZCTF: parse từ URL /games/<digits>)
        version_hints: thông tin phụ tuỳ ý (tên giải, fork whale, ...)
    """

    platform_type: str = "unknown"
    base_url: str = ""
    confidence: str = "low"  # high | medium | low
    signals: List[str] = field(default_factory=list)
    capabilities: Dict[str, Any] = field(default_factory=lambda: {
        "container": False,
        "scoreboard": True,
        "rules_via_api": False,
        "api_encryption": None,       # Optional[bool]
        "port_mapping_proxy": False,
    })
    game_id: Optional[int] = None
    version_hints: Dict[str, Any] = field(default_factory=dict)

    def add_signal(self, signal: str) -> None:
        """Ghi nhận một dấu hiệu khớp/không khớp (tránh ghi trùng)."""
        if signal not in self.signals:
            self.signals.append(signal)

    def to_dict(self) -> Dict[str, Any]:
        """Serialize đầy đủ để nhúng vào metadata/challenges.json."""
        return asdict(self)
