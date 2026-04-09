"""Session revocation store — PHX-SEC-005.

Tracks revoked token JTI values (jti claim) and issued refresh tokens
so that forced logout and session expiry are possible without waiting
for JWT TTL.

Design:
- Every issued access token includes a `jti` (JWT ID) claim.
- Revoked JTIs are stored in a thread-safe in-memory set AND optionally
  persisted to Postgres (table: revoked_tokens) when available.
- Refresh tokens are single-use, rotated on every use, and stored in
  Postgres (table: refresh_tokens).
- The in-memory set is the fast-path check; Postgres is the durable store
  consulted on restart.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional
from uuid import uuid4

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory revocation set (fast path)
# ---------------------------------------------------------------------------

_REVOKED_JTIS: set[str] = set()
_REVOCATION_LOCK = threading.Lock()

_REFRESH_TOKENS: dict[str, dict] = {}  # token -> {user_id, email, role, exp}
_REFRESH_LOCK = threading.Lock()

_REFRESH_TOKEN_TTL = 7 * 24 * 60 * 60  # 7 days


def _try_postgres_revoke(jti: str, expires_at: int) -> None:
    try:
        from psycopg.rows import tuple_row  # type: ignore
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO revoked_tokens (jti, revoked_at, expires_at)
                    VALUES (%s, NOW(), TO_TIMESTAMP(%s))
                    ON CONFLICT (jti) DO NOTHING
                    """,
                    (jti, expires_at),
                )
    except Exception:
        logger.debug("Postgres token revocation unavailable — in-memory only", exc_info=True)


def _load_revoked_from_postgres() -> None:
    """Load unexpired revoked JTIs from Postgres on startup."""
    try:
        from psycopg.rows import tuple_row  # type: ignore
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor(row_factory=tuple_row) as cur:
                cur.execute(
                    "SELECT jti FROM revoked_tokens WHERE expires_at > NOW()"
                )
                rows = cur.fetchall()
        with _REVOCATION_LOCK:
            for (jti,) in rows:
                _REVOKED_JTIS.add(jti)
        logger.info("Loaded %d revoked JTIs from Postgres", len(rows))
    except Exception:
        logger.debug("Could not load revoked tokens from Postgres", exc_info=True)


def revoke_token(jti: str, expires_at: int) -> None:
    """Revoke a token by JTI. Thread-safe."""
    with _REVOCATION_LOCK:
        _REVOKED_JTIS.add(jti)
    _try_postgres_revoke(jti, expires_at)
    logger.info("Token revoked jti=%s", jti)


def is_revoked(jti: str) -> bool:
    """Return True if the JTI is in the revocation list."""
    with _REVOCATION_LOCK:
        return jti in _REVOKED_JTIS


def revoke_all_for_user(user_id: str) -> int:
    """Revoke all refresh tokens for a user (forced logout). Returns count revoked."""
    revoked = 0
    with _REFRESH_LOCK:
        to_remove = [
            tok for tok, data in _REFRESH_TOKENS.items()
            if data.get("user_id") == user_id
        ]
        for tok in to_remove:
            del _REFRESH_TOKENS[tok]
            revoked += 1
    logger.info("Revoked %d refresh tokens for user_id=%s", revoked, user_id)
    return revoked


# ---------------------------------------------------------------------------
# Refresh token management
# ---------------------------------------------------------------------------

def issue_refresh_token(
    *,
    user_id: str,
    email: str,
    role: str,
) -> tuple[str, int]:
    """Issue a single-use refresh token. Returns (token, expires_at)."""
    token = uuid4().hex + uuid4().hex
    expires_at = int(time.time()) + _REFRESH_TOKEN_TTL
    with _REFRESH_LOCK:
        _REFRESH_TOKENS[token] = {
            "user_id": user_id,
            "email": email,
            "role": role,
            "exp": expires_at,
        }
    _try_postgres_refresh_insert(token, user_id, email, role, expires_at)
    return token, expires_at


def consume_refresh_token(token: str) -> Optional[dict]:
    """Consume a refresh token (single-use). Returns payload or None if invalid/expired."""
    now = int(time.time())
    with _REFRESH_LOCK:
        data = _REFRESH_TOKENS.pop(token, None)
    if data is None:
        # Try Postgres
        data = _try_postgres_consume_refresh(token)
    if data is None:
        return None
    if data.get("exp", 0) <= now:
        return None
    return data


def _try_postgres_refresh_insert(
    token: str, user_id: str, email: str, role: str, expires_at: int
) -> None:
    try:
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO refresh_tokens
                        (token, user_id, email, role, expires_at, used)
                    VALUES (%s, %s, %s, %s, TO_TIMESTAMP(%s), FALSE)
                    """,
                    (token, user_id, email, role, expires_at),
                )
    except Exception:
        logger.debug("Postgres refresh token insert unavailable", exc_info=True)


def _try_postgres_consume_refresh(token: str) -> Optional[dict]:
    try:
        from psycopg.rows import dict_row  # type: ignore
        from app.data.postgres import connect_with_retry, get_control_plane_dsn
        dsn = get_control_plane_dsn()
        with connect_with_retry(dsn, autocommit=True) as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    """
                    UPDATE refresh_tokens
                    SET used = TRUE, used_at = NOW()
                    WHERE token = %s AND used = FALSE AND expires_at > NOW()
                    RETURNING user_id, email, role,
                              EXTRACT(EPOCH FROM expires_at)::BIGINT AS exp
                    """,
                    (token,),
                )
                row = cur.fetchone()
        if row:
            return dict(row)
        return None
    except Exception:
        logger.debug("Postgres refresh token consume unavailable", exc_info=True)
        return None


__all__ = [
    "consume_refresh_token",
    "is_revoked",
    "issue_refresh_token",
    "revoke_all_for_user",
    "revoke_token",
]
