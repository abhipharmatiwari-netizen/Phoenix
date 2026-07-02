from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from http.client import HTTPMessage
from pathlib import Path
from typing import Any


class ProbeError(RuntimeError):
    pass


def _request(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: Any | None = None,
    timeout: float = 10.0,
) -> tuple[int, HTTPMessage, bytes]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    data = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=request_headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers, exc.read()


def _load_admin_key(container: str) -> str | None:
    secret_dir = str(os.getenv("PHX_SECRET_DIR") or "").strip()
    if secret_dir:
        path = Path(secret_dir) / "admin_api_key"
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
        if value:
            return value
    try:
        result = subprocess.run(
            [
                "docker",
                "exec",
                container,
                "python",
                "-c",
                "from pathlib import Path; print(Path('/run/secrets/admin_api_key').read_text().strip())",
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    if value:
        return value
    for env_name in ("PHOENIX_ADMIN_API_KEY", "ADMIN_API_KEY"):
        value = str(os.getenv(env_name) or "").strip()
        if value:
            return value
    return None


def _header(headers: HTTPMessage, name: str) -> str:
    return str(headers.get(name) or "").strip()


def _assert_contains(name: str, actual: str, expected: str) -> None:
    if expected.lower() not in actual.lower():
        raise ProbeError(f"{name}: expected {expected!r} in {actual!r}")


def _assert_no_stack_trace(name: str, raw: bytes) -> None:
    text = raw.decode("utf-8", errors="ignore").lower()
    for marker in ("traceback", "stack trace", "fastapi.exceptions", "starlette", "file \""):
        if marker in text:
            raise ProbeError(f"{name}: response contains stack-trace marker {marker!r}")


def _assert_json_route(base_url: str, path: str, timeout: float) -> None:
    status, headers, raw = _request(base_url, path, timeout=timeout)
    if status != 200:
        raise ProbeError(f"{path}: expected HTTP 200, got {status}")
    content_type = _header(headers, "Content-Type").lower()
    if "application/json" not in content_type:
        raise ProbeError(f"{path}: expected application/json, got {content_type!r}")
    body = raw.decode("utf-8", errors="ignore").lstrip()
    if body.startswith("<!doctype") or body.startswith("<html"):
        raise ProbeError(f"{path}: returned SPA/HTML instead of JSON")
    json.loads(body)
    print(f"PASS {path}: JSON HTTP 200")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only nginx route/security smoke.")
    parser.add_argument("--base-url", default="http://127.0.0.1")
    parser.add_argument("--backend-container", default="phoenix-v9-backend")
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    try:
        status, headers, raw = _request(args.base_url, "/", headers={"Accept": "text/html"}, timeout=args.timeout)
        if status != 200:
            raise ProbeError(f"/: expected HTTP 200, got {status}")
        _assert_contains("HSTS", _header(headers, "Strict-Transport-Security"), "max-age=31536000")
        csp = _header(headers, "Content-Security-Policy")
        _assert_contains("CSP default", csp, "default-src 'self'")
        _assert_contains("CSP scripts", csp, "script-src 'self'")
        _assert_contains("CSP frame", csp, "frame-ancestors 'self'")
        _assert_contains("frame protection", _header(headers, "X-Frame-Options"), "SAMEORIGIN")
        _assert_contains("content type protection", _header(headers, "X-Content-Type-Options"), "nosniff")
        _assert_contains("referrer policy", _header(headers, "Referrer-Policy"), "strict-origin-when-cross-origin")
        _assert_no_stack_trace("/", raw)
        print("PASS /: security headers present")

        admin_key = _load_admin_key(args.backend_container)
        if admin_key:
            status, headers, raw = _request(
                args.base_url,
                "/admin/dashboard/ws-ticket",
                method="POST",
                headers={
                    "X-Admin-Key": admin_key,
                    "X-Forwarded-Proto": "https",
                },
                body={},
                timeout=args.timeout,
            )
            if status != 200:
                raise ProbeError(f"/admin/dashboard/ws-ticket: expected HTTP 200, got {status}")
            set_cookie = _header(headers, "Set-Cookie")
            _assert_contains("dashboard ticket cookie", set_cookie, "HttpOnly")
            _assert_contains("dashboard ticket cookie", set_cookie, "Secure")
            _assert_contains("dashboard ticket cookie", set_cookie, "SameSite=strict")
            _assert_no_stack_trace("/admin/dashboard/ws-ticket", raw)
            print("PASS /admin/dashboard/ws-ticket: secure HttpOnly SameSite cookie")
        else:
            print("SKIP secure cookie runtime check: admin key unavailable")

        status, headers, raw = _request(args.base_url, "/static/__phoenix_missing_asset__.js", timeout=args.timeout)
        if status != 404:
            raise ProbeError(f"stale static asset: expected HTTP 404, got {status}")
        body = raw.decode("utf-8", errors="ignore").lower()
        if 'id="root"' in body or "loading phoenix" in body:
            raise ProbeError("stale static asset returned SPA index body")
        _assert_no_stack_trace("stale static asset", raw)
        print("PASS stale /static/*: HTTP 404 without SPA fallback")

        _assert_json_route(args.base_url, "/health/alerts", args.timeout)
        _assert_json_route(args.base_url, "/health/mitigations", args.timeout)

        status, _headers, raw = _request(args.base_url, "/bff/health/summary", timeout=args.timeout)
        if status != 404:
            raise ProbeError(f"/bff/health/summary: expected HTTP 404, got {status}")
        _assert_no_stack_trace("/bff/health/summary", raw)
        print("PASS blocked BFF diagnostic has no stack trace")
    except (ProbeError, json.JSONDecodeError) as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
