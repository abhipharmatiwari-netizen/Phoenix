"""
Root package for the multi-tenant trading hub service.
Keeps a light import surface and re-exports settings for quick access.
"""

# Light-weight re-export for settings without importing heavy modules at import time.
from app.config.settings import get_settings

__all__ = ["get_settings"]
