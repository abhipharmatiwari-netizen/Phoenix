from __future__ import annotations

from typing import Optional, Union


def is_html_block(body: Union[str, bytes, None], content_type: Optional[str] = None) -> bool:
    if content_type and "text/html" in content_type.lower():
        return True
    if body is None:
        return False
    if isinstance(body, bytes):
        text = body.decode("utf-8", errors="ignore")
    else:
        text = str(body)
    stripped = text.lstrip()
    lowered = stripped.lower()
    if lowered.startswith("<!doctype html") or lowered.startswith("<html"):
        return True
    if "request rejected" in lowered:
        return True
    return False
