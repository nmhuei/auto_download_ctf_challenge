"""Facade tương thích — toàn bộ logic quản lý container nằm ở
services.instance_service.InstanceService."""

from .services.instance_service import InstanceService

__all__ = ["InstanceManager", "InstanceService"]


class InstanceManager(InstanceService):
    """Facade mỏng giữ nguyên constructor + method công khai của bản cũ."""
