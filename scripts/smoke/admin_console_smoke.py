from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[2]
SECRET_KEY_PATTERN = re.compile(
    r"(secret|password|passwd|token|authorization|cookie|credential|api[_-]?key|private[_-]?key|pin|totp|jwt|session)",
    re.I,
)
ALLOWED_SECRET_SENTINELS = {
    "",
    "***",
    "***redacted***",
    "redacted",
    "configured",
    "missing",
    "present",
    "true",
    "false",
    "none",
    "null",
}


class ProbeError(RuntimeError):
    pass


def _json_bytes(payload: Any) -> bytes:
    return json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _request(
    base_url: str,
    path: str,
    *,
    admin_key: str | None = None,
    method: str = "GET",
    body: Any | None = None,
    timeout: float = 10.0,
) -> tuple[int, str, bytes]:
    url = f"{base_url.rstrip('/')}/{path.lstrip('/')}"
    headers = {"Accept": "application/json"}
    data = None
    if admin_key:
        headers["X-Admin-Key"] = admin_key
    if body is not None:
        data = _json_bytes(body)
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.headers.get("content-type", ""), resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.get("content-type", ""), exc.read()


def _load_admin_key(container: str, explicit_file: str | None) -> str:
    candidates: list[Path] = []
    if explicit_file:
        candidates.append(Path(explicit_file))
    secret_dir = str(os.getenv("PHX_SECRET_DIR") or "").strip()
    if secret_dir:
        candidates.append(Path(secret_dir) / "admin_api_key")

    for path in candidates:
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            continue
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
    except Exception as exc:  # pragma: no cover - environment failure
        raise ProbeError(f"could not read admin secret file from container: {type(exc).__name__}") from exc

    value = result.stdout.strip()
    if value:
        return value

    for env_name in ("PHOENIX_ADMIN_API_KEY", "ADMIN_API_KEY"):
        value = str(os.getenv(env_name) or "").strip()
        if value:
            return value

    raise ProbeError(
        "admin key unavailable: pass --admin-key-file, set PHX_SECRET_DIR, "
        "run the backend container with /run/secrets/admin_api_key, or set PHOENIX_ADMIN_API_KEY/ADMIN_API_KEY"
    )


def _parse_json(status: int, content_type: str, raw: bytes, path: str) -> Any:
    if "application/json" not in content_type.lower():
        raise ProbeError(f"{path} returned status {status} with non-JSON content-type {content_type!r}")
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ProbeError(f"{path} returned invalid JSON") from exc


def _assert_status(name: str, actual: int, expected: int) -> None:
    if actual != expected:
        raise ProbeError(f"{name}: expected HTTP {expected}, got {actual}")
    print(f"PASS {name}: HTTP {actual}")


def _assert_no_sensitive_values(value: Any, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = f"{path}.{key}"
            if SECRET_KEY_PATTERN.search(str(key)):
                text = str(item or "").strip().lower()
                if text not in ALLOWED_SECRET_SENTINELS and not text.startswith("***"):
                    raise ProbeError(f"secret-like field is not redacted at {next_path}")
            _assert_no_sensitive_values(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _assert_no_sensitive_values(item, f"{path}[{index}]")


def _assert_public_health_redacted(payload: Any) -> None:
    if not isinstance(payload, dict):
        raise ProbeError("public /health/summary did not return an object")
    forbidden = {
        "schema",
        "watchdog",
        "per_account_staleness",
        "tracked_account_count",
        "leader_lease",
        "position_record_invariants",
    }
    leaked = sorted(forbidden.intersection(payload))
    if leaked:
        raise ProbeError(f"public /health/summary leaked internal fields: {', '.join(leaked)}")
    print("PASS public /health/summary is redacted")


def _assert_frontend_uses_admin_summary() -> None:
    client = (REPO_ROOT / "frontend" / "src" / "client" / "index.ts").read_text(encoding="utf-8")
    overview = (REPO_ROOT / "frontend" / "src" / "pages" / "Overview.tsx").read_text(encoding="utf-8")
    safety = (REPO_ROOT / "frontend" / "src" / "pages" / "Safety.tsx").read_text(encoding="utf-8")
    required = [
        ("client admin health path", "bffPath('/admin/health/summary')" in client),
        ("client public fallback path", "path: '/health/summary'" in client),
        ("overview fail-closed classifier", "classifyOperatorHealth" in overview),
        ("overview public fallback warning", "public summary proves reachability only" in overview),
        ("safety admin-source gate", "healthSource !== 'admin'" in safety),
        ("safety fail-closed classifier", "classifyOperatorHealth" in safety),
    ]
    missing = [name for name, ok in required if not ok]
    if missing:
        raise ProbeError(f"frontend admin summary wiring missing: {', '.join(missing)}")
    print("PASS Overview and Safety source use authenticated admin summary and fail closed")


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only Phoenix admin console smoke.")
    parser.add_argument("--base-url", default="http://127.0.0.1")
    parser.add_argument("--backend-container", default="phoenix-v9-backend")
    parser.add_argument("--admin-key-file", default=None)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()

    try:
        admin_key = _load_admin_key(args.backend_container, args.admin_key_file)

        for path in ("/admin/health/summary", "/admin/release-evidence"):
            status, content_type, raw = _request(args.base_url, path, timeout=args.timeout)
            _assert_status(f"unauthenticated {path}", status, 401)
            if raw and "traceback" in raw.decode("utf-8", errors="ignore").lower():
                raise ProbeError(f"{path} unauthenticated response contains a traceback")

        for path in ("/admin/health/summary", "/admin/release-evidence", "/bff/admin/health/summary"):
            status, content_type, raw = _request(
                args.base_url,
                path,
                admin_key=admin_key,
                timeout=args.timeout,
            )
            _assert_status(f"authenticated {path}", status, 200)
            payload = _parse_json(status, content_type, raw, path)
            _assert_no_sensitive_values(payload)

        status, content_type, raw = _request(args.base_url, "/health/summary", timeout=args.timeout)
        _assert_status("public /health/summary", status, 200)
        public_summary = _parse_json(status, content_type, raw, "/health/summary")
        _assert_public_health_redacted(public_summary)

        for path in ("/bff/health/summary", "/bff/readyz", "/bff/dashboard/status"):
            status, _content_type, raw = _request(args.base_url, path, admin_key=admin_key, timeout=args.timeout)
            _assert_status(f"blocked direct diagnostic {path}", status, 404)
            text = raw.decode("utf-8", errors="ignore").lower()
            if "traceback" in text or "stack trace" in text:
                raise ProbeError(f"{path} response contains stack trace text")

        _assert_frontend_uses_admin_summary()
    except ProbeError as exc:
        print(f"FAIL {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
