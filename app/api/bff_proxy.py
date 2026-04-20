from __future__ import annotations

import logging
from typing import Any

import httpx
from fastapi import APIRouter, Request, Response

from app.config.settings import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/bff", tags=["bff"])


def _backend_url(path: str) -> str:
    settings = get_settings()
    base_url = str(settings.backend_url or "http://localhost:8080").rstrip("/")
    normalized_path = str(path or "").lstrip("/")
    return f"{base_url}/{normalized_path}"


def _proxy_headers(
    request: Request,
    *,
    include_json_content_type: bool = False,
) -> dict[str, str]:
    headers: dict[str, str] = {}

    for name in (
        "Authorization",
        "X-Admin-Key",
        "X-Tenant-Id",
        "X-Request-Id",
        "X-Correlation-Id",
    ):
        value = str(request.headers.get(name) or "").strip()
        if value:
            headers[name] = value

    if include_json_content_type:
        headers["Content-Type"] = "application/json"

    return headers


@router.post("/{path:path}")
async def proxy_post(request: Request, path: str):
    """A simple BFF proxy for POST requests."""
    headers = _proxy_headers(request, include_json_content_type=True)
    body = await request.json()
    url = _backend_url(path)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.post(
                url,
                json=body,
                headers=headers,
                params=request.query_params,
            )
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return Response(content=e.response.text, status_code=e.response.status_code)
        except Exception as e:
            logger.error("Error proxying POST to %s: %s: %s", path, type(e).__name__, e)
            return Response(status_code=500, content="Internal server error")

@router.get("/{path:path}")
async def proxy_get(request: Request, path: str):
    """A simple BFF proxy for GET requests."""
    headers = _proxy_headers(request)
    url = _backend_url(path)

    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, headers=headers, params=request.query_params)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as e:
            return Response(content=e.response.text, status_code=e.response.status_code)
        except Exception as e:
            logger.error("Error proxying GET to %s: %s: %s", path, type(e).__name__, e)
            return Response(status_code=500, content="Internal server error")
