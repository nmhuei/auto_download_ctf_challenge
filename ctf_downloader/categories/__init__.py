"""Extensible JSON-defined CTF category architecture."""

from .models import CategoryDefinition
from .registry import CategoryRegistry

__all__ = ["CategoryDefinition", "CategoryRegistry"]
