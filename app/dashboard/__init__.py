"""FastAPI dashboard/admin surfaces (§8)."""

# Dashboard and admin API surfaces for the trading hub.
# Exports routers and FastAPI helpers for UI endpoints.
from fastapi import APIRouter

from app.dashboard import admin_routes, tenant_routes

__all__ = ["admin_routes", "tenant_routes", "APIRouter"]
