"""Minimal conninfo helpers compatible with the repository tests."""

from __future__ import annotations

from typing import Dict
from urllib.parse import parse_qsl, quote, unquote, urlparse


def conninfo_to_dict(dsn: str) -> Dict[str, str]:
    text = str(dsn or "").strip()
    if not text:
        return {}

    if "://" in text:
        parsed = urlparse(text)
        result: Dict[str, str] = {}
        if parsed.hostname:
            result["host"] = parsed.hostname
        if parsed.port is not None:
            result["port"] = str(parsed.port)
        if parsed.username is not None:
            result["user"] = unquote(parsed.username)
        if parsed.password is not None:
            result["password"] = unquote(parsed.password)
        path = (parsed.path or "").lstrip("/")
        if path:
            result["dbname"] = unquote(path)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True):
            result[str(key)] = str(value)
        return result

    result: Dict[str, str] = {}
    for token in text.split():
        if "=" not in token:
            continue
        key, value = token.split("=", 1)
        key = str(key).strip()
        value = str(value).strip().strip("\"'")
        if key:
            result[key] = value
    return result


def make_conninfo(**params: object) -> str:
    normalized = {str(key): str(value) for key, value in params.items() if value not in (None, "")}
    host = normalized.get("host")
    dbname = normalized.get("dbname") or normalized.get("database")
    user = normalized.get("user")
    password = normalized.get("password")
    port = normalized.get("port")

    if host and dbname:
        auth = ""
        if user:
            auth = quote(user, safe="")
            if password is not None:
                auth += ":" + quote(password, safe="")
            auth += "@"
        uri = f"postgresql://{auth}{host}"
        if port:
            uri += f":{port}"
        uri += f"/{quote(dbname, safe='')}"
        extras = {
            key: value
            for key, value in normalized.items()
            if key not in {"host", "dbname", "database", "user", "password", "port"}
        }
        if extras:
            query = "&".join(f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in extras.items())
            uri += "?" + query
        return uri

    return " ".join(f"{key}={value}" for key, value in normalized.items())


__all__ = ["conninfo_to_dict", "make_conninfo"]
