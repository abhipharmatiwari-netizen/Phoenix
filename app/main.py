"""Entrypoint for Cloud Run.

This module is executed by the container CMD:

    python -m app.main

Cloud Run expects the process to start an HTTP server and listen on the port
provided in the PORT environment variable (default 8080).

Modes:
- RUNNER_MODE=uvicorn (default): start FastAPI (app.server:app) on 0.0.0.0:PORT
"""

from __future__ import annotations

import logging
import re
import shutil
import socket
from logging.handlers import RotatingFileHandler
import os
import sys
from pathlib import Path
from zoneinfo import ZoneInfo
from datetime import datetime
from typing import Optional

from dotenv import load_dotenv

# Parse a truthy environment value.
def _env_truthy(value: Optional[str]) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def _as_int_env(name: str, default: int) -> int:
    try:
        raw = os.getenv(name)
        return int(raw) if raw is not None else default
    except Exception:
        return default


def _env_file_looks_like_template(path: Path) -> bool:
    try:
        content = path.read_text(encoding="utf-8")
    except Exception:
        return False
    template_markers = (
        "CHANGE_ME",
        "CHANGE_ME_LONG_RANDOM_STRING",
    )
    return any(marker in content for marker in template_markers)


def _force_utf8_stdio() -> None:
    # Keep log output stable across Windows terminals and redirected streams.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="backslashreplace")
            except Exception:
                pass


# Configure basic stdout/stderr logging for Cloud Run and local runs.
def _configure_logging() -> None:
    _force_utf8_stdio()
    level = os.getenv("LOG_LEVEL", "INFO").upper()
    log_format = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    formatter = logging.Formatter(log_format)
    for handler in root_logger.handlers:
        if isinstance(handler, logging.StreamHandler):
            stream = getattr(handler, "stream", None)
            reconfigure = getattr(stream, "reconfigure", None)
            if callable(reconfigure):
                try:
                    reconfigure(encoding="utf-8", errors="backslashreplace")
                except Exception:
                    pass

    # Always keep a console handler for Cloud Run/stdout.
    if not any(isinstance(h, logging.StreamHandler) for h in root_logger.handlers):
        stream_handler = logging.StreamHandler(stream=sys.stdout)
        stream_handler.setFormatter(formatter)
        root_logger.addHandler(stream_handler)

    # Add a rotating file handler so logs are persisted locally.
    # APP_LOG_DIR overrides the default so test suites can redirect to an
    # isolated directory (e.g. .test_tmp/logs) and not contaminate production
    # log files that are bind-mounted from ./logs in the Docker compose stack.
    base_dir = Path(__file__).resolve().parents[1]
    _app_log_dir_env = os.getenv("APP_LOG_DIR", "").strip()
    if _app_log_dir_env:
        log_root = Path(_app_log_dir_env).resolve()
    else:
        log_root = base_dir / "logs"
    log_root.mkdir(parents=True, exist_ok=True)
    tz_name = os.getenv("DEFAULT_TIME_ZONE", "Asia/Kolkata")
    try:
        tz = ZoneInfo(tz_name)
    except Exception:
        tz = ZoneInfo("UTC")
    date_dir = log_root / datetime.now(tz).date().isoformat()
    date_dir.mkdir(parents=True, exist_ok=True)
    # Move legacy files from root logs/ into daily folders (except indicator/scrip master).
    def _is_date_folder(value: str) -> bool:
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""))

    def _target_dir_for_name(name: str) -> Path:
        match = re.search(r"\d{4}-\d{2}-\d{2}", name or "")
        if match:
            return log_root / match.group(0)
        return date_dir

    try:
        for entry in log_root.iterdir():
            if not entry.is_file():
                continue
            name = entry.name
            if name.startswith("indicator_bars"):
                continue
            if name.startswith("scrip_master_"):
                continue
            target_dir = _target_dir_for_name(name)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / name
            if target.exists():
                continue
            shutil.move(str(entry), str(target))
    except Exception:
        logger = logging.getLogger(__name__)
        logger.warning("Failed to migrate legacy log files to daily folders")
    def _should_use_per_process_log() -> bool:
        explicit = os.getenv("LOG_FILE_PER_PROCESS")
        if explicit is not None:
            return _env_truthy(explicit)
        if _as_int_env("WEB_CONCURRENCY", 1) > 1:
            return True
        if _as_int_env("UVICORN_WORKERS", 1) > 1:
            return True
        return False

    log_file_name = "app.log"
    if _should_use_per_process_log():
        log_file_name = f"app_{os.getpid()}.log"
    log_file = date_dir / log_file_name

    file_handler_exists = False
    stale_file_handlers: list[logging.FileHandler] = []
    for handler in list(root_logger.handlers):
        if isinstance(handler, logging.FileHandler):
            try:
                handler_path = Path(getattr(handler, "baseFilename", "")).resolve()
                if handler_path == log_file.resolve():
                    file_handler_exists = True
                else:
                    stale_file_handlers.append(handler)
            except Exception:
                continue
    for handler in stale_file_handlers:
        root_logger.removeHandler(handler)
        try:
            handler.close()
        except Exception:
            pass
    if not file_handler_exists:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=10 * 1024 * 1024,  # 10 MB per file
            backupCount=5,
            encoding="utf-8",
            errors="backslashreplace",
        )
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


# Detect whether we are running inside a Cloud Run container.
def _running_in_cloud_run() -> bool:
    return bool(os.getenv("K_SERVICE") or os.getenv("K_REVISION"))


# Load local Cloud Run env file and credentials when running outside Cloud Run.
def _load_cloudrun_env_if_local() -> None:
    if _running_in_cloud_run():
        return
    # Prefer localrun.env when running locally. The root cloudrun.env file is a
    # sanitized template and should not be auto-loaded while it still contains
    # placeholder values.
    base_dir = Path(__file__).resolve().parents[1]
    local_env_path = base_dir / "localrun.env"
    cloudrun_env_path = base_dir / "cloudrun.env"
    preserve_keys = ("RUNNER_MODE", "HOST", "PORT", "UVICORN_LOG_LEVEL", "LOG_LEVEL")
    preserved = {key: os.environ.get(key) for key in preserve_keys if key in os.environ}
    env_path: Path | None = None
    if local_env_path.exists():
        env_path = local_env_path
    elif cloudrun_env_path.exists():
        if _env_file_looks_like_template(cloudrun_env_path):
            logging.getLogger(__name__).info(
                "Skipping template cloudrun.env auto-load; use localrun.env or replace placeholders first."
            )
        else:
            env_path = cloudrun_env_path
    try:
        if env_path:
            load_dotenv(env_path, override=True)
            for key, value in preserved.items():
                os.environ[key] = value
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip("\"").strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value

    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Failed to load local env credentials: %s", exc
        )


# Convert a string to int with a fallback default.
def _as_int(value: Optional[str], default: int) -> int:
    try:
        return int(value) if value is not None else default
    except Exception:
        return default


def _is_port_preflight_enabled() -> bool:
    raw = os.getenv("PORT_BIND_PREFLIGHT")
    if raw is None:
        return True
    return _env_truthy(raw)


def _port_preflight_mode() -> str:
    # strict: error + exit, warn: warning + exit, soft: warning + continue.
    mode = str(os.getenv("PORT_BIND_PREFLIGHT_MODE", "strict")).strip().lower()
    if mode in {"strict", "warn", "soft"}:
        return mode
    return "strict"


def _preflight_port_bind(host: str, port: int) -> bool:
    if not _is_port_preflight_enabled():
        return True

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            # Windows: reject shared binds so active listeners are detected cleanly.
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((host, port))
        return True
    except OSError as exc:
        mode = _port_preflight_mode()
        msg = (
            "Port preflight failed for %s:%s; "
            "startup may conflict with an existing worker: %s"
        )
        if mode == "soft":
            logging.warning(msg + " (mode=soft; continuing startup)", host, port, exc)
            return True
        if mode == "warn":
            logging.warning(msg + " (mode=warn; exiting)", host, port, exc)
            return False
        logging.error(msg + " (mode=strict; exiting)", host, port, exc)
        return False
    finally:
        sock.close()


# Start the service in uvicorn mode.
def main() -> None:
    """Cloud Run default: start FastAPI via uvicorn."""
    _load_cloudrun_env_if_local()
    _configure_logging()

    # Allow running as a script (python app/main.py)
    if __package__ in (None, ""):
        sys.path.append(str(Path(__file__).resolve().parents[1]))

    runner_mode = os.getenv("RUNNER_MODE", "uvicorn").strip().lower()
    if runner_mode != "uvicorn":
        logging.error(
            "Unsupported RUNNER_MODE=%s. Use RUNNER_MODE=uvicorn.",
            runner_mode,
        )
        sys.exit(1)

    host = os.getenv("HOST", "0.0.0.0")
    port = _as_int(os.getenv("PORT"), 8080)
    if not _preflight_port_bind(host, port):
        sys.exit(1)

    # Import here so import errors are logged with our logging config.
    try:
        import uvicorn
    except ImportError as exc:
        logging.error("Failed to import uvicorn: %s", exc)
        sys.exit(1)

    log_level = os.getenv("UVICORN_LOG_LEVEL") or os.getenv("LOG_LEVEL", "info")
    log_level = log_level.lower()

    logging.info("Starting uvicorn on %s:%s (RUNNER_MODE=%s)", host, port, runner_mode)
    # Use app.server:app so FastAPI startup hooks run (worker threads, hub, watchdog).
    uvicorn.run(
        "app.server:app",
        host=host,
        port=port,
        log_level=log_level,
        proxy_headers=True,
        forwarded_allow_ips="*"
    )


if __name__ == "__main__":
    main()
